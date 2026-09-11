"""`web.api.routers.pasting`（pad-config API）の仕様テスト.

計画書 Phase 3「src/webui/routers/pasting.py」節が契約:

- PCB 未選択 → 409
- GET /api/pasting/pad-config の構造（outline / pads / tree / defaults /
  overrides、pad.resolved / enabled）
- PATCH node（enabled トグル → affected_pads、values upsert → 子 pad の
  resolved 反映、clear → 継承復帰）
- PATCH pads 一括
- 未知キー / 未知 node → 400

実 PCB（led_blinker）を読む経路は ``copper_pcb_path`` fixture を使う
（pcbnew 依存。conftest が pcb_browse_root へコピー済み）。設定書き込み先は
tmp（webui_settings の tmp data_dir 配下）。

node_id 規約（契約）: L0 / L1:{package} / L2:{designator} /
L3:{designator}:{shape_label} / L4:{designator}:{pad_ref}。
pad id = {designator}.{pad_ref}。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pcbasm.pasting.params import PASTE_PARAM_NAMES
from web.api.settings import Settings
from web.api.state import AppState

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


def _saved_board_settings_doc(webui_settings: Settings) -> dict:
    paths = list((webui_settings.webui_data_dir / "board_settings").rglob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


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
        assert defaults["line_direction"] == "unconstrained"
        assert (
            defaults["prime_extra_delay"] == 0.0
        )  # テスト用 config の machine.toml 由来
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
        assert pad["resolved"]["line_direction"] == "unconstrained"
        assert pad["resolved"]["prime_extra_delay"] == 0.0
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

    def test_tree_node_carries_resolved_own_override_and_summary(
        self, selected_client: TestClient
    ):
        config = _get_config(selected_client)
        u1_node = _node_by_id(config["tree"], "L2:U1")

        assert "resolved" in u1_node
        assert "own_override" in u1_node
        assert "descendant_summary" in u1_node


class TestInitialPurgePadConfig:
    """GET/PATCH initial_purge."""

    def test_get_includes_default_initial_purge_resolution(
        self, selected_client: TestClient
    ):
        config = _get_config(selected_client)
        initial = config["initial_purge"]

        assert initial["initial_purge_ul"] == pytest.approx(0.1)
        assert initial["point"] is None
        assert initial["resolved"]["source"] == "default"
        assert initial["resolved"]["amount"] == pytest.approx(0.1)
        # 自動は塗布順路先頭 pad の中心座標
        assert initial["resolved"]["point"] == pytest.approx([15.0, 4.212500000000003])
        assert initial["default_point"] == initial["resolved"]["point"]
        assert "自動" in initial["selection_label"]

    def test_patch_saves_machine_amount_and_board_point(
        self,
        selected_client: TestClient,
        config_dir: Path,
        webui_settings: Settings,
    ):
        response = selected_client.patch(
            "/api/pasting/pad-config/initial-purge",
            json={"initial_purge_ul": 0.25, "point": [15.0, 4.0]},
        )

        assert response.status_code == 200, response.text
        initial = response.json()["initial_purge"]
        assert initial["initial_purge_ul"] == pytest.approx(0.25)
        assert initial["point"] == pytest.approx([15.0, 4.0])
        assert initial["resolved"]["source"] == "explicit"

        machine_toml = config_dir / "machine.toml"
        assert "initial_purge_ul = 0.25" in machine_toml.read_text(encoding="utf-8")
        doc = _saved_board_settings_doc(webui_settings)
        assert doc["settings"]["initial_purge_point"] == pytest.approx([15.0, 4.0])

    def test_patch_point_after_amount_preserves_machine_amount(
        self,
        selected_client: TestClient,
        config_dir: Path,
        webui_settings: Settings,
    ):
        amount_response = selected_client.patch(
            "/api/pasting/pad-config/initial-purge",
            json={"initial_purge_ul": 0.22},
        )
        assert amount_response.status_code == 200, amount_response.text

        point_response = selected_client.patch(
            "/api/pasting/pad-config/initial-purge",
            json={"point": [15.0, 4.0]},
        )

        assert point_response.status_code == 200, point_response.text
        initial = point_response.json()["initial_purge"]
        assert initial["initial_purge_ul"] == pytest.approx(0.22)
        assert initial["point"] == pytest.approx([15.0, 4.0])

        machine_toml = config_dir / "machine.toml"
        assert "initial_purge_ul = 0.22" in machine_toml.read_text(encoding="utf-8")
        doc = _saved_board_settings_doc(webui_settings)
        assert doc["settings"]["initial_purge_point"] == pytest.approx([15.0, 4.0])

    def test_patch_amount_without_a_point_keeps_the_automatic_selection(
        self, selected_client: TestClient
    ):
        response = selected_client.patch(
            "/api/pasting/pad-config/initial-purge",
            json={"initial_purge_ul": 0.25},
        )

        assert response.status_code == 200, response.text
        initial = response.json()["initial_purge"]
        assert initial["point"] is None
        assert initial["resolved"]["source"] == "default"

    def test_patch_non_numeric_amount_returns_400(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/initial-purge",
            json={"initial_purge_ul": None},
        )

        assert response.status_code == 400

    def test_patch_without_selected_pcb_returns_409(self, client: TestClient):
        response = client.patch(
            "/api/pasting/pad-config/initial-purge",
            json={"initial_purge_ul": 0.25},
        )

        assert response.status_code == 409


class TestInitialPurgePoint:
    """PATCH initial-purge の座標指定（パージは pad ではなく座標で扱う）."""

    def test_patch_saves_the_point_and_resolves_it(
        self, selected_client: TestClient, webui_settings: Settings
    ):
        response = selected_client.patch(
            "/api/pasting/pad-config/initial-purge",
            json={"point": [15.0, 4.0]},
        )

        assert response.status_code == 200, response.text
        initial = response.json()["initial_purge"]
        assert initial["point"] == pytest.approx([15.0, 4.0])
        assert initial["resolved"]["source"] == "explicit"
        assert initial["resolved"]["point"] == pytest.approx([15.0, 4.0])
        assert "15.00" in initial["selection_label"]

        doc = _saved_board_settings_doc(webui_settings)
        assert doc["settings"]["initial_purge_point"] == pytest.approx([15.0, 4.0])

    def test_saved_point_survives_a_reload(self, selected_client: TestClient):
        saved = selected_client.patch(
            "/api/pasting/pad-config/initial-purge",
            json={"point": [15.0, 4.0]},
        )
        assert saved.status_code == 200, saved.text

        initial = _get_config(selected_client)["initial_purge"]

        assert initial["point"] == pytest.approx([15.0, 4.0])
        assert initial["resolved"]["source"] == "explicit"

    def test_null_point_returns_to_the_automatic_selection(
        self, selected_client: TestClient, webui_settings: Settings
    ):
        saved = selected_client.patch(
            "/api/pasting/pad-config/initial-purge",
            json={"point": [15.0, 4.0]},
        )
        assert saved.status_code == 200, saved.text

        response = selected_client.patch(
            "/api/pasting/pad-config/initial-purge",
            json={"point": None},
        )

        assert response.status_code == 200, response.text
        initial = response.json()["initial_purge"]
        assert initial["point"] is None
        assert initial["resolved"]["source"] == "default"
        doc = _saved_board_settings_doc(webui_settings)
        assert "initial_purge_point" not in doc["settings"]

    def test_point_outside_the_outline_returns_400(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/initial-purge",
            json={"point": [999.0, 999.0]},
        )

        assert response.status_code == 400
        assert "基板外形" in response.json()["detail"]

    @pytest.mark.parametrize("value", [[15.0], [15.0, 4.0, 1.0]])
    def test_malformed_point_returns_400(
        self, selected_client: TestClient, value: list[float]
    ):
        response = selected_client.patch(
            "/api/pasting/pad-config/initial-purge",
            json={"point": value},
        )

        assert response.status_code == 400


class TestFlowCalibrationPoint:
    """PATCH flow-calibration の座標指定（machine 設定とは別入口）."""

    def test_is_unset_and_disabled_by_default(self, selected_client: TestClient):
        flow = _get_config(selected_client)["flow_calibration"]

        assert flow["point"] is None
        assert flow["points"] is None
        assert flow["enabled"] is False
        assert "未設定" in flow["selection_label"]

    def test_patch_saves_the_point(
        self, selected_client: TestClient, webui_settings: Settings
    ):
        response = selected_client.patch(
            "/api/pasting/pad-config/flow-calibration",
            json={"point": [15.0, 4.0]},
        )

        assert response.status_code == 200, response.text
        flow = response.json()["flow_calibration"]
        assert flow["point"] == pytest.approx([15.0, 4.0])
        assert "15.00" in flow["selection_label"]

        doc = _saved_board_settings_doc(webui_settings)
        assert doc["settings"]["flow_calibration_point"] == pytest.approx([15.0, 4.0])

    def test_saved_point_survives_a_reload(self, selected_client: TestClient):
        saved = selected_client.patch(
            "/api/pasting/pad-config/flow-calibration",
            json={"point": [15.0, 4.0]},
        )
        assert saved.status_code == 200, saved.text

        flow = _get_config(selected_client)["flow_calibration"]

        assert flow["point"] == pytest.approx([15.0, 4.0])

    def test_null_point_returns_to_unset(
        self, selected_client: TestClient, webui_settings: Settings
    ):
        saved = selected_client.patch(
            "/api/pasting/pad-config/flow-calibration",
            json={"point": [15.0, 4.0]},
        )
        assert saved.status_code == 200, saved.text

        response = selected_client.patch(
            "/api/pasting/pad-config/flow-calibration",
            json={"point": None},
        )

        assert response.status_code == 200, response.text
        assert response.json()["flow_calibration"]["point"] is None
        doc = _saved_board_settings_doc(webui_settings)
        assert "flow_calibration_point" not in doc["settings"]

    def test_point_outside_the_outline_returns_400(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/flow-calibration",
            json={"point": [999.0, 999.0]},
        )

        assert response.status_code == 400
        assert "基板外形" in response.json()["detail"]

    @pytest.mark.parametrize("value", [[15.0], [15.0, 4.0, 1.0]])
    def test_malformed_point_returns_400(
        self, selected_client: TestClient, value: list[float]
    ):
        response = selected_client.patch(
            "/api/pasting/pad-config/flow-calibration",
            json={"point": value},
        )

        assert response.status_code == 400

    def test_an_empty_body_reports_the_current_state_without_saving(
        self, selected_client: TestClient, webui_settings: Settings
    ):
        response = selected_client.patch(
            "/api/pasting/pad-config/flow-calibration", json={}
        )

        assert response.status_code == 200, response.text
        assert response.json()["flow_calibration"]["point"] is None
        assert not list(
            (webui_settings.webui_data_dir / "board_settings").rglob("*.json")
        )

    def test_points_appear_once_the_machine_setting_is_enabled(
        self, selected_client: TestClient
    ):
        enabled = selected_client.put(
            "/api/settings/machine",
            json={
                "values": {
                    "paste_dispenser.flow_calibration.calibration_file": "cal.json"
                }
            },
        )
        assert enabled.status_code == 200, enabled.text
        saved = selected_client.patch(
            "/api/pasting/pad-config/flow-calibration",
            json={"point": [10.0, 4.0]},
        )
        assert saved.status_code == 200, saved.text

        flow = saved.json()["flow_calibration"]

        assert flow["enabled"] is True
        assert flow["points"] is not None
        assert len(flow["points"]) == flow["point_count"]
        assert flow["points"][0] == pytest.approx([10.0, 4.0])

    def test_zero_points_disables_measurement_while_keeping_the_point(
        self, selected_client: TestClient
    ):
        configured = selected_client.put(
            "/api/settings/machine",
            json={
                "values": {
                    "paste_dispenser.flow_calibration.calibration_file": "cal.json",
                    "paste_dispenser.flow_calibration.point_count": 0,
                }
            },
        )
        assert configured.status_code == 200, configured.text

        response = selected_client.patch(
            "/api/pasting/pad-config/flow-calibration",
            json={"point": [10.0, 4.0]},
        )

        assert response.status_code == 200, response.text
        flow = response.json()["flow_calibration"]
        assert flow["enabled"] is False
        assert flow["point"] == pytest.approx([10.0, 4.0])
        assert flow["points"] is None
        assert "測定点数 0" in flow["selection_label"]

    def test_a_row_running_off_the_board_is_reported_without_failing(
        self, selected_client: TestClient
    ):
        """起点は基板内でも並びがはみ出すことはある。保存は通し、理由を載せる."""
        configured = selected_client.put(
            "/api/settings/machine",
            json={
                "values": {
                    "paste_dispenser.flow_calibration.calibration_file": "cal.json",
                    "paste_dispenser.flow_calibration.point_pitch_mm": 200.0,
                }
            },
        )
        assert configured.status_code == 200, configured.text

        response = selected_client.patch(
            "/api/pasting/pad-config/flow-calibration",
            json={"point": [10.0, 4.0]},
        )

        assert response.status_code == 200, response.text
        flow = response.json()["flow_calibration"]
        assert flow["points"] is None
        assert flow["error"] is not None
        assert "基板外形" in flow["error"]
        # 画面の本文は selection_label だけなので、理由はそこにも出す
        assert "基板外形" in flow["selection_label"]

    def test_a_pitch_narrower_than_the_crop_is_reported_without_failing(
        self, selected_client: TestClient
    ):
        """Crop へ隣のドットが写り込む設定。machine.toml は読めたまま理由を出す."""
        configured = selected_client.put(
            "/api/settings/machine",
            json={
                "values": {
                    "paste_dispenser.flow_calibration.calibration_file": "cal.json",
                    "paste_dispenser.flow_calibration.crop_size_mm": 4.0,
                }
            },
        )
        assert configured.status_code == 200, configured.text
        # 片方だけ書いても machine.toml は壊れない（pad-config が引けること）
        assert selected_client.get("/api/pasting/pad-config").status_code == 200

        response = selected_client.patch(
            "/api/pasting/pad-config/flow-calibration",
            json={"point": [10.0, 4.0]},
        )

        assert response.status_code == 200, response.text
        flow = response.json()["flow_calibration"]
        assert flow["points"] is None
        assert flow["error"] is not None
        assert "crop" in flow["error"]
        assert "crop" in flow["selection_label"]


class TestPadConfigCopper:
    """GET pad-config/copper は表示用に簡略化した銅箔島を返す."""

    def test_returns_islands_for_the_selected_board(self, selected_client: TestClient):
        response = selected_client.get("/api/pasting/pad-config/copper")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["pcb_file"] == _get_config(selected_client)["pcb_file"]
        assert body["tolerance_mm"] == pytest.approx(0.02)
        assert body["islands"]
        island = body["islands"][0]
        assert island["layer"] in {"Top", "Bottom"}
        # 各環は閉環（末尾が始点の重複）
        for ring in island["rings"]:
            assert len(ring) >= 4
            assert ring[0] == ring[-1]

    def test_islands_stay_inside_the_board_outline(self, selected_client: TestClient):
        config = _get_config(selected_client)
        body = selected_client.get("/api/pasting/pad-config/copper").json()

        xs = [
            point[0]
            for island in body["islands"]
            for ring in island["rings"]
            for point in ring
        ]
        ys = [
            point[1]
            for island in body["islands"]
            for ring in island["rings"]
            for point in ring
        ]

        assert min(xs) >= -0.5
        assert min(ys) >= -0.5
        assert max(xs) <= config["width"] + 0.5
        assert max(ys) <= config["height"] + 0.5

    def test_requires_no_control_lease(self, selected_client: TestClient):
        # 装置を動かさない読み取りなので操作権は要らない
        assert selected_client.get("/api/pasting/pad-config/copper").status_code == 200

    def test_without_selected_pcb_returns_409(self, client: TestClient):
        assert client.get("/api/pasting/pad-config/copper").status_code == 409


class TestTreeNodeResolution:
    """GET tree の各ノードの resolved / own_override / descendant_summary 契約.

    計画書 Phase B「サーバ段階」が契約。各ノード行に解決済み値・自ノードの 明示 override・子孫 override
    集計を載せ、JS が再計算せず表示できるようにする。
    """

    def test_root_resolved_matches_defaults_when_no_overrides(
        self, selected_client: TestClient
    ):
        config = _get_config(selected_client)
        root = config["tree"]

        assert root["resolved"] == config["defaults"]

    def test_node_without_override_has_empty_own_override(
        self, selected_client: TestClient
    ):
        config = _get_config(selected_client)
        root = config["tree"]

        assert root["own_override"]["enabled"] is None
        assert root["own_override"]["values"] == {}

    def test_node_without_override_resolves_to_inherited_values(
        self, selected_client: TestClient
    ):
        config = _get_config(selected_client)
        u1_node = _node_by_id(config["tree"], "L2:U1")
        defaults = config["defaults"]

        # override 無しなので L2:U1 は defaults をそのまま継承
        assert u1_node["resolved"] == defaults

    def test_node_resolved_reflects_own_value_override(
        self, selected_client: TestClient
    ):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"prime_extra_delay": 0.3}},
        )

        config = _get_config(selected_client)
        u1_node = _node_by_id(config["tree"], "L2:U1")

        assert u1_node["resolved"]["prime_extra_delay"] == 0.3
        # 他 field は継承のまま
        assert u1_node["resolved"]["bead_width_factor"] == 1.0

    def test_descendant_node_inherits_ancestor_override(
        self, selected_client: TestClient
    ):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"prime_extra_delay": 0.3}},
        )

        config = _get_config(selected_client)
        u1_node = _node_by_id(config["tree"], "L2:U1")
        l4_node = _node_by_id(u1_node, "L4:U1:1")

        assert l4_node["resolved"]["prime_extra_delay"] == 0.3
        # 子ノード自身は override を持たない
        assert l4_node["own_override"]["values"] == {}

    def test_own_override_records_set_value_keys(self, selected_client: TestClient):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={
                "node": "L2:U1",
                "values": {"prime_extra_delay": 0.3, "bead_width_factor": 0.8},
            },
        )

        config = _get_config(selected_client)
        u1_node = _node_by_id(config["tree"], "L2:U1")
        own = u1_node["own_override"]

        assert set(own["values"]) == {"prime_extra_delay", "bead_width_factor"}
        assert own["values"]["prime_extra_delay"] == 0.3
        assert own["values"]["bead_width_factor"] == 0.8
        assert own["enabled"] is None  # enabled は明示していない

    def test_own_override_records_explicit_enabled(self, selected_client: TestClient):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "enabled": False},
        )

        config = _get_config(selected_client)
        u1_node = _node_by_id(config["tree"], "L2:U1")
        own = u1_node["own_override"]

        assert own["enabled"] is False
        assert own["values"] == {}

    def test_own_summary_empty_without_overrides(self, selected_client: TestClient):
        config = _get_config(selected_client)
        root = config["tree"]

        assert root["own_summary"] == {"enabled": False, "fields": [], "count": 0}

    def test_own_summary_counts_enabled_and_fields_in_ui_order(
        self, selected_client: TestClient
    ):
        # paste_height → ul_per_mm2 の順で設定しても fields は UI 表示順で返る。
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"paste_height": 0.2}},
        )
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"ul_per_mm2": 0.5}},
        )
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "enabled": False},
        )

        config = _get_config(selected_client)
        own_summary = _node_by_id(config["tree"], "L2:U1")["own_summary"]

        assert own_summary["enabled"] is True
        # PASTE_PARAM_FIELDS の列順: ul_per_mm2 が paste_height に先行
        assert own_summary["fields"] == ["ul_per_mm2", "paste_height"]
        assert own_summary["count"] == 3

    def test_fields_metadata_drives_ui_columns(self, selected_client: TestClient):
        """Pad-config の fields は PASTE_PARAM_FIELDS と同順・同集合で、JS の唯一の出典."""
        fields = _get_config(selected_client)["fields"]

        assert [field["name"] for field in fields] == list(PASTE_PARAM_NAMES)
        assert {field["kind"] for field in fields} == {"choice", "number", "height"}
        by_name = {field["name"]: field for field in fields}
        assert by_name["dispense_mode"]["kind"] == "choice"
        assert [c["value"] for c in by_name["dispense_mode"]["choices"]] == [
            "auto",
            "dot",
            "line",
            "area",
        ]
        assert by_name["paste_height"] == {
            "name": "paste_height",
            "label": "塗布高さ",
            "kind": "height",
            "unit": "mm",
            "choices": [],
        }
        assert by_name["ul_per_mm2"]["unit"] == "μL/mm²"

    def test_descendant_summary_empty_without_descendant_overrides(
        self, selected_client: TestClient
    ):
        config = _get_config(selected_client)
        u1_node = _node_by_id(config["tree"], "L2:U1")
        summary = u1_node["descendant_summary"]

        assert summary["node_count"] == 0
        assert summary["count"] == 0
        assert summary["enabled_count"] == 0
        assert summary["fields"] == []

    def test_descendant_summary_counts_descendant_overrides(
        self, selected_client: TestClient
    ):
        # L4:U1:1 に value override、L4:U1:2 に enabled をそれぞれ置く。
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L4:U1:1", "values": {"prime_extra_delay": 0.3}},
        )
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L4:U1:2", "enabled": False},
        )

        config = _get_config(selected_client)
        u1_node = _node_by_id(config["tree"], "L2:U1")
        summary = u1_node["descendant_summary"]

        # 2 個の子孫ノードに override（自ノード L2:U1 は含めない）
        assert summary["node_count"] == 2
        assert summary["count"] == 2
        assert summary["enabled_count"] == 1
        assert summary["field_counts"]["prime_extra_delay"] == 1
        assert "prime_extra_delay" in summary["fields"]

    def test_descendant_summary_excludes_self(self, selected_client: TestClient):
        # 自ノードに override を置いても descendant_summary には数えない。
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"prime_extra_delay": 0.3}},
        )

        config = _get_config(selected_client)
        u1_node = _node_by_id(config["tree"], "L2:U1")

        assert u1_node["descendant_summary"]["node_count"] == 0
        # 自ノードの own_override にだけ反映される
        assert u1_node["own_override"]["values"]["prime_extra_delay"] == 0.3

    def test_root_summary_aggregates_all_overrides(self, selected_client: TestClient):
        # ルート L0 の descendant_summary は全ノードの override を集計する。
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"prime_extra_delay": 0.3}},
        )
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L4:U1:1", "enabled": False},
        )

        config = _get_config(selected_client)
        summary = config["tree"]["descendant_summary"]

        assert summary["node_count"] == 2
        assert summary["enabled_count"] == 1
        assert summary["field_counts"]["prime_extra_delay"] == 1


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
            json={"node": "L2:U1", "values": {"prime_extra_delay": 0.3}},
        )

        config = _get_config(selected_client)
        assert config["overrides"]["L2:U1"]["values"]["prime_extra_delay"] == 0.3
        assert _pad_by_id(config, "U1.1")["resolved"]["prime_extra_delay"] == 0.3
        # U1 以外には波及しない
        assert _pad_by_id(config, "R1.1")["resolved"]["prime_extra_delay"] == 0.0

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
            json={"node": "L2:U1", "values": {"prime_extra_delay": 0.3}},
        )

        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "clear": ["prime_extra_delay"]},
        )

        assert response.status_code == 200, response.text
        config = _get_config(selected_client)
        # override が空 + enabled 継承 → ノードは overrides から消える（疎）
        assert "L2:U1" not in config["overrides"]
        assert _pad_by_id(config, "U1.1")["resolved"]["prime_extra_delay"] == 0.0

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
            json={"node": "L0", "values": {"prime_extra_delay": 0.35}},
        )

        assert response.status_code == 200, response.text
        config = _get_config(selected_client)
        assert config["defaults"]["prime_extra_delay"] == 0.0
        assert config["overrides"]["L0"]["values"]["prime_extra_delay"] == 0.35
        assert all(
            pad["resolved"]["prime_extra_delay"] == 0.35 for pad in config["pads"]
        )

    def test_l0_clear_returns_to_machine_default(self, selected_client: TestClient):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L0", "values": {"prime_extra_delay": 0.35}},
        )

        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L0", "clear": ["prime_extra_delay"]},
        )

        assert response.status_code == 200, response.text
        config = _get_config(selected_client)
        assert "L0" not in config["overrides"]
        assert _pad_by_id(config, "U1.1")["resolved"]["prime_extra_delay"] == 0.0

    def test_unknown_value_key_returns_400(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"no_such_field": 1.0}},
        )
        assert response.status_code == 400

    def test_removed_fill_speed_value_returns_400(self, selected_client: TestClient):
        # fill_speed は pad override から廃止された（max_fill_speed は装置一律設定）。
        # PASTE_PARAM_NAMES から消えたため未知項目として 400 になる。
        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"fill_speed": 0.3}},
        )
        assert response.status_code == 400

    def test_removed_fill_speed_clear_returns_400(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "clear": ["fill_speed"]},
        )
        assert response.status_code == 400

    def test_unknown_dispense_mode_returns_400(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"dispense_mode": "spray"}},
        )
        assert response.status_code == 400

    def test_unknown_line_direction_returns_400(self, selected_client: TestClient):
        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"line_direction": "sideways"}},
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


class TestExportImport:
    """GET export / POST import."""

    def test_export_contains_version_signature_and_settings(
        self, selected_client: TestClient
    ):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"prime_extra_delay": 0.3}},
        )

        response = selected_client.get("/api/pasting/pad-config/export")

        assert response.status_code == 200, response.text
        assert "attachment" in response.headers["content-disposition"]
        doc = response.json()
        assert doc["version"] == 1
        assert doc["source_pcb"].endswith("led_blinker.kicad_pcb")
        assert isinstance(doc["board_signature"], str)
        level = next(
            item for item in doc["settings"]["levels"] if item["key"] == ["L2", "U1"]
        )
        assert level["override"]["prime_extra_delay"] == 0.3

    def test_import_restores_saved_override(self, selected_client: TestClient):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"prime_extra_delay": 0.3}},
        )
        doc = selected_client.get("/api/pasting/pad-config/export").json()
        # export 後に override を公開 API で消し、import が復元することを見る
        cleared = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "clear": ["prime_extra_delay"]},
        )
        assert cleared.status_code == 200, cleared.text
        assert "L2:U1" not in _get_config(selected_client)["overrides"]

        response = selected_client.post(
            "/api/pasting/pad-config/import", json={"document": doc}
        )

        assert response.status_code == 200, response.text
        config = response.json()
        assert config["overrides"]["L2:U1"]["values"]["prime_extra_delay"] == 0.3
        assert _pad_by_id(config, "U1.1")["resolved"]["prime_extra_delay"] == 0.3

    def test_import_rejects_wrong_signature(self, selected_client: TestClient):
        doc = selected_client.get("/api/pasting/pad-config/export").json()
        doc["board_signature"] = "wrong"

        response = selected_client.post(
            "/api/pasting/pad-config/import", json={"document": doc}
        )

        assert response.status_code == 400


class TestPadConfigRoute:
    """POST /api/pasting/pad-config/route（選択基板の有効 pad を要求 layer で順路化）."""

    def _post_route(self, client: TestClient, layer: str) -> dict:
        response = client.post("/api/pasting/pad-config/route", json={"layer": layer})
        assert response.status_code == 200, response.text
        return response.json()

    def test_route_excludes_disabled_pad(self, selected_client: TestClient):
        config = _get_config(selected_client)
        target = next(
            pad for pad in config["pads"] if pad["layer"] == "Top" and pad["enabled"]
        )

        patch = selected_client.patch(
            "/api/pasting/pad-config/pads",
            json={"ids": [target["id"]], "enabled": False},
        )
        assert patch.status_code == 200, patch.text

        route = self._post_route(selected_client, "Top")
        routed_ids = {pad["id"] for pad in route["pads"]}
        expected_ids = {
            pad["id"]
            for pad in config["pads"]
            if pad["layer"] == "Top" and pad["enabled"] and pad["id"] != target["id"]
        }

        assert target["id"] not in routed_ids
        assert routed_ids == expected_ids

    def test_route_includes_only_requested_layer(self, selected_client: TestClient):
        config = _get_config(selected_client)
        expected_ids = {
            pad["id"]
            for pad in config["pads"]
            if pad["layer"] == "Bottom" and pad["enabled"]
        }

        route = self._post_route(selected_client, "Bottom")

        assert route["layer"] == "Bottom"
        assert {pad["id"] for pad in route["pads"]} == expected_ids
        assert [pad["order"] for pad in route["pads"]] == list(
            range(1, len(route["pads"]) + 1)
        )
        assert all(len(pad["center"]) == 2 for pad in route["pads"])

    def test_route_without_selected_pcb_returns_409(self, client: TestClient):
        response = client.post("/api/pasting/pad-config/route", json={"layer": "Top"})

        assert response.status_code == 409


class TestPadConfigFillPath:
    """POST /api/pasting/pad-config/fill-path（解決済み設定で有効 pad の塗布パス）."""

    def _post_fill_path(self, client: TestClient, layer: str) -> dict:
        response = client.post(
            "/api/pasting/pad-config/fill-path", json={"layer": layer}
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_fill_path_includes_only_enabled_requested_layer(
        self, selected_client: TestClient
    ):
        config = _get_config(selected_client)
        expected_ids = {
            pad["id"]
            for pad in config["pads"]
            if pad["layer"] == "Top" and pad["enabled"]
        }

        fill_path = self._post_fill_path(selected_client, "Top")

        assert fill_path["layer"] == "Top"
        assert fill_path["nozzle_diameter"] == pytest.approx(0.34)
        assert {pad["id"] for pad in fill_path["pads"]} == expected_ids
        assert fill_path["pads"]
        for pad in fill_path["pads"]:
            assert pad["dispense_mode"] in {"dot", "line", "area"}
            assert pad["path_count"] == len(pad["paths"])
            assert pad["point_count"] == sum(len(path) for path in pad["paths"])
            assert all(len(point) == 2 for path in pad["paths"] for point in path)

    def test_fill_path_excludes_disabled_pad(self, selected_client: TestClient):
        config = _get_config(selected_client)
        target = next(
            pad for pad in config["pads"] if pad["layer"] == "Top" and pad["enabled"]
        )

        patch = selected_client.patch(
            "/api/pasting/pad-config/pads",
            json={"ids": [target["id"]], "enabled": False},
        )
        assert patch.status_code == 200, patch.text

        fill_path = self._post_fill_path(selected_client, "Top")

        assert target["id"] not in {pad["id"] for pad in fill_path["pads"]}

    def test_fill_path_uses_resolved_overrides(self, selected_client: TestClient):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L0", "values": {"dispense_mode": "area"}},
        )
        before = self._post_fill_path(selected_client, "Top")

        patch = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L0", "values": {"boundary_margin": 0.3}},
        )
        assert patch.status_code == 200, patch.text

        after = self._post_fill_path(selected_client, "Top")

        assert after["pads"] != before["pads"]

    def test_fill_path_uses_resolved_dispense_mode(self, selected_client: TestClient):
        patch = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L0", "values": {"dispense_mode": "dot"}},
        )
        assert patch.status_code == 200, patch.text

        fill_path = self._post_fill_path(selected_client, "Top")

        assert fill_path["pads"]
        assert {pad["dispense_mode"] for pad in fill_path["pads"]} == {"dot"}
        assert all(pad["point_count"] == pad["path_count"] for pad in fill_path["pads"])

    def test_fill_path_reverses_lines_between_outward_and_inward(
        self, selected_client: TestClient
    ):
        outward_patch = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={
                "node": "L2:U1",
                "values": {"dispense_mode": "line", "line_direction": "outward"},
            },
        )
        assert outward_patch.status_code == 200, outward_patch.text
        outward = self._post_fill_path(selected_client, "Top")

        inward_patch = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L2:U1", "values": {"line_direction": "inward"}},
        )
        assert inward_patch.status_code == 200, inward_patch.text
        inward = self._post_fill_path(selected_client, "Top")

        outward_lines = {
            pad["id"]: pad["paths"][0]
            for pad in outward["pads"]
            if pad["id"].startswith("U1.") and pad["dispense_mode"] == "line"
        }
        inward_lines = {
            pad["id"]: pad["paths"][0]
            for pad in inward["pads"]
            if pad["id"] in outward_lines
        }
        assert outward_lines
        assert inward_lines.keys() == outward_lines.keys()
        for pad_id, path in outward_lines.items():
            assert inward_lines[pad_id] == list(reversed(path))

    def test_fill_path_without_selected_pcb_returns_409(self, client: TestClient):
        response = client.post(
            "/api/pasting/pad-config/fill-path", json={"layer": "Top"}
        )

        assert response.status_code == 409


class TestExpectedPcb:
    """expected_pcb による PCB 切替の検出（MR1: 409 / None は無検査）."""

    def test_matching_expected_pcb_is_accepted(self, selected_client: TestClient):
        pcb_file = _get_config(selected_client)["pcb_file"]

        response = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L0", "enabled": False, "expected_pcb": pcb_file},
        )

        assert response.status_code == 200, response.text

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/api/pasting/pad-config/node", {"node": "L0", "enabled": False}),
            ("/api/pasting/pad-config/pads", {"ids": ["U1.1"], "enabled": False}),
            ("/api/pasting/pad-config/initial-purge", {"initial_purge_ul": 1.0}),
        ],
    )
    def test_other_pcb_is_rejected_with_409(
        self, selected_client: TestClient, path: str, body: dict
    ):
        response = selected_client.patch(
            path, json={**body, "expected_pcb": "boards/other.kicad_pcb"}
        )

        assert response.status_code == 409, response.text

    def test_rejected_patch_does_not_persist(
        self, selected_client: TestClient, webui_settings: Settings
    ):
        # 先に成功する PATCH を通して JSON を作る（「一度も書かれていない」
        # 状態と「409 で書かれなかった」状態を区別するため）
        accepted = selected_client.patch(
            "/api/pasting/pad-config/pads",
            json={"ids": ["U1.2"], "enabled": False},
        )
        assert accepted.status_code == 200, accepted.text
        before = _saved_board_settings_doc(webui_settings)

        selected_client.patch(
            "/api/pasting/pad-config/pads",
            json={
                "ids": ["U1.1"],
                "enabled": False,
                "expected_pcb": "boards/other.kicad_pcb",
            },
        )

        assert _saved_board_settings_doc(webui_settings) == before
        assert _pad_by_id(_get_config(selected_client), "U1.1")["enabled"] is True

    def test_rejected_initial_purge_does_not_touch_machine_toml(
        self, selected_client: TestClient, config_dir: Path
    ):
        """machine.toml は基板横断のグローバル設定なので 409 で不変であること."""
        machine_toml = config_dir / "machine.toml"
        before = machine_toml.read_text(encoding="utf-8")
        # fixture は未設定（解決値 0.1 は PasteDispenser の既定）
        assert "initial_purge_ul" not in before

        response = selected_client.patch(
            "/api/pasting/pad-config/initial-purge",
            json={
                "initial_purge_ul": 0.42,
                "expected_pcb": "boards/other.kicad_pcb",
            },
        )

        assert response.status_code == 409, response.text
        assert machine_toml.read_text(encoding="utf-8") == before
        initial = _get_config(selected_client)["initial_purge"]
        assert initial["initial_purge_ul"] == pytest.approx(0.1)
