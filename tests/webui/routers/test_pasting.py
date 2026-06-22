"""`webui.routers.pasting`（pad-config API）の仕様テスト.

計画書 Phase 3「src/webui/routers/pasting.py」節が契約:

- PCB 未選択 → 409
- GET /api/pasting/pad-config の構造（outline / pads / tree / defaults /
  overrides、pad.resolved / enabled）
- PATCH node（enabled トグル → affected_pads、values upsert → 子 pad の
  resolved 反映、clear → 継承復帰）
- PATCH pads 一括
- 未知キー / 未知 node → 400
- POST reset で override 破棄

実 PCB（led_blinker）を読む経路は ``copper_pcb_path`` fixture を使う
（pcbnew 依存。conftest が pcb_browse_root へコピー済み）。設定書き込み先は
test-fixture（webui_settings の tmp data_dir 配下）。

node_id 規約（契約）: L0 / L1:{package} / L2:{designator} /
L3:{designator}:{shape_label} / L4:{designator}:{pad_ref}。
pad id = {designator}.{pad_ref}。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webui.settings import Settings
from webui.state import AppState

# led_blinker の安定した参照点
_U1_PADS = {f"U1.{n}" for n in range(1, 7)}  # U1 は 6 pad、全て同形状（L3 単一）


@pytest.fixture
def selected_client(
    client: TestClient, appstate: AppState, copper_pcb_path: Path
) -> TestClient:
    """led_blinker を選択済みの TestClient."""
    appstate.select_pcb(copper_pcb_path)
    return client


def _get_config(client: TestClient) -> dict:
    response = client.get("/api/pasting/pad-config")
    assert response.status_code == 200, response.text
    return response.json()


def _pad_by_id(config: dict, pad_id: str) -> dict:
    return next(pad for pad in config["pads"] if pad["id"] == pad_id)


def _node_by_id(node: dict, node_id: str) -> dict:
    stack = [node]
    while stack:
        current = stack.pop()
        if current["id"] == node_id:
            return current
        stack.extend(current["children"])
    raise AssertionError(f"node not found: {node_id}")


def _leaf_pad_ids(node: dict) -> set[str]:
    if node["level"] == 4:
        _, designator, pad_number = node["id"].split(":", 2)
        return {f"{designator}.{pad_number}"}
    ids: set[str] = set()
    for child in node["children"]:
        ids |= _leaf_pad_ids(child)
    return ids


class TestPcbNotSelected:
    """PCB 未選択 → 409."""

    def test_get_without_selection_returns_409(self, client: TestClient):
        response = client.get("/api/pasting/pad-config")
        assert response.status_code == 409

    def test_patch_node_without_selection_returns_409(self, client: TestClient):
        response = client.patch(
            "/api/pasting/pad-config/node", json={"node": "L0", "enabled": False}
        )
        assert response.status_code == 409


class TestGetPadConfig:
    """GET /api/pasting/pad-config の構造."""

    def test_top_level_shape(self, selected_client: TestClient):
        config = _get_config(selected_client)

        assert config["pcb_file"].endswith("led_blinker.kicad_pcb")
        assert config["machine"] == "kurousagi"
        assert config["width"] > 0
        assert config["height"] > 0
        assert len(config["outline"]) >= 4
        assert isinstance(config["tree"], dict)
        assert config["tree"]["id"] == "L0"
        assert config["tree"]["level"] == 0
        assert len(config["pads"]) == 18

    def test_defaults_come_from_machine_config(self, selected_client: TestClient):
        config = _get_config(selected_client)
        defaults = config["defaults"]

        assert defaults["enabled"] is True
        assert defaults["dispense_mode"] == "auto"
        assert defaults["fill_speed"] == 0.8  # test-fixture machine.toml 由来
        assert defaults["paste_height"] == "auto"
        assert defaults["bead_width_factor"] == 1.0  # PasteDispenser 既定
        assert defaults["boundary_margin"] == 0.0

    def test_pad_has_geometry_and_resolved(self, selected_client: TestClient):
        config = _get_config(selected_client)
        pad = _pad_by_id(config, "U1.1")

        assert pad["designator"] == "U1"
        assert pad["pad_number"] == "1"
        assert pad["package"] == "SOT-23-6"
        assert pad["layer"] in ("Top", "Bottom")
        assert len(pad["polygon"]) >= 4
        assert all(len(point) == 2 for point in pad["polygon"])
        assert pad["enabled"] is True
        assert pad["resolved"]["dispense_mode"] == "auto"
        assert pad["resolved"]["fill_speed"] == 0.8
        assert pad["resolved"]["paste_height"] == "auto"

    def test_pad_exposes_full_node_id_path(self, selected_client: TestClient):
        config = _get_config(selected_client)
        pad = _pad_by_id(config, "U1.1")

        node_ids = pad["node_ids"]
        assert all(isinstance(node_id, str) for node_id in node_ids)
        assert [node_id.split(":", 1)[0] for node_id in node_ids] == [
            "L0",
            "L1",
            "L2",
            "L3",
            "L4",
        ]
        assert node_ids[0] == "L0"
        assert "L1:SOT-23-6" in node_ids
        assert "L2:U1" in node_ids
        assert any(node_id.startswith("L3:U1:") for node_id in node_ids)
        assert node_ids[-1] == "L4:U1:1"

    def test_l3_membership_is_discoverable_from_node_ids(
        self, selected_client: TestClient
    ):
        config = _get_config(selected_client)
        u1_node = _node_by_id(config["tree"], "L2:U1")
        l3_node = next(child for child in u1_node["children"] if child["level"] == 3)
        l3_node_id = l3_node["id"]

        expected_pad_ids = _leaf_pad_ids(l3_node)
        matched_pad_ids = {
            pad["id"] for pad in config["pads"] if l3_node_id in pad["node_ids"]
        }

        assert expected_pad_ids
        assert matched_pad_ids == expected_pad_ids

    def test_overrides_start_empty(self, selected_client: TestClient):
        config = _get_config(selected_client)

        assert config["overrides"] == {}

    def test_tree_contains_u1_node(self, selected_client: TestClient):
        config = _get_config(selected_client)
        l1_ids = {child["id"] for child in config["tree"]["children"]}
        assert "L1:SOT-23-6" in l1_ids


class TestPatchNode:
    """PATCH /api/pasting/pad-config/node."""

    def test_l4_enable_toggle_affects_single_pad(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L4:U1:1", "enabled": False},
        )

        assert response.status_code == 200, response.text
        affected = response.json()["affected_pads"]
        assert {pad["id"] for pad in affected} == {"U1.1"}
        assert affected[0]["enabled"] is False

    def test_l2_values_upsert_reflects_on_children(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"bead_width_factor": 0.8}},
        )

        assert response.status_code == 200, response.text
        affected = response.json()["affected_pads"]
        assert {pad["id"] for pad in affected} == _U1_PADS
        for pad in affected:
            assert pad["resolved"]["bead_width_factor"] == 0.8

    def test_value_upsert_persists_on_reget(self, selected_client: TestClient):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"fill_speed": 0.3}},
        )

        config = _get_config(selected_client)
        assert config["overrides"]["L2:U1"]["values"]["fill_speed"] == 0.3
        assert _pad_by_id(config, "U1.1")["resolved"]["fill_speed"] == 0.3
        # U1 以外には波及しない
        assert _pad_by_id(config, "R1.1")["resolved"]["fill_speed"] == 0.8

    def test_mode_and_height_upsert_persist_on_reget(self, selected_client: TestClient):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={
                "node": "L2:U1",
                "values": {
                    "dispense_mode": "line",
                    "paste_height": "auto",
                },
            },
        )
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L4:U1:1", "values": {"paste_height": 0.25}},
        )

        config = _get_config(selected_client)
        assert config["overrides"]["L2:U1"]["values"]["dispense_mode"] == "line"
        assert config["overrides"]["L2:U1"]["values"]["paste_height"] == "auto"
        assert _pad_by_id(config, "U1.2")["resolved"]["dispense_mode"] == "line"
        assert _pad_by_id(config, "U1.2")["resolved"]["paste_height"] == "auto"
        assert _pad_by_id(config, "U1.1")["resolved"]["paste_height"] == 0.25

    def test_clear_returns_to_inheritance(self, selected_client: TestClient):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"fill_speed": 0.3}},
        )

        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "clear": ["fill_speed"]},
        )

        assert response.status_code == 200, response.text
        config = _get_config(selected_client)
        # override が空 + enabled 継承 → ノードは overrides から消える（疎）
        assert "L2:U1" not in config["overrides"]
        assert _pad_by_id(config, "U1.1")["resolved"]["fill_speed"] == 0.8

    def test_l0_enable_toggle_persists(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L0", "enabled": False},
        )

        assert response.status_code == 200, response.text
        config = _get_config(selected_client)
        assert config["defaults"]["enabled"] is True
        assert config["overrides"]["L0"]["enabled"] is False
        # 全 pad が無効に解決される
        assert all(pad["enabled"] is False for pad in config["pads"])

    def test_l0_value_override_persists(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L0", "values": {"fill_speed": 0.35}},
        )

        assert response.status_code == 200, response.text
        config = _get_config(selected_client)
        assert config["defaults"]["fill_speed"] == 0.8
        assert config["overrides"]["L0"]["values"]["fill_speed"] == 0.35
        assert all(pad["resolved"]["fill_speed"] == 0.35 for pad in config["pads"])

    def test_l0_clear_returns_to_machine_default(self, selected_client: TestClient):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L0", "values": {"fill_speed": 0.35}},
        )

        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L0", "clear": ["fill_speed"]},
        )

        assert response.status_code == 200, response.text
        config = _get_config(selected_client)
        assert "L0" not in config["overrides"]
        assert _pad_by_id(config, "U1.1")["resolved"]["fill_speed"] == 0.8

    def test_unknown_value_key_returns_400(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"no_such_field": 1.0}},
        )
        assert response.status_code == 400

    def test_unknown_dispense_mode_returns_400(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"dispense_mode": "spray"}},
        )
        assert response.status_code == 400

    def test_invalid_paste_height_returns_400(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"paste_height": 0.0}},
        )
        assert response.status_code == 400

    def test_unknown_node_returns_400(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:NOPE", "enabled": False},
        )
        assert response.status_code == 400


class TestPatchPads:
    """PATCH /api/pasting/pad-config/pads（一括 enabled）."""

    def test_bulk_disable(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/pads",
            json={"ids": ["U1.1", "U1.2"], "enabled": False},
        )

        assert response.status_code == 200, response.text
        affected = response.json()["affected_pads"]
        assert {pad["id"] for pad in affected} == {"U1.1", "U1.2"}
        assert all(pad["enabled"] is False for pad in affected)

    def test_bulk_disable_persists(self, selected_client: TestClient):
        selected_client.patch(
            "/api/pasting/pad-config/pads",
            json={"ids": ["U1.1", "U1.2"], "enabled": False},
        )

        config = _get_config(selected_client)
        assert _pad_by_id(config, "U1.1")["enabled"] is False
        assert _pad_by_id(config, "U1.2")["enabled"] is False
        assert _pad_by_id(config, "U1.3")["enabled"] is True

    def test_persistence_uses_webui_data_dir(
        self, selected_client: TestClient, webui_settings: Settings
    ):
        selected_client.patch(
            "/api/pasting/pad-config/pads",
            json={"ids": ["U1.1"], "enabled": False},
        )

        assert list((webui_settings.webui_data_dir / "board_settings").rglob("*.json"))
        assert not (webui_settings.data_dir / "board_settings").exists()


class TestReset:
    """POST /api/pasting/pad-config/reset."""

    def test_reset_discards_overrides(self, selected_client: TestClient):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"fill_speed": 0.3}},
        )

        response = selected_client.post("/api/pasting/pad-config/reset")

        assert response.status_code == 200, response.text
        config = response.json()
        assert config["overrides"] == {}
        assert _pad_by_id(config, "U1.1")["resolved"]["fill_speed"] == 0.8


class TestExportImport:
    """GET export / POST import."""

    def test_export_contains_version_signature_and_settings(
        self, selected_client: TestClient
    ):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"fill_speed": 0.3}},
        )

        response = selected_client.get("/api/pasting/pad-config/export")

        assert response.status_code == 200, response.text
        assert "attachment" in response.headers["content-disposition"]
        doc = response.json()
        assert doc["version"] == 1
        assert doc["machine"] == "kurousagi"
        assert doc["source_pcb"].endswith("led_blinker.kicad_pcb")
        assert isinstance(doc["board_signature"], str)
        level = next(
            item for item in doc["settings"]["levels"] if item["key"] == ["L2", "U1"]
        )
        assert level["override"]["fill_speed"] == 0.3

    def test_import_restores_saved_override(self, selected_client: TestClient):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"fill_speed": 0.3}},
        )
        doc = selected_client.get("/api/pasting/pad-config/export").json()
        selected_client.post("/api/pasting/pad-config/reset")

        response = selected_client.post(
            "/api/pasting/pad-config/import", json={"document": doc}
        )

        assert response.status_code == 200, response.text
        config = response.json()
        assert config["overrides"]["L2:U1"]["values"]["fill_speed"] == 0.3
        assert _pad_by_id(config, "U1.1")["resolved"]["fill_speed"] == 0.3

    def test_import_rejects_wrong_signature(self, selected_client: TestClient):
        doc = selected_client.get("/api/pasting/pad-config/export").json()
        doc["board_signature"] = "wrong"

        response = selected_client.post(
            "/api/pasting/pad-config/import", json={"document": doc}
        )

        assert response.status_code == 400
