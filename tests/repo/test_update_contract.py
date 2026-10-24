"""更新経路と緊急停止の凍結した契約.

自己更新では旧版の updater が新しい版を取り込み、旧版の frontend が新しい版の機体を更新し、止める。

ここに固定したものを変えると、古い版から WebUI だけでは新しい版へ辿り着けなくなる。
"""

import importlib
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import attrs
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel

from pcbasm.hal.fake import FakeAudioPlayer, FakeKlipper
from pcbasm.paths import PROJECT_ROOT
from tests.support.settings import make_api_settings, make_ui_settings
from web.api.app import create_app as create_backend_app
from web.contract.update import (
    UpdateRepositoryInfo,
    UpdateRunInfo,
    UpdateRunResponse,
    UpdateStatusResponse,
    UpdateStepInfo,
)
from web.selfupdate.service import CANONICAL_ORDER, UNIT_NAMES
from web.selfupdate.settings import UpdateSettings
from web.selfupdate.steps import restart_command
from web.ui.app import create_app as create_frontend_app

# 応答の欄（旧版の update.js / bulk.js が読む）。足すのも消すのもスタックの外で行う
RESPONSE_FIELDS: dict[type[BaseModel], set[str]] = {
    UpdateStatusResponse: {
        "enabled",
        "hostname",
        "summary",
        "repository",
        "blocker",
        "fetch_error",
        "update_available",
        "restart_units",
        "restart_notice",
        "run",
    },
    UpdateRepositoryInfo: {
        "head",
        "head_subject",
        "head_label",
        "branch",
        "upstream",
        "upstream_head",
        "ahead",
        "behind",
        "dirty_paths",
        "untracked_paths",
        "up_to_date",
        "error",
    },
    UpdateRunInfo: {
        "run_id",
        "state",
        "state_label",
        "step",
        "steps",
        "from_head",
        "from_subject",
        "to_head",
        "to_subject",
        "restart_units",
        "error",
        "failed_detail",
        "warnings",
        "started_at",
        "finished_at",
    },
    UpdateStepInfo: {"step", "label", "ok", "detail"},
    UpdateRunResponse: {"run_id", "run"},
}

SUDOERS_SCRIPT = PROJECT_ROOT / "scripts" / "install-update-sudoers.sh"

ALICE = {"X-Pcbasm-Session": "alice-session"}
BOB = {"X-Pcbasm-Session": "bob-session"}


def route(app: FastAPI, method: str, path: str) -> APIRoute:
    """App に登録された 1 本の route（無ければテストを落とす）."""
    for candidate in app.routes:
        if (
            isinstance(candidate, APIRoute)
            and candidate.path == path
            and method in candidate.methods
        ):
            return candidate
    pytest.fail(f"{method} {path} がありません")


@pytest.fixture
def backend_app(
    tmp_path: Path, paste_test_board_footprint_root: Path
) -> Iterator[FastAPI]:
    """起動済みの backend（lifespan の後始末まで走らせる）."""
    app = create_backend_app(
        make_api_settings(tmp_path / "api", hostname="contract"),
        audio_player=FakeAudioPlayer(),
        paste_test_board_footprint_root=paste_test_board_footprint_root,
    )
    with TestClient(app):
        yield app


class TestAppModules:
    """Unit が起動し、更新後の smoke が import するモジュール."""

    @pytest.mark.parametrize("module", ["web.api.app", "web.ui.app"])
    def test_app_module_has_the_factory(self, module: str):
        assert callable(importlib.import_module(module).create_app)

    @pytest.mark.parametrize(
        ("target", "command"),
        [("api", "uv run python -m web.api"), ("ui", "uv run python -m web.ui")],
    )
    def test_make_target_starts_the_server(self, target: str, command: str):
        result = subprocess.run(
            ["make", "--no-print-directory", "-n", target],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )

        assert result.stdout.splitlines() == [command]


class TestServiceRestart:
    """Unit 名と、再起動に使う sudo の argv."""

    def test_unit_names(self):
        assert UNIT_NAMES == {"api": "pcbasm-api.service", "ui": "pcbasm-ui.service"}

    def test_restart_argv_is_allowed_by_the_sudoers_fragment(self):
        defaults = attrs.fields(UpdateSettings)
        settings = UpdateSettings(
            sudo_bin=defaults.sudo_bin.default,
            systemctl_bin=defaults.systemctl_bin.default,
        )
        sudoers = subprocess.run(
            ["bash", str(SUDOERS_SCRIPT), "show"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        # Cmnd_Alias の 1 行 1 コマンド（行末の ", \\" を落とす）
        allowed = [
            line.strip().rstrip("\\").strip().rstrip(",")
            for line in sudoers.splitlines()
            if line.strip().startswith(settings.systemctl_bin)
        ]
        units = [UNIT_NAMES[key] for key in CANONICAL_ORDER]

        for variant in ([units[0]], [units[1]], units):
            argv = restart_command(settings, variant)
            assert argv[:2] == ("/usr/bin/sudo", "-n")
            assert " ".join(argv[2:]) in allowed


class TestBackendEndpoints:
    """機体の `/api/update/*`."""

    @pytest.mark.parametrize(
        ("method", "path", "model"),
        [
            ("GET", "/api/update/status", UpdateStatusResponse),
            ("POST", "/api/update/check", UpdateStatusResponse),
            ("POST", "/api/update/run", UpdateRunResponse),
        ],
    )
    def test_update_endpoint_answers_the_frozen_model(
        self, backend_app: FastAPI, method: str, path: str, model: type[BaseModel]
    ):
        assert route(backend_app, method, path).response_model is model


class TestEmergencyStop:
    """緊急停止は操作権にも装置ロックにも妨げられない."""

    def test_viewer_stops_a_busy_machine(
        self, backend_app: FastAPI, mount_klipper: Callable[[FakeKlipper], None]
    ):
        klipper = FakeKlipper()
        mount_klipper(klipper)
        client = TestClient(backend_app)
        assert client.post("/api/control/acquire", headers=ALICE).status_code == 200

        with backend_app.state.appstate.machine_lock("pytest-job"):
            response = client.post("/api/emergency-stop", headers=BOB)

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert klipper.sent_lines == ["M112"]


class TestFrontendEndpoints:
    """UI サーバー自身の `/api/self-update/**`."""

    @pytest.mark.parametrize(
        ("method", "path", "model"),
        [
            ("GET", "/api/self-update", UpdateStatusResponse),
            ("POST", "/api/self-update/check", UpdateStatusResponse),
            ("POST", "/api/self-update/run", UpdateRunResponse),
        ],
    )
    def test_self_update_endpoint_answers_the_frozen_model(
        self, tmp_path: Path, method: str, path: str, model: type[BaseModel]
    ):
        app = create_frontend_app(
            make_ui_settings((), machines_file=tmp_path / "absent-machines.toml")
        )

        assert route(app, method, path).response_model is model


class TestResponseShape:
    """更新の状態と実行の応答（backend と UI サーバーで共通のモデル）."""

    @pytest.mark.parametrize("model", list(RESPONSE_FIELDS))
    def test_fields(self, model: type[BaseModel]):
        assert set(model.model_fields) == RESPONSE_FIELDS[model]
