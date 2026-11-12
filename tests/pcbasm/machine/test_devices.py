"""`pcbasm.machine.devices` の仕様テスト.

Moonraker は `moonraker` fixture（httpx.MockTransport で応える FakeMoonraker）で置き換える。
"""

import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, override

import httpx
import pytest
import tomlkit
from shapely import Point
from tomlkit.items import Table

from pcbasm.config import ConfigError, load_motion
from pcbasm.config.klipper import KlipperConfig
from pcbasm.config.pnp import MotionConfig
from pcbasm.errors import DeviceError
from pcbasm.hal import (
    ContactSettings,
    FrameHub,
    GamepadState,
    Limits,
    ScalarLimits,
    ServoMode,
    Velocity,
    XYZStage,
    mcu,
)
from pcbasm.hal.fake import (
    FAKE_MACHINE_PRINTER_CONFIG,
    FakeCamera,
    FakeGamepad,
    FakeKlipper,
    FakeMotionStage,
    FakeServoBus,
    ServoCommand,
)
from pcbasm.hal.mcu import LoadCellSettings, McuMotionSettings, TmcSettings
from pcbasm.machine.devices import Devices, motion_settings, open_devices
from pcbasm.machine.journal import Journal
from pcbasm.vision import Image
from tests.support import polling
from tests.support.machine_toml import motion_table, pnp_motion
from tests.support.moonraker import FakeMoonraker
from tests.support.paths import FAKE_CAMERA_IMAGE, MINIMAL_MACHINE_TOML
from tests.support.web import copy_test_config


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return copy_test_config(tmp_path) / "machine.toml"


@pytest.fixture
def journal_dir(tmp_path: Path) -> Path:
    return tmp_path / "journal"


@pytest.fixture
def devices(config_path: Path, journal_dir: Path) -> Devices:
    return open_devices(config_path, journal_dir, fake=False)


class TestMoonraker:
    def test_opening_connects_to_nothing(
        self, config_path: Path, journal_dir: Path, moonraker: FakeMoonraker
    ):
        open_devices(config_path, journal_dir, fake=False)

        assert moonraker.requests == []

    def test_works_once_moonraker_comes_up(
        self, devices: Devices, moonraker: FakeMoonraker
    ):
        """電源投入直後に Moonraker が遅れて起きても、操作なしで使える状態へ戻る."""
        moonraker.up = False
        with pytest.raises(httpx.ConnectError):
            devices.klipper(10.0).get_status("toolhead", "homed_axes")

        moonraker.up = True

        assert devices.klipper(10.0).get_status("toolhead", "homed_axes") == ""

    def test_printer_cfg_change_takes_effect_on_the_next_stage(
        self, devices: Devices, moonraker: FakeMoonraker
    ):
        """Mainsail で可動域を変えても実行中のジョブは同じ版で走り、次のジョブから新しい可動域で検証する."""
        job = devices.klipper()
        assert XYZStage(job).limits.x.max == 300.0

        moonraker.config["stepper_x"]["position_max"] = "200"

        assert XYZStage(job).limits.x.max == 300.0
        assert XYZStage(devices.klipper()).limits.x.max == 200.0

    def test_follows_klipper_endpoint_change(
        self, config_path: Path, devices: Devices, moonraker: FakeMoonraker
    ):
        devices.klipper().send_gcode("M400")
        text = config_path.read_text(encoding="utf-8")
        config_path.write_text(text.replace("port = 7126", "port = 7200"))

        devices.klipper().send_gcode("M400")

        assert [request.url.port for request in moonraker.requests] == [7126, 7200]

    def test_given_endpoint_is_used_without_reading_machine_toml(
        self, config_path: Path, devices: Devices, moonraker: FakeMoonraker
    ):
        """ジョブは開始時に読んだ接続先で走り、途中で machine.toml が壊れても終了時の駐機を送れる."""
        config_path.write_text("[klipper\n", encoding="utf-8")

        devices.klipper(endpoint=KlipperConfig("localhost", 7300)).send_gcode("M84")

        assert [request.url.port for request in moonraker.requests] == [7300]

    def test_applies_timeout_of_each_connection(
        self, devices: Devices, moonraker: FakeMoonraker
    ):
        devices.klipper(60.0).send_gcode("G28")
        devices.klipper().send_gcode("M400")
        devices.klipper(60.0).send_gcode("M400", timeout=5.0)

        assert [
            request.extensions["timeout"]["read"] for request in moonraker.requests
        ] == [60.0, None, 5.0]

    @pytest.mark.usefixtures("moonraker")
    def test_journals_rest_sends_to_today_and_job_sends_to_the_given_journal(
        self, devices: Devices, journal_dir: Path
    ):
        job = Journal.for_job(journal_dir, "job1")

        devices.klipper(60.0).send_gcode("G28")
        devices.klipper(journal=job).send_gcode("M84")

        rest = journal_dir / f"{date.today().isoformat()}-rest.gcode"
        assert rest.read_text(encoding="utf-8") == "# send timeout=60.0\nG28\n"
        assert job.path.read_text(encoding="utf-8") == "# send timeout=None\nM84\n"

    def test_emergency_stop_posts_to_moonraker(
        self, devices: Devices, moonraker: FakeMoonraker
    ):
        devices.emergency_stop()

        assert [request.url.path for request in moonraker.requests] == [
            "/printer/emergency_stop"
        ]

    def test_emergency_stop_does_not_wait_behind_a_long_send(
        self, devices: Devices, moonraker: FakeMoonraker
    ):
        """緊急停止は使い回すクライアントでの長い送信の後ろで待たない."""
        moonraker.released.clear()
        sending = threading.Thread(
            target=devices.klipper().send_gcode, args=("G4 P600000",), daemon=True
        )
        sending.start()
        polling.wait_until(lambda: len(moonraker.requests) == 1)

        polling.before_deadline(devices.emergency_stop, what="緊急停止")

        moonraker.released.set()
        sending.join()
        assert [request.url.path for request in moonraker.requests] == [
            "/printer/gcode/script",
            "/printer/emergency_stop",
        ]


