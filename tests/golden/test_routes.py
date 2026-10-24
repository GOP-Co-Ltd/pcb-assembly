"""装置を動かす REST の route が実機へ送る G-code の列を golden で固定する.

route は実アプリ（``create_app``）に TestClient で送り、送信先は ``Devices`` に注入した 1 台の fake 機体にする。
golden は ``routes/<名前>.gcode`` に置く。
対話ジョブ中のマシン操作は REST と同じ golden と比べ、経路によらず同じ列を送ることを固定する。
ファームウェア再起動は WebUI のサービスも再起動するので、UpdateSandbox のスタブの中でだけ採る。
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcbasm.hal import XYZStage
from pcbasm.hal.fake import FakeAudioPlayer, FakeKlipper
from pcbasm.machine.devices import Devices
from tests.golden.conftest import GOLDEN_DIR, finish
from tests.support import polling
from tests.support.golden import assert_transcript
from tests.web.api.jobs.conftest import register_synthetic
from tests.web.update_support import make_update_sandbox
from web.api.app import create_app
from web.api.backend import Backend
from web.api.env import Env
from web.api.jobs.catalog import JobCatalog
from web.api.jobs.context import JobContext
from web.api.jobs.machine_commands import handle_machine_command
from web.api.jobs.manager import JobManager
from web.selfupdate.runner import UpdateRunner

ROUTES = GOLDEN_DIR / "routes"
# route を呼ぶ前の装置の状態（全軸ホーミング済みで、原点以外にいる）
START_POSITION = [100.0, 80.0, 5.0, 0.0]


@pytest.fixture
def homed_klipper(machine_klipper: FakeKlipper) -> FakeKlipper:
    machine_klipper.set_status("gcode_move", "gcode_position", START_POSITION)
    machine_klipper.set_status("toolhead", "homed_axes", "xyz")
    return machine_klipper


@pytest.fixture
def make_app(
    job_settings: Env, devices: Devices, paste_test_board_footprint_root: Path
) -> Callable[[UpdateRunner | None], FastAPI]:
    """実アプリを golden 用の config と装置で組む関数を返す（自己更新のランナーだけ差し替えられる）."""

    def make(update_runner: UpdateRunner | None) -> FastAPI:
        return create_app(
            job_settings,
            devices=devices,
            paste_test_board_footprint_root=paste_test_board_footprint_root,
            update_runner=update_runner,
        )

    return make


@pytest.fixture
def app(make_app: Callable[[UpdateRunner | None], FastAPI]) -> FastAPI:
    return make_app(None)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


# マシン操作パネルの命令（REST と対話ジョブの command が同じ形で送る）
MACHINE_COMMANDS = [
    # 全軸ホーミングのボタンは軸を指定せずに送る
    ("home_all", {"type": "home", "axes": []}),
    ("home_xyz", {"type": "home", "axes": ["x", "y", "z"]}),
    ("home_xy", {"type": "home", "axes": ["x", "y"]}),
    ("jog_x", {"type": "jog", "axis": "x", "distance": 1.5}),
    ("jog_z", {"type": "jog", "axis": "z", "distance": -0.1}),
    ("move", {"type": "move", "x": 120.0, "y": 50.0, "z": 5.0}),
    ("relax", {"type": "relax"}),
    ("focus_z", {"type": "focus_z"}),
    ("gcode", {"type": "gcode", "gcode": "G1 X110 F600"}),
    ("move_to_cap", {"type": "move_to_cap"}),
]


class TestMachineControl:
    """マシン操作パネル（POST /api/machine/command と、対話ジョブ中の command）."""

    @pytest.mark.parametrize(("name", "command"), MACHINE_COMMANDS)
    def test_rest(
        self,
        client: TestClient,
        homed_klipper: FakeKlipper,
        name: str,
        command: dict[str, Any],
    ):
        response = client.post("/api/machine/command", json=command)

        assert response.status_code == 200, response.text
        assert_transcript(homed_klipper.sent, ROUTES / f"machine_control_{name}.gcode")

    @pytest.mark.parametrize(("name", "command"), MACHINE_COMMANDS)
    def test_job_command(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        job_backend: Backend,
        homed_klipper: FakeKlipper,
        name: str,
        command: dict[str, Any],
    ):
        """対話ジョブ中の同じ命令も、REST と同じ列を同じ timeout で送る."""

        def run(ctx: JobContext) -> None:
            # 塗布のジョブと同じく、既定の timeout を持たない接続で受ける
            klipper = ctx.klipper()
            assert handle_machine_command(ctx, klipper, XYZStage(klipper), command)

        register_synthetic(catalog, run, name="machine_command")
        finish(job_backend, manager.start("machine_command", {}))

        assert_transcript(homed_klipper.sent, ROUTES / f"machine_control_{name}.gcode")


class TestNozzleMaintenance:
    """ノズルキャップとクリーニング位置の記録、クリーニングのテスト実行."""

    @pytest.mark.parametrize(
        ("path", "table"),
        [
            ("/api/pasting/nozzle/cap/record", "nozzle_cap"),
            ("/api/pasting/nozzle/clean/record", "nozzle_clean"),
        ],
    )
    def test_record_sends_nothing(
        self,
        client: TestClient,
        homed_klipper: FakeKlipper,
        job_backend: Backend,
        path: str,
        table: str,
    ):
        """記録は現在位置を読むだけで、何も送らない."""
        response = client.post(path)

        assert response.status_code == 200, response.text
        position = getattr(
            job_backend.machine_config.machine().paste_dispenser, table
        ).position
        assert (position.x, position.y, position.z) == tuple(START_POSITION[:3])
        assert homed_klipper.sent == ()

    def test_clean_test(self, client: TestClient, homed_klipper: FakeKlipper):
        """プライム → 接近 → パージ → 十字往復 → 退避 → リトラクト → キャップへ戻る."""
        response = client.post("/api/pasting/nozzle/clean/test")

        assert response.status_code == 200, response.text
        assert_transcript(homed_klipper.sent, ROUTES / "nozzle_clean_test.gcode")


class TestEmergencyStop:
    def test_sends_m112(self, client: TestClient, homed_klipper: FakeKlipper):
        assert client.post("/api/emergency-stop").status_code == 200

        assert_transcript(homed_klipper.sent, ROUTES / "emergency_stop.gcode")


class TestFirmwareRestart:
    def test_restarts_klipper_then_services(
        self,
        make_app: Callable[[UpdateRunner | None], FastAPI],
        homed_klipper: FakeKlipper,
        tmp_path: Path,
    ):
        """FIRMWARE_RESTART を送ってから、WebUI のサービスを再起動する."""
        sandbox = make_update_sandbox(tmp_path / "sandbox")
        with TestClient(make_app(UpdateRunner(sandbox.settings))) as client:
            response = client.post("/api/machine/firmware-restart")

        assert response.status_code == 200, response.text
        polling.wait_until(lambda: sandbox.restarts() != [], timeout=10.0)
        assert_transcript(homed_klipper.sent, ROUTES / "firmware_restart.gcode")


class TestJobEndParking:
    def test_parks_at_cap(
        self, app: FastAPI, client: TestClient, homed_klipper: FakeKlipper
    ):
        """装置を動かすジョブは、何も送らずに終わってもノズルキャップへ駐機する."""
        backend: Backend = app.state.backend
        register_synthetic(
            backend.catalog, lambda ctx: None, name="parking", uses_machine=True
        )

        assert client.post("/api/jobs/parking", json={}).status_code == 201
        polling.wait_until(
            lambda: client.get("/api/jobs/current").json()["job"]["status"]
            == "succeeded"
            and backend.lock.owner is None,
            timeout=30.0,
        )

        assert_transcript(homed_klipper.sent, ROUTES / "job_end_parking.gcode")
