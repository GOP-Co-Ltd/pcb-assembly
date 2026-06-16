"""POST /api/pasting/pad-config/route contract tests."""

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


def _post_route(client: TestClient, layer: str) -> dict:
    response = client.post("/api/pasting/pad-config/route", json={"layer": layer})
    assert response.status_code == 200, response.text
    return response.json()


class TestPadConfigRoute:
    """Paste route API returns selected-board enabled pads for the requested
    layer."""

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

        route = _post_route(selected_client, "Top")
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

        route = _post_route(selected_client, "Bottom")

        assert route["layer"] == "Bottom"
        assert {pad["id"] for pad in route["pads"]} == expected_ids
        assert [pad["order"] for pad in route["pads"]] == list(
            range(1, len(route["pads"]) + 1)
        )
        assert all(len(pad["center"]) == 2 for pad in route["pads"])

    def test_route_without_selected_pcb_returns_409(self, client: TestClient):
        response = client.post("/api/pasting/pad-config/route", json={"layer": "Top"})

        assert response.status_code == 409