class TestFake:
    def test_records_every_connection_on_one_machine(
        self, config_path: Path, journal_dir: Path
    ):
        klipper = FakeKlipper()
        devices = Devices(config_path, journal_dir, klipper=klipper)

        devices.klipper(60.0).send_gcode("G28")
        devices.klipper().send_gcode("M400")
        devices.emergency_stop()

        assert [(str(sent.gcode), sent.timeout) for sent in klipper.sent] == [
            ("G28", 60.0),
            ("M400", None),
            ("M112", 10.0),
        ]

    def test_open_devices_fake_never_reaches_moonraker(
        self, config_path: Path, journal_dir: Path
    ):
        # open_klipper は root conftest が塞いでいるので、呼べばテストが落ちる
        devices = open_devices(config_path, journal_dir, fake=True)

        devices.klipper().send_gcode("G28")
        devices.emergency_stop()

        assert devices.klipper().get_status("toolhead", "homed_axes") == "xyz"

    def test_open_devices_fake_has_the_machine_printer_cfg(
        self, config_path: Path, journal_dir: Path
    ):
        """シミュレーション起動（make dev）の printer.cfg は、駐機の PRESENT マクロまで実機と同じ."""
        devices = open_devices(config_path, journal_dir, fake=True)

        assert devices.klipper().get_config() == FAKE_MACHINE_PRINTER_CONFIG


