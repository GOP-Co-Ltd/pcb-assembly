"""Backend software-update HTTP 境界の契約テスト。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcbasm.software_update import UpdateCoordinator, UpdatePhase, UpdateStatus
from tests.helpers import FakeAudioPlayer
from web.api.app import create_app
from web.api.settings import Settings

ALICE = {"X-Pcbasm-Session": "alice"}
BOB = {"X-Pcbasm-Session": "bob"}
REVISION_A = "a" * 40
REVISION_B = "b" * 40


def _available_status(role: str = "api") -> UpdateStatus:
    return UpdateStatus(
        role=role,
        enabled=True,
        phase=UpdatePhase.AVAILABLE,
        available=REVISION_B,
        previous=REVISION_A,
        branch="main",
        can_apply=True,
        message="更新できます",
    )


class RecordingCoordinator(UpdateCoordinator):
    def __init__(self, status: UpdateStatus | None = None) -> None:
        self.current = status or _available_status()
        self.check_requests = 0
        self.apply_requests: list[dict[str, object]] = []
        self.unavailable = False

    def status(self) -> UpdateStatus:
        return self.current

    def request_check(self) -> str:
        if self.unavailable:
            raise OSError("systemd unavailable")
        self.check_requests += 1
        return "check-1"

    def request_apply(
        self,
        *,
        expected_branch: str,
        expected_revision: str,
        confirmed: bool,
        branch_confirmation: str | None,
    ) -> str:
        if self.unavailable:
            raise OSError("systemd unavailable")
        self.apply_requests.append(
            {
                "expected_branch": expected_branch,
                "expected_revision": expected_revision,
                "confirmed": confirmed,
                "branch_confirmation": branch_confirmation,
            }
        )
        self.current = UpdateStatus(
            role=self.current.role,
            enabled=True,
            phase=UpdatePhase.QUEUED,
            running=True,
            available=self.current.available,
            previous=self.current.previous,
            branch=self.current.branch,
            can_apply=False,
            request_id="apply-1",
            message="更新を予約しました",
        )
        return "apply-1"


@contextmanager
def _client(
    settings: Settings,
    audio_player: FakeAudioPlayer,
    coordinator: RecordingCoordinator,
) -> Iterator[TestClient]:
    app = create_app(
        settings,
        audio_player=audio_player,
        update_coordinator=coordinator,
        revision=REVISION_A,
    )
    with TestClient(app) as client:
        yield client


class TestHealthEndpoint:
    def test_reports_role_revision_and_ok(
        self, webui_settings: Settings, audio_player: FakeAudioPlayer
    ):
        app = create_app(webui_settings, audio_player=audio_player, revision=REVISION_A)
        with TestClient(app) as client:
            response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json() == {
            "service": "api",
            "revision": REVISION_A,
            "status": "ok",
        }


class TestSoftwareUpdateStatus:
    def test_uninstalled_feature_returns_disabled_status(self, client: TestClient):
        response = client.get("/api/software-update")

        assert response.status_code == 200
        assert response.json()["enabled"] is False
        assert response.json()["phase"] == "disabled"
        assert response.json()["can_apply"] is False

    def test_returns_resolved_persistent_status(
        self, webui_settings: Settings, audio_player: FakeAudioPlayer
    ):
        coordinator = RecordingCoordinator()
        with _client(webui_settings, audio_player, coordinator) as client:
            response = client.get("/api/software-update")

        assert response.status_code == 200
        assert response.json()["role"] == "api"
        assert response.json()["available"] == REVISION_B
        assert response.json()["message"] == "更新できます"


class TestSoftwareUpdateRequests:
    def test_check_starts_fixed_oneshot_and_returns_202(
        self, webui_settings: Settings, audio_player: FakeAudioPlayer
    ):
        coordinator = RecordingCoordinator()
        with _client(webui_settings, audio_player, coordinator) as client:
            response = client.post("/api/software-update/check")

        assert response.status_code == 202
        assert coordinator.check_requests == 1

    def test_check_reports_launcher_unavailable_as_503(
        self, webui_settings: Settings, audio_player: FakeAudioPlayer
    ):
        coordinator = RecordingCoordinator()
        coordinator.unavailable = True
        with _client(webui_settings, audio_player, coordinator) as client:
            response = client.post("/api/software-update/check")

        assert response.status_code == 503

    def test_viewer_cannot_apply(
        self, webui_settings: Settings, audio_player: FakeAudioPlayer
    ):
        coordinator = RecordingCoordinator()
        with _client(webui_settings, audio_player, coordinator) as client:
            assert client.post("/api/control/acquire", headers=ALICE).status_code == 200
            response = client.post(
                "/api/software-update/apply",
                headers=BOB,
                json={
                    "expected_branch": "main",
                    "expected_revision": REVISION_B,
                    "confirmed": True,
                },
            )

        assert response.status_code == 423
        assert coordinator.apply_requests == []

    def test_stale_revision_and_client_selected_command_are_rejected(
        self, webui_settings: Settings, audio_player: FakeAudioPlayer
    ):
        coordinator = RecordingCoordinator()
        with _client(webui_settings, audio_player, coordinator) as client:
            stale = client.post(
                "/api/software-update/apply",
                headers=ALICE,
                json={
                    "expected_branch": "main",
                    "expected_revision": "c" * 40,
                    "confirmed": True,
                },
            )
            injected = client.post(
                "/api/software-update/apply",
                headers=ALICE,
                json={
                    "expected_branch": "main",
                    "expected_revision": REVISION_B,
                    "confirmed": True,
                    "service": "evil.service",
                    "command": ["sh"],
                },
            )

        assert stale.status_code == 409
        assert injected.status_code == 422
        assert coordinator.apply_requests == []

    def test_busy_machine_is_409_and_successful_request_enters_maintenance(
        self, webui_settings: Settings, audio_player: FakeAudioPlayer
    ):
        coordinator = RecordingCoordinator()
        app: FastAPI = create_app(
            webui_settings,
            audio_player=audio_player,
            update_coordinator=coordinator,
            revision=REVISION_A,
        )
        with TestClient(app) as client:
            app.state.appstate.acquire_machine("job")
            busy = client.post(
                "/api/software-update/apply",
                headers=ALICE,
                json={
                    "expected_branch": "main",
                    "expected_revision": REVISION_B,
                    "confirmed": True,
                },
            )
            app.state.appstate.release_machine()
            accepted = client.post(
                "/api/software-update/apply",
                headers=ALICE,
                json={
                    "expected_branch": "main",
                    "expected_revision": REVISION_B,
                    "confirmed": True,
                },
            )
            mutation = client.post(
                "/api/jobs/no_such_job", headers=ALICE, json={"params": {}}
            )
            emergency = client.post("/api/emergency-stop", headers=BOB)

        assert busy.status_code == 409
        assert accepted.status_code == 202
        assert mutation.status_code == 409
        assert emergency.status_code != 409

    def test_persisted_running_status_blocks_mutation_after_process_restart(
        self, webui_settings: Settings, audio_player: FakeAudioPlayer
    ):
        coordinator = RecordingCoordinator(
            UpdateStatus(
                role="api",
                enabled=True,
                phase=UpdatePhase.PREPARING,
                running=True,
                available=REVISION_B,
                previous=REVISION_A,
                branch="main",
                message="更新中です",
            )
        )
        with _client(webui_settings, audio_player, coordinator) as client:
            mutation = client.post(
                "/api/jobs/no_such_job", headers=ALICE, json={"params": {}}
            )
            emergency = client.post("/api/emergency-stop", headers=BOB)

        assert mutation.status_code == 409
        assert emergency.status_code != 409
