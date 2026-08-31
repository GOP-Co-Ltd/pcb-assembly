"""はんだペースト流量キャリブレーション基板APIの結合テスト."""

from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi.testclient import TestClient

from pcbasm.pcb import PcbFile
from tests.helpers import FakeAudioPlayer
from web.api.app import create_app
from web.api.settings import Settings

_BASE = "/api/pasting/paste-flow-calibration-board"
_R0402 = "Resistor_SMD.pretty/R_0402_1005Metric#pad-0"
_R0603 = "Resistor_SMD.pretty/R_0603_1608Metric#pad-0"
_QFN = "Package_DFN_QFN.pretty/QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm"


def _default_config(client: TestClient) -> dict[str, Any]:
    return client.get(f"{_BASE}/options").json()["config"]


class TestPasteFlowCalibrationBoardOptions:
    def test_returns_six_default_pad_patterns_and_library_size(
        self, client: TestClient
    ):
        response = client.get(f"{_BASE}/options")

        assert response.status_code == 200
        body = response.json()
        assert body["kind"] == "paste_flow_calibration_board"
        assert body["schema_version"] == 3
        assert body["config"]["auto_pack"] is True
        assert body["config"]["custom_pads"] == []
        assert [item["shape"] for item in body["custom_pad_shapes"]] == [
            "circle",
            "rectangle",
            "roundrect",
            "oval",
        ]
        assert body["footprint_count"] > 10_000
        assert len(body["catalog"]) == 6
        assert [item["footprint_label"] for item in body["catalog"]] == [
            "R_0402_1005Metric",
            "R_0603_1608Metric",
            "R_0805_2012Metric",
            "R_1206_3216Metric",
            "SOT-23",
            "SOT-23-5",
        ]
        assert body["catalog"][0]["source_pad_count"] == 2
        assert body["catalog"][0]["default_transpose"] is False
        assert all(
            item["default_rotation_span_deg"] == 180.0 for item in body["catalog"]
        )
        assert len(body["config"]["patterns"]) == 6
        assert all(
            pattern["rotation_span_deg"] == 180.0
            for pattern in body["config"]["patterns"]
        )
        assert all(not pattern["transpose"] for pattern in body["config"]["patterns"])

    def test_searches_installed_footprints(self, client: TestClient):
        response = client.get(
            f"{_BASE}/footprints?query="
            "QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm&limit=10"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["footprint_count"] > 10_000
        assert any(item["footprint_id"] == _QFN for item in body["results"])

    def test_lists_common_footprints_without_a_query(self, client: TestClient):
        response = client.get(f"{_BASE}/footprints?query=&limit=100")

        assert response.status_code == 200
        body = response.json()
        assert len(body["results"]) == 69
        assert body["results"][0]["footprint_id"] == (
            "Resistor_SMD.pretty/R_0201_0603Metric"
        )
        assert any(item["footprint_id"] == _QFN for item in body["results"])

    def test_returns_each_distinct_pad_pattern_for_a_footprint(
        self, client: TestClient
    ):
        response = client.get(
            f"{_BASE}/pad-patterns?footprint_id={quote(_QFN, safe='')}"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["footprint_id"] == _QFN
        assert len(body["catalog"]) == 3
        assert [item["source_pad_count"] for item in body["catalog"]] == [4, 16, 1]
        assert all(
            item["default_rotation_span_deg"] == 180.0 for item in body["catalog"]
        )
        assert body["catalog"][0]["label"].startswith("Paste aperture")
        assert body["catalog"][1]["label"].startswith("Pad 1–16")

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
                },
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["config"]["custom_pads"]) == 1
        assert (
            body["config"]["custom_pads"][0]["name"] == "角丸矩形 1.2 × 0.8 mm R0.2 mm"
        )
        assert body["config"]["patterns"][-1]["transpose"] is False
        assert body["catalog"][-1]["footprint_label"] == "角丸矩形 1.2 × 0.8 mm R0.2 mm"
        assert body["catalog"][-1]["label"].startswith("角丸矩形")