class TestCamera:
    """FrameHub は初回の呼び出しでカメラを開き、再構築と後始末で止める."""

    @pytest.fixture
    def opened(self) -> list[FakeCamera]:
        return []

    @pytest.fixture
    def camera_devices(
        self, config_path: Path, journal_dir: Path, opened: list[FakeCamera]
    ) -> Devices:
        def open_camera() -> FakeCamera:
            opened.append(FakeCamera([Image.load(FAKE_CAMERA_IMAGE)]))
            return opened[-1]

        return Devices(config_path, journal_dir, camera=open_camera)

    def test_frame_hub_opens_camera_once_on_first_use(
        self, camera_devices: Devices, opened: list[FakeCamera]
    ):
        assert opened == []

        hub = camera_devices.frame_hub()

        assert isinstance(hub, FrameHub)
        assert camera_devices.frame_hub() is hub
        assert len(opened) == 1

    def test_rebuild_camera_stops_hub_and_reopens(self, camera_devices: Devices):
        hub = camera_devices.frame_hub()
        hub.start()

        camera_devices.rebuild_camera()

        assert not hub.running
        assert camera_devices.frame_hub() is not hub

    def test_close_stops_hub(self, camera_devices: Devices):
        hub = camera_devices.frame_hub()
        hub.start()

        camera_devices.close()

        assert not hub.running

    def test_open_devices_fake_camera_draws_the_scene_where_the_fake_stage_is(
        self, config_path: Path, journal_dir: Path
    ):
        devices = open_devices(
            config_path,
            journal_dir,
            fake=True,
            scene=lambda: [Point(10.0, 20.0).buffer(1.0)],
        )
        hub = devices.frame_hub()
        hub.start()
        camera = hub.subscribe()
        klipper = devices.klipper()

        def center() -> int:
            image = camera.capture().numpy()
            return int(image[image.shape[0] // 2, image.shape[1] // 2, 0])

        try:
            klipper.send_gcode("G28\nG90\nG1 X10 Y20")
            on_shape = center()
            klipper.send_gcode("G1 X30 Y20")
            off_shape = center()
        finally:
            devices.close()

        assert on_shape > off_shape

    def test_open_devices_camera_image_is_a_still_camera(
        self, config_path: Path, journal_dir: Path
    ):
        devices = open_devices(
            config_path, journal_dir, fake=False, camera_image=FAKE_CAMERA_IMAGE
        )

        camera = devices.frame_hub().subscribe()

        assert camera.info.name == "FakeCamera"
        assert camera.resolution.width == Image.load(FAKE_CAMERA_IMAGE).width


_SERVO_ROLES = {
    "rotation_id": 1,
    "open_close_id": 2,
    "closed_angle": 180.0,
    "open_angle": 225.0,
}


def _write_servo(config_path: Path, **values: object) -> None:
    """machine.toml の末尾に [pnp.servo] を足す（port は /dev/ttyACM0）."""
    lines = ['port = "/dev/ttyACM0"'] + [
        f"{key} = {value}" for key, value in values.items()
    ]
    with config_path.open("a", encoding="utf-8") as f:
        f.write("\n[pnp.servo]\n" + "\n".join(lines) + "\n")


def _servo_bus() -> FakeServoBus:
    """サーボ設定ジョブを済ませた 2 台（角度制限は _SERVO_ROLES の閉じ切り 180° 〜 開き切り 225°）."""
    return FakeServoBus(
        {1: ServoMode.STEP, 2: ServoMode.POSITION},
        angle_limits={1: (0, 0), 2: (2048, 2560)},
    )


class TestGamepad:
    """コントローラは呼ぶたびに開く（呼んだ側が閉じる）."""

    def test_connect_gamepad_returns_the_fake_given(
        self, config_path: Path, journal_dir: Path
    ):
        pad = FakeGamepad()
        devices = Devices(config_path, journal_dir, gamepad=pad)

        assert devices.connect_gamepad() is pad

    def test_open_devices_fake_has_a_released_gamepad(
        self, config_path: Path, journal_dir: Path
    ):
        # open_gamepad は root conftest が塞いでいるので、呼べばテストが落ちる
        devices = open_devices(config_path, journal_dir, fake=True)

        assert devices.connect_gamepad().state() == GamepadState()


class TestServoBus:
    """サーボのバスは初回の呼び出しで machine.toml の [pnp.servo] から開き、後始末で閉じる."""

    def test_uses_the_given_fake_bus(self, config_path: Path, journal_dir: Path):
        _write_servo(config_path)
        bus = _servo_bus()
        devices = Devices(config_path, journal_dir, servo_bus=bus)

        assert devices.servo_bus() is bus

    def test_opens_the_configured_port_once_on_first_use(
        self,
        config_path: Path,
        journal_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _write_servo(config_path, baudrate=500000)
        opened: list[tuple[str, int]] = []
        bus = _servo_bus()

        def open_servo_bus(port: str, baudrate: int = 1_000_000) -> FakeServoBus:
            opened.append((port, baudrate))
            return bus

        monkeypatch.setattr("pcbasm.hal.open_servo_bus", open_servo_bus)
        devices = Devices(config_path, journal_dir)

        assert devices.servo_bus() is bus
        assert devices.servo_bus() is bus
        assert opened == [("/dev/ttyACM0", 500000)]

    def test_without_pnp_section_is_a_config_error(
        self, config_path: Path, journal_dir: Path
    ):
        devices = Devices(config_path, journal_dir)

        with pytest.raises(ConfigError):
            devices.servo_bus()

    def test_without_servo_table_is_a_config_error(
        self, config_path: Path, journal_dir: Path
    ):
        with config_path.open("a", encoding="utf-8") as f:
            f.write("\n[pnp]\n")
        devices = Devices(config_path, journal_dir)

        with pytest.raises(ConfigError):
            devices.servo_bus()

    def test_reopens_when_the_port_changes(
        self,
        config_path: Path,
        journal_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _write_servo(config_path)
        opened: list[tuple[str, int]] = []
        buses: list[FakeServoBus] = []

        def open_servo_bus(port: str, baudrate: int = 1_000_000) -> FakeServoBus:
            opened.append((port, baudrate))
            buses.append(_servo_bus())
            return buses[-1]

        monkeypatch.setattr("pcbasm.hal.open_servo_bus", open_servo_bus)
        devices = Devices(config_path, journal_dir)
        first = devices.servo_bus()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'port = "/dev/ttyACM0"', 'port = "/dev/ttyACM1"\nbaudrate = 500000'
            ),
            encoding="utf-8",
        )

        second = devices.servo_bus()

        assert opened == [("/dev/ttyACM0", 1_000_000), ("/dev/ttyACM1", 500000)]
        assert second is not first
        assert first is buses[0] and buses[0].closed
        assert not buses[1].closed

    def test_close_closes_the_opened_bus(
        self,
        config_path: Path,
        journal_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _write_servo(config_path)
        bus = _servo_bus()
        monkeypatch.setattr(
            "pcbasm.hal.open_servo_bus", lambda port, baudrate=1_000_000: bus
        )
        devices = Devices(config_path, journal_dir)
        devices.servo_bus()

        devices.close()

        assert bus.closed

    def test_close_closes_the_bus(self, config_path: Path, journal_dir: Path):
        _write_servo(config_path)
        bus = _servo_bus()
        devices = Devices(config_path, journal_dir, servo_bus=bus)
        devices.servo_bus()

        devices.close()

        assert bus.closed


class TestTweezer:
    """ピンセットは呼ぶたびに machine.toml の [pnp.servo] の役割で組む."""

    def test_tweezer_drives_the_configured_roles(
        self, config_path: Path, journal_dir: Path
    ):
        _write_servo(config_path, **_SERVO_ROLES)
        bus = _servo_bus()
        devices = Devices(config_path, journal_dir, servo_bus=bus)

        tweezer = devices.tweezer()
        tweezer.rotate_by(90.0)
        tweezer.open_by(10.0)

        assert [c for c in bus.commands if c.kind != "torque"] == [
            ServoCommand(1, "step", 1024),
            ServoCommand(2, "goto", 2048),  # 脱力していたので、今の位置で保持してから
            ServoCommand(2, "goto", 2048 + 114),
        ]

    def test_closed_and_open_on_the_same_raw_position_is_a_config_error(
        self, config_path: Path, journal_dir: Path
    ):
        """角度は違っても raw に丸めると同じ位置なら、開閉できない（開き具合を割れない）."""
        _write_servo(
            config_path,
            rotation_id=1,
            open_close_id=2,
            closed_angle=10.0,
            open_angle=10.01,
        )
        devices = Devices(config_path, journal_dir, servo_bus=_servo_bus())

        with pytest.raises(ConfigError) as raised:
            devices.tweezer()

        assert "closed_angle" in str(raised.value)

    def test_tweezer_rotates_at_the_configured_speed(
        self, config_path: Path, journal_dir: Path
    ):
        _write_servo(config_path, **_SERVO_ROLES, rotation_speed=90.0)
        bus = _servo_bus()
        devices = Devices(config_path, journal_dir, servo_bus=bus)

        devices.tweezer().rotate_by(90.0)

        assert bus.speed(1) == 1024

    @pytest.mark.parametrize("missing", list(_SERVO_ROLES))
    def test_unset_role_is_a_config_error_naming_the_key(
        self, config_path: Path, journal_dir: Path, missing: str
    ):
        _write_servo(
            config_path,
            **{key: value for key, value in _SERVO_ROLES.items() if key != missing},
        )
        bus = _servo_bus()
        devices = Devices(config_path, journal_dir, servo_bus=bus)

        with pytest.raises(ConfigError) as raised:
            devices.tweezer()

        assert missing in str(raised.value)
        assert bus.commands == []

    def test_reads_machine_toml_on_each_call(
        self, config_path: Path, journal_dir: Path
    ):
        """役割を判別して machine.toml に保存したら、作り直さずに次の呼び出しから使える."""
        _write_servo(config_path)
        bus = _servo_bus()
        devices = Devices(config_path, journal_dir, servo_bus=bus)
        with pytest.raises(ConfigError):
            devices.tweezer()

        text = config_path.read_text(encoding="utf-8")
        roles = "".join(f"{key} = {value}\n" for key, value in _SERVO_ROLES.items())
        config_path.write_text(
            text.replace('port = "/dev/ttyACM0"\n', f'port = "/dev/ttyACM0"\n{roles}'),
            encoding="utf-8",
        )

        assert devices.tweezer().opening() == 0.0

    def test_open_devices_fake_has_a_tweezer_on_ids_1_and_2(
        self, config_path: Path, journal_dir: Path
    ):
        # open_servo_bus は root conftest が塞いでいるので、呼べばテストが落ちる
        _write_servo(config_path, **_SERVO_ROLES)
        devices = open_devices(config_path, journal_dir, fake=True)

        tweezer = devices.tweezer()
        tweezer.rotate_by(90.0)

        bus = devices.servo_bus()
        assert bus.mode(1) == ServoMode.STEP
        assert bus.mode(2) == ServoMode.POSITION
        # サーボ設定ジョブを済ませた状態（Rotation は制限なし、Open Close は設定の閉じ切り〜開き切り）
        assert (bus.angle_limits(1), bus.angle_limits(2)) == ((0, 0), (2048, 2560))
        assert bus.position(1) == (2048 + 1024) % 4096


def _use_motion(config_path: Path, motion: dict[str, Any] | None = None) -> None:
    """machine.toml を、[klipper] の無い新ファームの機体（[pnp.motion]）にする."""
    config_path.write_text(
        pnp_motion(config_path.read_text(encoding="utf-8"), motion), encoding="utf-8"
    )


def _motion_stage() -> FakeMotionStage:
    return FakeMotionStage(
        Limits(
            x=ScalarLimits(0.0, 68.0),
            y=ScalarLimits(0.0, 50.0),
            z=ScalarLimits(-40.0, 0.0),
            v=ScalarLimits(0.0, 100.0),
        ),
        Velocity(100.0, 100.0, 50.0),
    )


class _OpenedStages:
    """`mcu.open_motion_stage` の代わりに、渡された設定を記録して fake のステージを返す."""

    def __init__(self) -> None:
        self.settings: list[McuMotionSettings] = []
        self.stages: list[FakeMotionStage] = []

    def __call__(self, settings: McuMotionSettings) -> FakeMotionStage:
        self.settings.append(settings)
        self.stages.append(_motion_stage())
        return self.stages[-1]


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> _OpenedStages:
    stages = _OpenedStages()
    monkeypatch.setattr(mcu, "open_motion_stage", stages)
    return stages


class TestMotionStage:
    """新ファームのステージは初回の呼び出しで [pnp.motion] から開き、設定が変わっても restart まで開き直さない."""

    def test_klipper_machine_does_not_use_motion_stage(self, devices: Devices):
        assert not devices.uses_motion_stage()
        assert devices.opened_motion_stage() is None

    def test_without_motion_table_is_a_device_error(self, devices: Devices):
        with pytest.raises(DeviceError, match=r"\[pnp\.motion\]"):
            devices.motion_stage()

    def test_uses_the_given_fake_stage(self, config_path: Path, journal_dir: Path):
        _use_motion(config_path)
        stage = _motion_stage()
        devices = Devices(config_path, journal_dir, motion=stage)

        assert devices.uses_motion_stage()
        assert devices.motion_stage() is stage
        assert devices.opened_motion_stage() is stage

    def test_opens_once_on_first_use(
        self, config_path: Path, journal_dir: Path, opened: _OpenedStages
    ):
        _use_motion(config_path)
        devices = Devices(config_path, journal_dir)
        assert devices.uses_motion_stage()
        assert devices.opened_motion_stage() is None
        assert opened.settings == []

        first = devices.motion_stage()

        assert devices.motion_stage() is first
        assert devices.opened_motion_stage() is first
        assert [settings.device for settings in opened.settings] == ["/dev/ttyACM0"]

    def test_does_not_reopen_when_machine_toml_changes(
        self, config_path: Path, journal_dir: Path, opened: _OpenedStages
    ):
        _use_motion(config_path)
        devices = Devices(config_path, journal_dir)
        first = devices.motion_stage()
        motion = motion_table()
        motion["serial"] = "/dev/ttyACM1"
        _use_motion_text(config_path, motion)

        assert devices.motion_stage() is first
        assert len(opened.settings) == 1

    def test_restart_reopens_with_the_changed_machine_toml(
        self, config_path: Path, journal_dir: Path, opened: _OpenedStages
    ):
        _use_motion(config_path)
        devices = Devices(config_path, journal_dir)
        first = devices.motion_stage()
        motion = motion_table()
        motion["serial"] = "/dev/ttyACM1"
        _use_motion_text(config_path, motion)

        second = devices.restart_motion_stage()

        assert second is not first
        assert devices.motion_stage() is second
        assert [settings.device for settings in opened.settings] == [
            "/dev/ttyACM0",
            "/dev/ttyACM1",
        ]
        assert opened.stages[0].closed
        assert not opened.stages[1].closed

    def test_restart_restarts_the_open_stage_when_unchanged(
        self, config_path: Path, journal_dir: Path, opened: _OpenedStages
    ):
        _use_motion(config_path)
        devices = Devices(config_path, journal_dir)
        stage = devices.motion_stage()
        stage.emergency_stop()

        assert devices.restart_motion_stage() is stage
        assert len(opened.settings) == 1
        assert opened.stages[0].commands[-1] == "restart"
        assert stage.status().shutdown_reason is None

    def test_restart_opens_the_stage_when_not_open(
        self, config_path: Path, journal_dir: Path, opened: _OpenedStages
    ):
        _use_motion(config_path)
        devices = Devices(config_path, journal_dir)

        stage = devices.restart_motion_stage()

        assert devices.opened_motion_stage() is stage
        assert len(opened.settings) == 1

    def test_restart_restarts_the_given_fake_stage(
        self, config_path: Path, journal_dir: Path
    ):
        _use_motion(config_path)
        stage = _motion_stage()
        devices = Devices(config_path, journal_dir, motion=stage)

        assert devices.restart_motion_stage() is stage
        assert stage.commands == ["restart"]

    def test_limits_come_from_machine_toml_without_opening(
        self, config_path: Path, journal_dir: Path, opened: _OpenedStages
    ):
        _use_motion(config_path)
        devices = Devices(config_path, journal_dir)

        limits = devices.motion_limits()

        assert (limits.x.min, limits.x.max) == (0.0, 68.0)
        assert (limits.z.min, limits.z.max) == (-40.0, 0.0)
        assert limits.v.max == 100.0
        assert opened.settings == []
        assert devices.opened_motion_stage() is None

    def test_limits_of_the_given_fake_stage(self, config_path: Path, journal_dir: Path):
        _use_motion(config_path)
        stage = _motion_stage()
        devices = Devices(config_path, journal_dir, motion=stage)

        assert devices.motion_limits() == stage.limits

    def test_close_closes_the_opened_stage(
        self, config_path: Path, journal_dir: Path, opened: _OpenedStages
    ):
        _use_motion(config_path)
        devices = Devices(config_path, journal_dir)
        devices.motion_stage()

        devices.close()

        assert opened.stages[0].closed
        assert devices.opened_motion_stage() is None

    def test_close_closes_the_given_fake_stage(
        self, config_path: Path, journal_dir: Path
    ):
        _use_motion(config_path)
        stage = _motion_stage()
        devices = Devices(config_path, journal_dir, motion=stage)

        devices.close()

        assert stage.closed


class TestMotionEmergencyStop:
    """新ファームの機体の緊急停止は MCU へ送り、Moonraker へは送らない."""

    def test_goes_to_the_open_stage(
        self,
        config_path: Path,
        journal_dir: Path,
        opened: _OpenedStages,
        moonraker: FakeMoonraker,
    ):
        _use_motion(config_path)
        devices = Devices(config_path, journal_dir)
        devices.motion_stage()

        devices.emergency_stop()

        assert opened.stages[0].commands == ["emergency_stop"]
        assert moonraker.requests == []

    def test_sends_with_a_minimal_open_when_the_stage_is_not_open(
        self,
        config_path: Path,
        journal_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        moonraker: FakeMoonraker,
    ):
        """開いていないステージは開かず（設定も reset もせず）、緊急停止だけを送る."""
        _use_motion(config_path)
        sent: list[str] = []
        monkeypatch.setattr(
            mcu, "send_emergency_stop", lambda device, **_kwargs: sent.append(device)
        )
        devices = Devices(config_path, journal_dir)

        devices.emergency_stop()

        assert sent == ["/dev/ttyACM0"]
        assert devices.opened_motion_stage() is None
        assert moonraker.requests == []

    def test_goes_to_the_given_fake_stage(self, config_path: Path, journal_dir: Path):
        _use_motion(config_path)
        stage = _motion_stage()
        klipper = FakeKlipper()
        devices = Devices(config_path, journal_dir, klipper=klipper, motion=stage)

        devices.emergency_stop()

        assert stage.commands == ["emergency_stop"]
        assert klipper.sent == ()

    @pytest.mark.parametrize(
        "closing",
        [
            pytest.param(Devices.restart_motion_stage, id="restart-with-changed-toml"),
            pytest.param(Devices.close, id="close"),
        ],
    )
    def test_reaches_the_old_stage_while_it_is_closing(
        self,
        config_path: Path,
        journal_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        closing: Callable[[Devices], object],
    ):
        """古いステージを閉じ終えるまで（実行中の原点出しや移動の終わりを待つ間）、緊急停止は古い接続から送る."""
        _use_motion(config_path)
        old = _SlowToCloseStage()
        stages = iter([old, _motion_stage()])
        monkeypatch.setattr(mcu, "open_motion_stage", lambda _settings: next(stages))
        sent: list[str] = []
        monkeypatch.setattr(
            mcu, "send_emergency_stop", lambda device, **_kwargs: sent.append(device)
        )
        devices = Devices(config_path, journal_dir)
        devices.motion_stage()
        motion = motion_table()
        motion["serial"] = "/dev/ttyACM1"
        _use_motion_text(config_path, motion)
        worker = threading.Thread(target=closing, args=(devices,))
        worker.start()
        try:
            assert old.closing.wait(timeout=5.0)
            devices.emergency_stop()
        finally:
            old.release.set()
            worker.join(timeout=5.0)

        assert old.commands == ["emergency_stop"]
        assert sent == []
        assert old.closed


class _SlowToCloseStage(FakeMotionStage):
    """閉じるときに、実行中の操作の終わりを待つステージ（``release`` まで close が戻らない）."""

    def __init__(self) -> None:
        stage = _motion_stage()
        super().__init__(stage.limits, stage.max_velocity)
        self.closing = threading.Event()
        self.release = threading.Event()

    @override
    def close(self) -> None:
        self.closing.set()
        self.release.wait(timeout=5.0)
        super().close()


def _break_motion(config_path: Path, *, klipper: bool) -> None:
    """[pnp.motion] を読めない値にする（移行の途中で書き損じた machine.toml）."""
    motion = motion_table()
    motion["x"]["max_velocity"] = "fast"
    config_path.write_text(
        pnp_motion(config_path.read_text(encoding="utf-8"), motion, klipper=klipper),
        encoding="utf-8",
    )


class TestBrokenMotionTable:
    """[pnp.motion] が読めなくても、[klipper] が読めれば Klipper の機体として緊急停止と操作を届ける."""

    def test_emergency_stop_reaches_klipper(
        self, config_path: Path, journal_dir: Path, moonraker: FakeMoonraker
    ):
        _break_motion(config_path, klipper=True)
        devices = Devices(config_path, journal_dir)

        devices.emergency_stop()

        assert not devices.uses_motion_stage()
        assert [request.url.path for request in moonraker.requests] == [
            "/printer/emergency_stop"
        ]

    def test_without_klipper_the_motion_error_is_raised(
        self, config_path: Path, journal_dir: Path, moonraker: FakeMoonraker
    ):
        _break_motion(config_path, klipper=False)
        devices = Devices(config_path, journal_dir)

        with pytest.raises(ConfigError, match="pnp.motion"):
            devices.uses_motion_stage()
        with pytest.raises(ConfigError, match="pnp.motion"):
            devices.emergency_stop()
        assert moonraker.requests == []


class TestKlipperOnMotionMachine:
    def test_klipper_is_a_device_error_without_klipper_table(
        self, config_path: Path, journal_dir: Path, moonraker: FakeMoonraker
    ):
        _use_motion(config_path)
        devices = Devices(config_path, journal_dir)

        with pytest.raises(DeviceError, match="Klipper を使いません"):
            devices.klipper()

        assert moonraker.requests == []

    def test_klipper_table_kept_with_motion_is_still_reached(
        self, config_path: Path, journal_dir: Path, moonraker: FakeMoonraker
    ):
        config_path.write_text(
            pnp_motion(config_path.read_text(encoding="utf-8"), klipper=True),
            encoding="utf-8",
        )
        devices = Devices(config_path, journal_dir)

        assert devices.klipper(10.0).get_status("toolhead", "homed_axes") == ""


class TestMotionSettings:
    """[pnp.motion] を MCU のステージの設定へ直す（ピンの !

    と ^ を解く）.
    """

    def test_kurousagi_values(self, tmp_path: Path):
        settings = motion_settings(_load_motion(tmp_path, motion_table()))

        assert settings.device == "/dev/ttyACM0"
        assert settings.kinematics == "inversed_corexy"
        assert (settings.tmc_rx_pin, settings.tmc_tx_pin) == ("gpio9", "gpio8")
        assert not settings.tmc_rx_pull_up
        x = settings.x
        assert (x.step_pin, x.dir_pin, x.enable_pin, x.endstop_pin) == (
            "gpio11",
            "gpio10",
            "gpio12",
            "gpio4",
        )
        assert x.invert_dir and x.invert_enable
        assert x.endstop_pull_up and not x.endstop_invert
        assert x.steps_per_mm == 200 * 64 / 32.0
        assert (x.position_min, x.position_max, x.position_endstop) == (0.0, 68.0, 0.0)
        assert not x.homing_positive
        assert (x.homing_speed, x.second_homing_speed, x.homing_retract) == (
            20.0,
            10.0,
            5.0,
        )
        assert (x.max_velocity, x.max_accel, x.tmc_address) == (100.0, 500.0, 0)
        assert x.tmc == TmcSettings(
            run_current=0.4, hold_current=None, sense_resistor=0.110, microsteps=64
        )
        z = settings.z
        assert z.homing_positive
        assert (z.position_min, z.position_max) == (-40.0, 0.0)
        assert z.second_homing_speed == 5.0
        assert z.tmc_address == 1

    def test_given_second_homing_speed_and_hold_current_are_used(self, tmp_path: Path):
        motion = motion_table()
        motion["x"]["second_homing_speed"] = 3.0
        motion["x"]["hold_current"] = 0.2

        x = motion_settings(_load_motion(tmp_path, motion)).x

        assert x.second_homing_speed == 3.0
        assert x.tmc.hold_current == 0.2

    @pytest.mark.parametrize(
        ("pin", "pull_up", "invert"),
        [
            ("gpio4", False, False),
            ("^gpio4", True, False),
            ("!gpio4", False, True),
            ("^!gpio4", True, True),
            ("!^gpio4", True, True),
        ],
    )
    def test_endstop_pin_takes_pull_up_and_invert(
        self, tmp_path: Path, pin: str, pull_up: bool, invert: bool
    ):
        motion = motion_table()
        motion["x"]["endstop_pin"] = pin

        x = motion_settings(_load_motion(tmp_path, motion)).x

        assert (x.endstop_pin, x.endstop_pull_up, x.endstop_invert) == (
            "gpio4",
            pull_up,
            invert,
        )

    def test_idle_timeout_is_passed_on(self, tmp_path: Path):
        motion = motion_table()
        motion["idle_timeout"] = 120.0

        settings = motion_settings(_load_motion(tmp_path, motion))

        assert settings.idle_timeout == 120.0

    def test_tmc_rx_pin_takes_pull_up(self, tmp_path: Path):
        motion = motion_table()
        motion["tmc_rx_pin"] = "^gpio9"

        settings = motion_settings(_load_motion(tmp_path, motion))

        assert (settings.tmc_rx_pin, settings.tmc_rx_pull_up) == ("gpio9", True)

    @pytest.mark.parametrize(
        ("table", "key", "pin"),
        [
            ("x", "endstop_pin", "~gpio4"),  # プルダウンは持たない
            ("x", "step_pin", "!gpio11"),
            ("x", "dir_pin", "^gpio10"),
            ("x", "enable_pin", "^!gpio12"),
            ("x", "endstop_pin", "^^gpio4"),
            ("x", "endstop_pin", "^"),
            (None, "tmc_tx_pin", "!gpio8"),
            (None, "tmc_rx_pin", "!gpio9"),
        ],
    )
    def test_unsupported_pin_notation_is_a_config_error(
        self, tmp_path: Path, table: str | None, key: str, pin: str
    ):
        motion = motion_table()
        (motion if table is None else motion[table])[key] = pin
        config = _load_motion(tmp_path, motion)

        with pytest.raises(ConfigError, match=key) as raised:
            motion_settings(config)

        assert raised.value.sections == {"pnp"}


class TestMotionSettingsLedAndLoadCell:
    """[pnp.motion] の led_pin と [pnp.motion.load_cell] を MCU の設定に直す."""

    def test_absent_stays_absent(self, tmp_path: Path):
        settings = motion_settings(_load_motion(tmp_path, motion_table()))

        assert (settings.led_pin, settings.load_cell) == (None, None)

    def test_kurousagi_values(self, tmp_path: Path):
        motion = motion_table()
        motion["led_pin"] = "gpio17"
        motion["load_cell"] = {
            "sclk_pin": "gpio0",
            "dout_pin": "gpio1",
            "counts_per_gram": 2666.98,
        }

        settings = motion_settings(_load_motion(tmp_path, motion))

        assert settings.led_pin == "gpio17"
        assert settings.load_cell == LoadCellSettings(
            sclk_pin="gpio0",
            dout_pin="gpio1",
            gain_channel=1,
            sample_rate=80,
            counts_per_gram=2666.98,
            inverted=False,
            contact=ContactSettings(
                trigger_force=50.0, release_force=15.0, press_positive=False
            ),
            free_tare_zone=5.0,
        )

    def test_contact_forces_and_direction(self, tmp_path: Path):
        motion = motion_table()
        motion["load_cell"] = {
            "sclk_pin": "gpio0",
            "dout_pin": "gpio1",
            "counts_per_gram": 2666.98,
            "trigger_force": 80.0,
            "release_force": 20.0,
            "press_positive": True,
        }

        load_cell = motion_settings(_load_motion(tmp_path, motion)).load_cell

        assert load_cell is not None
        assert load_cell.contact == ContactSettings(
            trigger_force=80.0, release_force=20.0, press_positive=True
        )

    @pytest.mark.parametrize(
        ("gain", "channel"), [("A-128", 1), ("B-32", 2), ("A-64", 3)]
    )
    def test_gain_is_the_hx711_gain_channel(
        self, tmp_path: Path, gain: str, channel: int
    ):
        motion = motion_table()
        motion["load_cell"] = {
            "sclk_pin": "gpio0",
            "dout_pin": "gpio1",
            "counts_per_gram": 1.0,
            "gain": gain,
        }

        load_cell = motion_settings(_load_motion(tmp_path, motion)).load_cell

        assert load_cell is not None
        assert load_cell.gain_channel == channel

    def test_inverted_orientation(self, tmp_path: Path):
        motion = motion_table()
        motion["load_cell"] = {
            "sclk_pin": "gpio0",
            "dout_pin": "gpio1",
            "counts_per_gram": 1.0,
            "sensor_orientation": "inverted",
        }

        load_cell = motion_settings(_load_motion(tmp_path, motion)).load_cell

        assert load_cell is not None and load_cell.inverted

    @pytest.mark.parametrize(
        ("key", "pin"),
        [
            ("led_pin", "!gpio17"),
            ("load_cell.sclk_pin", "^gpio0"),
            ("load_cell.dout_pin", "!gpio1"),
        ],
    )
    def test_pins_take_no_marks(self, tmp_path: Path, key: str, pin: str):
        motion = motion_table()
        motion["load_cell"] = {
            "sclk_pin": "gpio0",
            "dout_pin": "gpio1",
            "counts_per_gram": 1.0,
        }
        table, _, name = key.rpartition(".")
        (motion[table] if table else motion)[name] = pin
        config = _load_motion(tmp_path, motion)

        with pytest.raises(ConfigError, match=key) as raised:
            motion_settings(config)

        assert raised.value.sections == {"pnp"}


def _use_motion_text(config_path: Path, motion: dict[str, Any]) -> None:
    """[pnp.motion] の表を差し替える."""
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    pnp = document["pnp"]
    assert isinstance(pnp, Table)
    pnp["motion"] = motion
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")


def _load_motion(tmp_path: Path, motion: dict[str, Any]) -> MotionConfig:
    path = tmp_path / "machine.toml"
    path.write_text(
        pnp_motion(MINIMAL_MACHINE_TOML.read_text(encoding="utf-8"), motion),
        encoding="utf-8",
    )
    config = load_motion(path)
    assert config is not None
    return config


class TestFakeMotion:
    def test_open_devices_fake_has_a_motion_stage_from_machine_toml(
        self, config_path: Path, journal_dir: Path
    ):
        # open_motion_stage と send_emergency_stop は root conftest が塞いでいるので、呼べばテストが落ちる
        _use_motion(config_path)
        devices = open_devices(config_path, journal_dir, fake=True)

        stage = devices.motion_stage()
        devices.emergency_stop()

        assert devices.opened_motion_stage() is stage
        assert stage.limits == Limits(
            x=ScalarLimits(0.0, 68.0),
            y=ScalarLimits(0.0, 50.0),
            z=ScalarLimits(-40.0, 0.0),
            v=ScalarLimits(0.0, 100.0),
        )
        assert stage.max_velocity == Velocity(100.0, 100.0, 50.0)
        assert stage.status().homed == frozenset()
        assert stage.status().shutdown_reason is not None

    def test_open_devices_fake_reads_the_load_when_the_load_cell_is_written(
        self, config_path: Path, journal_dir: Path
    ):
        motion = motion_table()
        motion["load_cell"] = {
            "sclk_pin": "gpio0",
            "dout_pin": "gpio1",
            "counts_per_gram": 2666.98,
        }
        _use_motion(config_path, motion)

        stage = open_devices(config_path, journal_dir, fake=True).motion_stage()

        assert stage.status().load == 0.0

    def test_open_devices_fake_without_the_load_cell_has_no_load(
        self, config_path: Path, journal_dir: Path
    ):
        _use_motion(config_path)

        stage = open_devices(config_path, journal_dir, fake=True).motion_stage()

        assert stage.status().load is None

    def test_open_devices_fake_on_klipper_machine_has_no_motion_stage(
        self, config_path: Path, journal_dir: Path
    ):
        devices = open_devices(config_path, journal_dir, fake=True)

        assert not devices.uses_motion_stage()
        assert devices.opened_motion_stage() is None
