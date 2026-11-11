"""`pcbasm.machine.devices` の仕様テスト.

Moonraker は `moonraker` fixture（httpx.MockTransport で応える FakeMoonraker）で置き換える。
"""

import threading
from datetime import date
from pathlib import Path

import httpx
import pytest
from shapely import Point

from pcbasm.config import ConfigError
from pcbasm.config.klipper import KlipperConfig
from pcbasm.hal import FrameHub, GamepadState, ServoMode, XYZStage
from pcbasm.hal.fake import (
    FAKE_MACHINE_PRINTER_CONFIG,
    FakeCamera,
    FakeGamepad,
    FakeKlipper,
    FakeServoBus,
    ServoCommand,
)
from pcbasm.machine.devices import Devices, open_devices
from pcbasm.machine.journal import Journal
from pcbasm.vision import Image
from tests.support import polling
from tests.support.moonraker import FakeMoonraker
from tests.support.paths import FAKE_CAMERA_IMAGE
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
