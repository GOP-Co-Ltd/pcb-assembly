"""はんだペースト流量キャリブレーション基板APIの結合テスト."""

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from pcbasm.pcb import PcbFile
from tests.helpers import FakeAudioPlayer
from web.api.app import create_app
from web.api.settings import Settings

_BASE = "/api/pasting/paste-flow-calibration-board"


def _default_config(client: TestClient) -> dict[str, Any]:
    return client.get(f"{_BASE}/options").json()["config"]


class TestPasteFlowCalibrationBoardOptions:
    def test_returns_fixed_catalog_and_six_pattern_recipe(self, client: TestClient):
        response = client.get(f"{_BASE}/options")

        assert response.status_code == 200
        body = response.json()
        assert body["kind"] == "paste_flow_calibration_board"
        assert body["schema_version"] == 1
        assert len(body["catalog"]) == 11
        assert [item["label"] for item in body["catalog"][:6]] == [
            "0402",
            "0603",
            "0805",
            "1206",
            "SOT-23",
            "SOT-23-5",
        ]
        assert len(body["config"]["patterns"]) == 6


class TestPasteFlowCalibrationBoardPreview:
    def test_resolves_real_pad_polygons_and_group_dimensions(self, client: TestClient):
        response = client.post(f"{_BASE}/preview", json=_default_config(client))

        assert response.status_code == 200, response.text
        layout = response.json()
        assert layout["component_count"] == 64
        assert layout["purge_pad"] == {
            "x": 1.0,
            "y": 1.0,
            "width": 2.0,
            "height": 2.0,
        }
        assert len(layout["groups"]) == 6
        assert layout["groups"][0]["angles_deg"] == [0.0, 45.0, 90.0, 135.0]
        layers = {
            polygon["layer"]
            for polygon in layout["groups"][0]["components"][0]["polygons"]
        }
        assert layers == {"F.Cu", "F.Paste"}

    def test_domain_error_is_400(self, client: TestClient):
        config = _default_config(client)
        config["patterns"][0]["rotation_span_deg"] = 0

        response = client.post(f"{_BASE}/preview", json=config)

        assert response.status_code == 400
        assert "theta" in response.text

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
        with TestClient(app) as client:
            response = client.post(f"{_BASE}/preview", json=_default_config(client))

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

    def test_import_validates_identity_and_normalizes_order(self, client: TestClient):
        config = _default_config(client)
        document = {
            "kind": "paste_flow_calibration_board",
            "schema_version": 1,
            "board": config["board"],
            "purge_pad": config["purge_pad"],
            "patterns": list(reversed(config["patterns"][:2])),
        }

        response = client.post(f"{_BASE}/import", json={"document": document})

        assert response.status_code == 200
        assert [item["catalog_id"] for item in response.json()["patterns"]] == [
            "r_0402_1005metric",
            "r_0603_1608metric",
        ]

    def test_import_rejects_another_document_kind(self, client: TestClient):
        response = client.post(
            f"{_BASE}/import",
            json={
                "document": {
                    "kind": "calibration_board",
                    "schema_version": 1,
                    "board": {},
                    "purge_pad": {},
                    "patterns": [],
                }
            },
        )

        assert response.status_code == 400
        assert "はんだペースト流量" in response.text


class TestPasteFlowCalibrationBoardGenerate:
    def test_downloads_a_round_trippable_kicad_board_without_control(
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
