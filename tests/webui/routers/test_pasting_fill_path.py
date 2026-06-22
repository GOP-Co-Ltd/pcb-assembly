"""POST /api/pasting/pad-config/fill-path contract tests."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webui.state import AppState


@pytest.fixture
def selected_client(
    client: TestClient, appstate: AppState, copper_pcb_path: Path
) -> TestClient:
    appstate.select_pcb(copper_pcb_path)
    return client


def _get_config(client: TestClient) -> dict:
    response = client.get("/api/pasting/pad-config")
    assert response.status_code == 200, response.text
    return response.json()


def _post_fill_path(client: TestClient, layer: str) -> dict:
    response = client.post("/api/pasting/pad-config/fill-path", json={"layer": layer})
    assert response.status_code == 200, response.text
    return response.json()


class TestPadConfigFillPath:
    """Fill path API returns enabled pad paths using resolved pad settings."""

    def test_fill_path_includes_only_enabled_requested_layer(
        self, selected_client: TestClient
    ):
        config = _get_config(selected_client)
        expected_ids = {
            pad["id"]
            for pad in config["pads"]
            if pad["layer"] == "Top" and pad["enabled"]
        }

        fill_path = _post_fill_path(selected_client, "Top")

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

        fill_path = _post_fill_path(selected_client, "Top")

        assert target["id"] not in {pad["id"] for pad in fill_path["pads"]}

    def test_fill_path_uses_resolved_overrides(self, selected_client: TestClient):
        selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L0", "values": {"dispense_mode": "area"}},
        )
        before = _post_fill_path(selected_client, "Top")

        patch = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L0", "values": {"boundary_margin": 0.3}},
        )
        assert patch.status_code == 200, patch.text

        after = _post_fill_path(selected_client, "Top")

        assert after["pads"] != before["pads"]

    def test_fill_path_uses_resolved_dispense_mode(self, selected_client: TestClient):
        patch = selected_client.patch(
            "/api/pasting/pad-config/node",
            json={"node": "L0", "values": {"dispense_mode": "dot"}},
        )
        assert patch.status_code == 200, patch.text

        fill_path = _post_fill_path(selected_client, "Top")

        assert fill_path["pads"]
        assert {pad["dispense_mode"] for pad in fill_path["pads"]} == {"dot"}
        assert all(pad["point_count"] == pad["path_count"] for pad in fill_path["pads"])

    def test_fill_path_without_selected_pcb_returns_409(self, client: TestClient):
        response = client.post(
            "/api/pasting/pad-config/fill-path", json={"layer": "Top"}
        )

        assert response.status_code == 409
