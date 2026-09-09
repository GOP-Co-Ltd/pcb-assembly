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
_FILESYSTEM_UNSAFE_FOOTPRINT_IDS = [
    pytest.param(f"{'l' * 249}.pretty/Part", id="library-over-name-max"),
    pytest.param(
        f"Test.pretty/{'p' * 246}",
        id="footprint-with-suffix-over-name-max",
    ),
    pytest.param("Test.pretty/Bad\x00Name", id="nul-in-footprint-name"),
]


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

    def test_duplicate_addition_is_a_successful_no_op(self, client: TestClient):
        first_response = client.post(
            f"{_BASE}/patterns/from-footprint",
            json={"config": _default_config(client), "footprint_id": _QFN},
        )
        assert first_response.status_code == 200, first_response.text
        first = first_response.json()

        response = client.post(
            f"{_BASE}/patterns/from-footprint",
            json={"config": first["config"], "footprint_id": _QFN},
        )

        assert response.status_code == 200
        assert response.json() == {**first, "added_count": 0}

    def test_addition_recovers_a_config_with_no_patterns(self, client: TestClient):
        config = _default_config(client)
        config["patterns"] = []

        response = client.post(
            f"{_BASE}/patterns/from-footprint",
            json={"config": config, "footprint_id": _QFN},
        )

        assert response.status_code == 200, response.text
        assert response.json()["added_count"] == 3

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

    @pytest.mark.parametrize("footprint_id", _FILESYSTEM_UNSAFE_FOOTPRINT_IDS)
    def test_filesystem_unsafe_footprint_id_is_a_domain_400(
        self,
        client: TestClient,
        footprint_id: str,
    ):
        response = client.post(
            f"{_BASE}/patterns/from-footprint",
            json={
                "config": _default_config(client),
                "footprint_id": footprint_id,
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

    def test_circle_request_needs_only_the_displayed_diameter(self, client: TestClient):
        response = client.post(
            f"{_BASE}/custom-pads",
            json={
                "config": _default_config(client),
                "custom_pad": {"shape": "circle", "width_mm": 0.75},
            },
        )

        assert response.status_code == 200, response.text
        custom_pad = response.json()["config"]["custom_pads"][0]
        assert custom_pad["height_mm"] == 0.75
        assert custom_pad["corner_radius_mm"] == 0.0
        assert custom_pad["name"] == "円 φ0.75 mm"

    def test_custom_pad_recovers_a_config_with_no_patterns(self, client: TestClient):
        config = _default_config(client)
        config["patterns"] = []

        response = client.post(
            f"{_BASE}/custom-pads",
            json={
                "config": config,
                "custom_pad": {"shape": "circle", "width_mm": 0.75},
            },
        )

        assert response.status_code == 200, response.text
        assert len(response.json()["config"]["patterns"]) == 1

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

    @pytest.mark.parametrize(
        "width_mm",
        [
            pytest.param(3_000.0, id="outside-signed-32-bit-nm"),
            pytest.param(0.000_000_6, id="below-one-nm"),
        ],
    )
    def test_kicad_unrepresentable_custom_pad_width_is_a_domain_400(
        self,
        client: TestClient,
        width_mm: float,
    ):
        response = client.post(
            f"{_BASE}/custom-pads",
            json={
                "config": _default_config(client),
                "custom_pad": {
                    "shape": "rectangle",
                    "width_mm": width_mm,
                    "height_mm": 1.0,
                },
            },
        )

        assert response.status_code == 400


class TestPasteTestBoardPreview:
    def test_returns_resolved_config_catalog_and_layout(self, client: TestClient):
        response = client.post(f"{_BASE}/preview", json=_default_config(client))

        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["pad_count"] == 64
        assert len(preview["patterns"]) == 6
        assert len(preview["pads"]) == 64
        assert preview["overflow_message"] is None
        assert preview["placement_area"] == {
            "x": 1.0,
            "y": 1.0,
            "width": 38.0,
            "height": 38.0,
        }
        assert preview["preview_bounds"] == {
            "x": 0.0,
            "y": 0.0,
            "width": 40.0,
            "height": 40.0,
        }
        assert [item["catalog_id"] for item in preview["catalog"]] == [
            item["catalog_id"] for item in preview["config"]["patterns"]
        ]
        assert {polygon["layer"] for polygon in preview["pads"][0]["polygons"]} == {
            "F.Cu",
            "F.Paste",
        }
        assert preview["pads"][0]["display_name"].startswith("R_0402_1005Metric / ")
        assert len(preview["flow_pads"]) == 5
        # 1 パッドが F.Cu / F.Paste の 2 polygon で 1 グループになる
        assert [
            [polygon["layer"] for polygon in group]
            for group in preview["flow_polygons"]
        ] == [["F.Cu", "F.Paste"]] * 5

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

    @pytest.mark.parametrize(
        "width_mm",
        [
            pytest.param(3_000.0, id="outside-signed-32-bit-nm"),
            pytest.param(0.000_000_6, id="below-one-nm"),
        ],
    )
    def test_kicad_unrepresentable_board_width_is_a_domain_400(
        self,
        client: TestClient,
        width_mm: float,
    ):
        config = _default_config(client)
        config["board"]["width_mm"] = width_mm
        config["board"]["edge_margin_mm"] = 0.0

        response = client.post(f"{_BASE}/preview", json=config)

        assert response.status_code == 400

    @pytest.mark.parametrize("footprint_id", _FILESYSTEM_UNSAFE_FOOTPRINT_IDS)
    def test_filesystem_unsafe_catalog_id_is_a_domain_400(
        self,
        client: TestClient,
        footprint_id: str,
    ):
        config = _default_config(client)
        config["patterns"][0]["catalog_id"] = f"{footprint_id}#pad-0"

        response = client.post(f"{_BASE}/preview", json=config)

        assert response.status_code == 400

    def test_resource_exhausting_pad_count_is_400(self, client: TestClient):
        config = _default_config(client)
        config["patterns"][0]["rotation_count"] = 10**400

        response = client.post(f"{_BASE}/preview", json=config)

        assert response.status_code == 400
        assert "10,000" in response.text

    def test_layout_overflow_returns_diagnostic_geometry(self, client: TestClient):
        config = _default_config(client)
        config["board"]["width_mm"] = 10.0

        response = client.post(f"{_BASE}/preview", json=config)

        assert response.status_code == 200, response.text
        preview = response.json()
        assert "収まりません" in preview["overflow_message"]
        assert preview["pad_count"] == 64
        area = preview["placement_area"]
        right = area["x"] + area["width"]
        bottom = area["y"] + area["height"]
        assert any(
            pad["bounds"]["x"] + pad["bounds"]["width"] > right + 1e-9
            or pad["bounds"]["y"] + pad["bounds"]["height"] > bottom + 1e-9
            for pad in preview["pads"]
        )

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

    @pytest.mark.parametrize(
        "document",
        [
            {"kind": "calibration_board", "schema_version": 1},
            {"kind": "paste_test_board", "schema_version": 2},
            {
                "kind": "paste_test_board",
                "schema_version": 1,
                "unexpected": True,
            },
            {},
        ],
    )
    def test_import_rejects_wrong_or_malformed_documents(
        self, client: TestClient, document: dict[str, object]
    ):
        response = client.post(f"{_BASE}/import", json={"document": document})

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
        output = tmp_path / "download.kicad_pcb"
        output.write_bytes(response.content)
        pcb = PcbFile(output)
        assert pcb.outline.width == 40.0
        # パッド種 64 + PURGE 1 + FLOW1..5
        assert len(pcb.components) == 70
        assert len(pcb.pads) == 70
        designators = {pad.designator for pad in pcb.pads}
        assert "PURGE" in designators
        assert {f"FLOW{index}" for index in range(1, 6)} <= designators

    def test_layout_overflow_is_rejected(self, client: TestClient):
        config = _default_config(client)
        config["board"]["width_mm"] = 10.0

        response = client.post(f"{_BASE}/generate", json=config)

        assert response.status_code == 422
        assert "収まりません" in response.text
