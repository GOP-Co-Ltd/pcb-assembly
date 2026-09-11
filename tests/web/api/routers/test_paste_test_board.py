"""テスト塗布基板APIの結合テスト."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pcbasm.pcb import PcbFile
from tests.helpers import FakeAudioPlayer
from web.api.app import create_app
from web.api.settings import Settings

_BASE = "/api/pasting/paste-test-board"
_R0402 = "Resistor_SMD.pretty/R_0402_1005Metric#pad-0"
_R0603 = "Resistor_SMD.pretty/R_0603_1608Metric#pad-0"
_QFN = "Package_DFN_QFN.pretty/QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm"


def _default_config(client: TestClient) -> dict[str, Any]:
    response = client.get(f"{_BASE}/options")
    assert response.status_code == 200, response.text
    return response.json()["config"]


def _document(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "paste_test_board",
        "schema_version": 1,
        **config,
    }


class TestPasteTestBoardOptions:
    def test_returns_schema_one_defaults_from_the_injected_library(
        self, client: TestClient
    ):
        response = client.get(f"{_BASE}/options")

        assert response.status_code == 200
        body = response.json()
        assert body["kind"] == "paste_test_board"
        assert body["schema_version"] == 1
        assert body["footprint_count"] == 8
        assert body["config"]["custom_pads"] == []
        assert [item["shape"] for item in body["custom_pad_shapes"]] == [
            "circle",
            "rectangle",
            "roundrect",
            "oval",
        ]
        assert [item["footprint_label"] for item in body["catalog"]] == [
            "R_0402_1005Metric",
            "R_0603_1608Metric",
            "R_0805_2012Metric",
            "R_1206_3216Metric",
            "SOT-23",
            "SOT-23-5",
        ]
        assert len(body["config"]["patterns"]) == 6

    def test_searches_only_the_injected_footprint_inventory(self, client: TestClient):
        response = client.get(f"{_BASE}/footprints?query=QFN%203x3&limit=10")

        assert response.status_code == 200
        body = response.json()
        assert body["footprint_count"] == 8
        assert [item["footprint_id"] for item in body["results"]] == [_QFN]

    @pytest.mark.parametrize("limit", [0, 101])
    def test_search_limit_outside_the_http_contract_is_422(
        self, client: TestClient, limit: int
    ):
        response = client.get(f"{_BASE}/footprints?query=&limit={limit}")

        assert response.status_code == 422


class TestPasteTestBoardPatternAddition:
    def test_adds_all_pad_patterns_from_one_footprint(self, client: TestClient):
        response = client.post(
            f"{_BASE}/patterns/from-footprint",
            json={"config": _default_config(client), "footprint_id": _QFN},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["added_count"] == 3
        assert [item["source_pad_count"] for item in body["catalog"][-3:]] == [
            4,
            16,
            1,
        ]
        assert [item["catalog_id"] for item in body["catalog"]] == [
            item["catalog_id"] for item in body["config"]["patterns"]
        ]

    @pytest.mark.parametrize("footprint_id", ["", "x" * 301, True])
    def test_invalid_footprint_id_is_422(
        self, client: TestClient, footprint_id: object
    ):
        response = client.post(
            f"{_BASE}/patterns/from-footprint",
            json={
                "config": _default_config(client),
                "footprint_id": footprint_id,
            },
        )

        assert response.status_code == 422

    def test_unknown_footprint_id_is_a_domain_400(self, client: TestClient):
        response = client.post(
            f"{_BASE}/patterns/from-footprint",
            json={
                "config": _default_config(client),
                "footprint_id": "not-a-footprint-id",
            },
        )

        assert response.status_code == 400


class TestPasteTestBoardCustomPad:
    def test_adds_a_custom_roundrect_pad_with_a_default_name(self, client: TestClient):
        response = client.post(
            f"{_BASE}/custom-pads",
            json={
                "config": _default_config(client),
                "custom_pad": {
                    "shape": "roundrect",
                    "width_mm": 1.2,
                    "height_mm": 0.8,
                    "corner_radius_mm": 0.2,
                    "name": "",
                },
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["config"]["custom_pads"]) == 1
        assert body["config"]["custom_pads"][0]["name"] == (
            "角丸矩形 1.2 × 0.8 mm R0.2 mm"
        )
        assert body["catalog"][-1]["label"].startswith("角丸矩形")

    def test_invalid_shape_is_422(self, client: TestClient):
        response = client.post(
            f"{_BASE}/custom-pads",
            json={
                "config": _default_config(client),
                "custom_pad": {
                    "shape": "triangle",
                    "width_mm": 1.0,
                    "height_mm": 1.0,
                    "corner_radius_mm": 0.0,
                    "name": "",
                },
            },
        )

        assert response.status_code == 422

    def test_invalid_roundrect_geometry_is_a_domain_400(self, client: TestClient):
        response = client.post(
            f"{_BASE}/custom-pads",
            json={
                "config": _default_config(client),
                "custom_pad": {
                    "shape": "roundrect",
                    "width_mm": 1.0,
                    "height_mm": 0.5,
                    "corner_radius_mm": 0.3,
                    "name": "",
                },
            },
        )

        assert response.status_code == 400


class TestPasteTestBoardPreview:
    def test_returns_resolved_config_catalog_and_layout(self, client: TestClient):
        """JS が描画に使うキーが 1 往復で揃う（幾何の中身は core が担保）."""
        response = client.post(f"{_BASE}/preview", json=_default_config(client))

        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["pad_count"] == len(preview["pads"])
        assert preview["overflow_message"] is None
        assert set(preview["placement_area"]) == {"x", "y", "width", "height"}
        assert set(preview["preview_bounds"]) == {"x", "y", "width", "height"}
        # catalog は config の pattern と同じ並びで返る（JS が対応付けしない）
        assert [item["catalog_id"] for item in preview["catalog"]] == [
            item["catalog_id"] for item in preview["config"]["patterns"]
        ]
        assert preview["pads"][0]["display_name"]
        assert preview["flow_pads"]
        assert preview["flow_polygons"]

    @pytest.mark.parametrize(
        ("target", "field", "value"),
        [
            ("board", "width_mm", True),
            ("board", "height_mm", "40"),
            ("board", "width_mm", 10**400),
            ("pattern", "rotation_count", True),
        ],
    )
    def test_non_strict_input_is_422(
        self,
        client: TestClient,
        target: str,
        field: str,
        value: object,
    ):
        config = _default_config(client)
        if target == "config":
            config[field] = value
        elif target == "board":
            config["board"][field] = value
        else:
            config["patterns"][0][field] = value

        response = client.post(f"{_BASE}/preview", json=config)

        assert response.status_code == 422

    def test_domain_error_is_400(self, client: TestClient):
        config = _default_config(client)
        config["patterns"][0]["rotation_span_deg"] = 0.0

        response = client.post(f"{_BASE}/preview", json=config)

        assert response.status_code == 400
        assert "回転範囲" in response.text

    def test_resource_exhausting_pad_count_is_400(self, client: TestClient):
        config = _default_config(client)
        config["patterns"][0]["rotation_count"] = 10**400

        response = client.post(f"{_BASE}/preview", json=config)

        assert response.status_code == 400
        assert "10,000" in response.text

    def test_layout_overflow_is_reported_without_failing(self, client: TestClient):
        """溢れても preview は 200 を返し、overflow_message で診断する."""
        config = _default_config(client)
        config["board"]["width_mm"] = 10.0

        response = client.post(f"{_BASE}/preview", json=config)

        assert response.status_code == 200, response.text
        assert "収まりません" in response.json()["overflow_message"]

    def test_missing_footprint_library_is_503(
        self,
        webui_settings: Settings,
        audio_player: FakeAudioPlayer,
        tmp_path: Path,
    ):
        app = create_app(
            webui_settings,
            audio_player=audio_player,
            paste_test_board_footprint_root=tmp_path / "missing",
        )
        with TestClient(app) as isolated_client:
            response = isolated_client.get(f"{_BASE}/options")

        assert response.status_code == 503
        assert "KiCad footprint" in response.text


class TestPasteTestBoardConfigTransfer:
    def test_export_allows_structurally_valid_overflow(self, client: TestClient):
        config = _default_config(client)
        config["board"]["width_mm"] = 10.0

        response = client.post(f"{_BASE}/export", json=config)

        assert response.status_code == 200
        assert response.headers["content-disposition"] == (
            'attachment; filename="pcbasm-paste-test-board.json"'
        )
        assert response.json()["schema_version"] == 1
        assert response.json()["board"]["width_mm"] == 10.0

    def test_import_validates_schema_one_and_normalizes_pattern_order(
        self, client: TestClient
    ):
        config = _default_config(client)
        document = _document(
            {**config, "patterns": list(reversed(config["patterns"][:2]))}
        )

        response = client.post(f"{_BASE}/import", json={"document": document})

        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["catalog_id"] for item in body["config"]["patterns"]] == [
            _R0402,
            _R0603,
        ]
        assert [item["catalog_id"] for item in body["catalog"]] == [
            _R0402,
            _R0603,
        ]

    def test_import_rejects_a_wrong_document(self, client: TestClient):
        """文書 identity / schema 版 / 余剰キーの判定は core が担う。ここは 400 写像だけ."""
        response = client.post(
            f"{_BASE}/import",
            json={"document": {"kind": "calibration_board", "schema_version": 1}},
        )

        assert response.status_code == 400

    @pytest.mark.parametrize("document", [None, [], "not-an-object"])
    def test_import_wrapper_is_strict(self, client: TestClient, document: object):
        response = client.post(f"{_BASE}/import", json={"document": document})

        assert response.status_code == 422


class TestPasteTestBoardGenerate:
    def test_downloads_a_real_kicad_board_without_control(
        self, client: TestClient, tmp_path: Path
    ):
        response = client.post(f"{_BASE}/generate", json=_default_config(client))

        assert response.status_code == 200, response.text
        assert response.headers["content-disposition"] == (
            'attachment; filename="pcbasm-paste-test-board.kicad_pcb"'
        )
        # 配信したバイト列が KiCad の基板として実際に開ける（中身は core が担保）
        output = tmp_path / "download.kicad_pcb"
        output.write_bytes(response.content)
        assert PcbFile(output).pads

    def test_layout_overflow_is_rejected(self, client: TestClient):
        config = _default_config(client)
        config["board"]["width_mm"] = 10.0

        response = client.post(f"{_BASE}/generate", json=config)

        assert response.status_code == 422
        assert "収まりません" in response.text
