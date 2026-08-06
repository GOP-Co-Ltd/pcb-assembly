"""UI host の local update API と machine proxy の分離契約。"""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcbasm.software_update import UpdateCoordinator, UpdatePhase, UpdateStatus
from tests.helpers import FakeAudioPlayer
from web.api.app import create_app as create_backend_app
from web.api.settings import Settings as ApiSettings
from web.ui.app import create_app as create_frontend_app
from web.ui.settings import Settings as UiSettings

REVISION_A = "a" * 40
REVISION_B = "b" * 40
REVISION_C = "c" * 40


class RecordingCoordinator(UpdateCoordinator):
    def __init__(self, role: str, available: str) -> None:
        self.current = UpdateStatus(
            role=role,
            enabled=True,
            phase=UpdatePhase.AVAILABLE,
            available=available,
            previous=REVISION_A,
            branch="main",
            can_apply=True,
            message=f"{role} update",
        )
        self.apply_requests: list[dict[str, object]] = []

    def status(self) -> UpdateStatus:
        return self.current

    def request_check(self) -> str:
        return "check"

    def request_apply(
        self,
        *,
        expected_branch: str,
        expected_revision: str,
        confirmed: bool,
        branch_confirmation: str | None,
    ) -> str:
        self.apply_requests.append(
            {
                "expected_branch": expected_branch,
                "expected_revision": expected_revision,
                "confirmed": confirmed,
                "branch_confirmation": branch_confirmation,
            }
        )
        return "apply"


def _apps(
    api_settings: ApiSettings,
    ui_settings: UiSettings,
    audio_player: FakeAudioPlayer,
    api_update: RecordingCoordinator,
    ui_update: RecordingCoordinator,
) -> tuple[FastAPI, FastAPI]:
    backend = create_backend_app(
        api_settings,
        audio_player=audio_player,
        update_coordinator=api_update,
        revision=REVISION_A,
    )
    frontend = create_frontend_app(
        ui_settings,
        transport_factory=lambda _endpoint: httpx.ASGITransport(app=backend),
        update_coordinator=ui_update,
        revision=REVISION_A,
    )
    return backend, frontend


def _close_backend(app: FastAPI) -> None:
    app.state.preview.request_shutdown()
    app.state.jobs.shutdown()
    app.state.appstate.close()


class TestUiHealth:
    def test_local_health_identifies_ui_service(self, ui_settings: UiSettings):
        app = create_frontend_app(ui_settings, revision=REVISION_A)

        with TestClient(app) as client:
            response = client.get("/api/health")

        assert response.json() == {
            "service": "ui",
            "revision": REVISION_A,
            "status": "ok",
        }


class TestLocalAndMachineUpdateTargets:
    def test_status_endpoints_keep_ui_and_selected_backend_separate(
        self,
        backend_settings: ApiSettings,
        ui_settings: UiSettings,
        audio_player: FakeAudioPlayer,
    ):
        api_update = RecordingCoordinator("api", REVISION_B)
        ui_update = RecordingCoordinator("ui", REVISION_C)
        backend, frontend = _apps(
            backend_settings, ui_settings, audio_player, api_update, ui_update
        )
        try:
            with TestClient(frontend) as client:
                local = client.get("/api/software-update")
                machine = client.get("/m/uitest/api/software-update")
        finally:
            _close_backend(backend)

        assert local.status_code == 200
        assert local.json()["role"] == "ui"
        assert local.json()["available"] == REVISION_C
        assert machine.status_code == 200
        assert machine.json()["role"] == "api"
        assert machine.json()["available"] == REVISION_B

    def test_local_apply_targets_only_ui_and_requires_exact_branch_confirmation(
        self,
        backend_settings: ApiSettings,
        ui_settings: UiSettings,
        audio_player: FakeAudioPlayer,
    ):
        api_update = RecordingCoordinator("api", REVISION_B)
        ui_update = RecordingCoordinator("ui", REVISION_C)
        ui_update.current = UpdateStatus(
            role="ui",
            enabled=True,
            phase=UpdatePhase.AVAILABLE,
            available=REVISION_C,
            previous=REVISION_A,
            branch="release",
            branch_change=True,
            can_apply=False,
            message="branch confirmation required",
        )
        backend, frontend = _apps(
            backend_settings, ui_settings, audio_player, api_update, ui_update
        )
        try:
            with TestClient(frontend) as client:
                rejected = client.post(
                    "/api/software-update/apply",
                    json={
                        "expected_branch": "release",
                        "expected_revision": REVISION_C,
                        "confirmed": True,
                        "branch_confirmation": "Release",
                    },
                )
                accepted = client.post(
                    "/api/software-update/apply",
                    json={
                        "expected_branch": "release",
                        "expected_revision": REVISION_C,
                        "confirmed": True,
                        "branch_confirmation": "release",
                    },
                )
        finally:
            _close_backend(backend)

        assert rejected.status_code == 409
        assert accepted.status_code == 202
        assert api_update.apply_requests == []
        assert ui_update.apply_requests == [
            {
                "expected_branch": "release",
                "expected_revision": REVISION_C,
                "confirmed": True,
                "branch_confirmation": "release",
            }
        ]