class TestPasteFlowCalibrationBoardPreview:
    def test_resolves_single_pad_polygons_and_group_dimensions(
        self, client: TestClient
    ):
        response = client.post(f"{_BASE}/preview", json=_default_config(client))

        assert response.status_code == 200, response.text
        layout = response.json()
        assert layout["pad_count"] == 64
        assert layout["purge_pad"] == {
            "x": 1.0,
            "y": 1.0,
            "width": 2.0,
            "height": 2.0,
        }
        assert len(layout["groups"]) == 6
        assert layout["groups"][0]["angles_deg"] == [0.0, 45.0, 90.0, 135.0]
        assert layout["groups"][0]["transpose"] is False
        assert len(layout["groups"][0]["pads"]) == 12
        layers = {
            polygon["layer"] for polygon in layout["groups"][0]["pads"][0]["polygons"]
        }
        assert layers == {"F.Cu", "F.Paste"}

    def test_domain_error_is_400(self, client: TestClient):
        config = _default_config(client)
        config["patterns"][0]["rotation_span_deg"] = 0

        response = client.post(f"{_BASE}/preview", json=config)

        assert response.status_code == 400
        assert "回転範囲" in response.text

    def test_layout_overflow_is_422(self, client: TestClient):
        config = _default_config(client)
        config["board"]["width_mm"] = 10

        response = client.post(f"{_BASE}/preview", json=config)

        assert response.status_code == 422
        assert "超え" in response.text

    def test_missing_footprint_library_is_503(
        self,
        webui_settings: Settings,
        audio_player: FakeAudioPlayer,
        tmp_path: Path,
    ):
        app = create_app(
            webui_settings,
            audio_player=audio_player,
            paste_flow_calibration_footprint_root=tmp_path,
        )
        with TestClient(app) as isolated_client:
            response = isolated_client.get(f"{_BASE}/options")

        assert response.status_code == 503
        assert "KiCad footprint" in response.text


class TestPasteFlowCalibrationBoardConfigTransfer:
    def test_export_allows_structurally_valid_overflow(self, client: TestClient):
        config = _default_config(client)
        config["board"]["width_mm"] = 10

        response = client.post(f"{_BASE}/export", json=config)

        assert response.status_code == 200
        assert response.headers["content-disposition"] == (
            'attachment; filename="pcbasm-paste-flow-calibration-board.json"'
        )
        assert response.json()["kind"] == "paste_flow_calibration_board"
        assert response.json()["board"]["width_mm"] == 10
        assert response.json()["board"]["pad_gap_mm"] == 1.0
        assert response.json()["auto_pack"] is True
        assert response.json()["patterns"][0]["transpose"] is False

    def test_import_validates_identity_and_normalizes_pad_order(
        self, client: TestClient
    ):
        config = _default_config(client)
        document = {
            "kind": "paste_flow_calibration_board",
            "schema_version": 3,
            "auto_pack": config["auto_pack"],
            "board": config["board"],
            "purge_pad": config["purge_pad"],
            "custom_pads": config["custom_pads"],
            "patterns": list(reversed(config["patterns"][:2])),
        }

        response = client.post(f"{_BASE}/import", json={"document": document})

        assert response.status_code == 200
        body = response.json()
        assert [item["catalog_id"] for item in body["config"]["patterns"]] == [
            _R0402,
            _R0603,
        ]
        assert [item["catalog_id"] for item in body["catalog"]] == [_R0402, _R0603]

    def test_import_rejects_another_document_kind(self, client: TestClient):
        response = client.post(
            f"{_BASE}/import",
            json={
                "document": {
                    "kind": "calibration_board",
                    "schema_version": 3,
                    "auto_pack": True,
                    "board": {},
                    "purge_pad": {},
                    "custom_pads": [],
                    "patterns": [],
                }
            },
        )

        assert response.status_code == 400
        assert "はんだペースト流量" in response.text


class TestPasteFlowCalibrationBoardGenerate:
    def test_downloads_a_one_pad_per_footprint_board_without_control(
        self, client: TestClient, tmp_path: Path
    ):
        response = client.post(f"{_BASE}/generate", json=_default_config(client))

        assert response.status_code == 200, response.text
        assert response.headers["content-disposition"] == (
            'attachment; filename="pcbasm-paste-flow-calibration-board.kicad_pcb"'
        )
        output = tmp_path / "download.kicad_pcb"
        output.write_bytes(response.content)
        pcb = PcbFile(output)
        assert pcb.outline.width == 40.0
        assert len(pcb.components) == 65
        assert len(pcb.pads) == 65
