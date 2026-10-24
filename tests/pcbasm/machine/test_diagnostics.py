"""`pcbasm.machine.diagnostics` の仕様テスト.

Klipper は FakeKlipper と、不通の FakeMoonraker で置き換える。
"""

import contextlib
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np
import pytest

from pcbasm.config import load_machine
from pcbasm.hal import MoonrakerKlipper
from pcbasm.hal.fake import (
    FAKE_AUDIO_DEVICES,
    FAKE_MACHINE_PRINTER_CONFIG,
    FakeCamera,
    FakeKlipper,
)
from pcbasm.machine.diagnostics import check_audio, check_camera, check_klipper
from pcbasm.vision import CameraCalibration, Image
from tests.support.moonraker import FakeMoonraker
from tests.support.paths import TESTING_DATA_DIR

MACHINE = load_machine(TESTING_DATA_DIR / "machine.toml")


def _frame(width: int, height: int) -> Image:
    return Image(np.zeros((height, width, 3), dtype=np.uint8))


class TestCheckKlipper:
    def test_all_sections_present(self):
        klipper = FakeKlipper(FAKE_MACHINE_PRINTER_CONFIG)

        checks = check_klipper(klipper, MACHINE)

        assert [(check.name, check.ok) for check in checks] == [
            ("Klipper の接続", True),
            ("ステージ", True),
            ("ディスペンサー", True),
            ("プローブ", True),
            ("駐機", True),
        ]
        assert checks[0].detail == "192.168.1.100:7125"
        assert checks[1].detail == "X 0〜300 / Y 0〜300 / Z -5〜50 mm、最大 100 mm/s"
        assert checks[3].detail == "[load_cell_probe] があります"
        assert klipper.sent == ()

    @pytest.mark.parametrize(
        ("section", "name"),
        [
            ("stepper_z", "ステージ"),
            ("printer", "ステージ"),
            ("manual_stepper paste_dispenser", "ディスペンサー"),
            ("output_pin air_pump", "ディスペンサー"),
            ("load_cell_probe", "プローブ"),
            ("gcode_macro PRESENT", "駐機"),
        ],
    )
    def test_missing_section_fails_only_the_part_that_needs_it(
        self, section: str, name: str
    ):
        config = {
            key: value
            for key, value in FAKE_MACHINE_PRINTER_CONFIG.items()
            if key != section
        }

        checks = check_klipper(FakeKlipper(config), MACHINE)

        failed = [check for check in checks if not check.ok]
        assert [check.name for check in failed] == [name]
        assert f"[{section}]" in failed[0].detail

    def test_klipper_shutdown_fails_the_connection(self):
        """Moonraker には届くが、MCU が切れて Klipper が shutdown している."""
        moonraker = FakeMoonraker()
        moonraker.config = FAKE_MACHINE_PRINTER_CONFIG
        moonraker.webhooks = {
            "state": "shutdown",
            "state_message": "Lost communication with MCU 'mcu'",
        }
        klipper = MoonrakerKlipper(
            "192.168.1.100", 7125, transport=httpx.MockTransport(moonraker.handle)
        )

        checks = check_klipper(klipper, MACHINE)

        assert (checks[0].ok, checks[0].detail) == (
            False,
            "192.168.1.100:7125 の Klipper が shutdown です: "
            "Lost communication with MCU 'mcu'",
        )
        assert all(check.ok for check in checks[1:])

    def test_unreachable_moonraker_skips_printer_cfg(self):
        moonraker = FakeMoonraker()
        moonraker.up = False
        klipper = MoonrakerKlipper(
            "192.168.1.100", 7125, transport=httpx.MockTransport(moonraker.handle)
        )

        checks = check_klipper(klipper, MACHINE)

        assert [(check.name, check.ok) for check in checks] == [
            ("Klipper の接続", False)
        ]
        assert "192.168.1.100:7125 に接続できません" in checks[0].detail


class TestCheckCamera:
    @pytest.fixture
    def calibration_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "calibration.json"
        CameraCalibration(
            pixel_per_mm=20.0,
            square_size_mm=1.5,
            mean_distance_px=30.0,
            std_distance_px=0.1,
            resolution=(1280, 800),
            crop_size=(600, 600),
            calibrated_at=datetime(2026, 10, 24),
        ).save(path)
        return path

    def test_resolution_matches_calibration(self, calibration_file: Path):
        camera = FakeCamera([_frame(1280, 800)])

        check = check_camera(lambda: contextlib.nullcontext(camera), calibration_file)

        assert (check.ok, check.detail) == (True, "1280x800（校正と一致）")

    def test_resolution_differs_from_calibration(self, calibration_file: Path):
        """[camera] の幅と高さを変えたのに校正し直していない."""
        camera = FakeCamera([_frame(640, 480)])

        check = check_camera(lambda: contextlib.nullcontext(camera), calibration_file)

        assert (check.ok, check.detail) == (
            False,
            "カメラ 640x480 が校正 1280x800 と違います",
        )

    def test_frame_does_not_arrive(self, calibration_file: Path):
        camera = FakeCamera(
            [TimeoutError("フレーム待ちがタイムアウトしました"), _frame(1280, 800)]
        )

        check = check_camera(lambda: contextlib.nullcontext(camera), calibration_file)

        assert not check.ok
        assert "カメラから撮れません" in check.detail

    def test_not_calibrated_yet(self, tmp_path: Path):
        camera = FakeCamera([_frame(1280, 800)])

        check = check_camera(
            lambda: contextlib.nullcontext(camera), tmp_path / "missing.json"
        )

        assert not check.ok
        assert "校正ファイルを読めません" in check.detail


class TestCheckAudio:
    def test_configured_device_is_listed(self):
        check = check_audio(FAKE_AUDIO_DEVICES, "plughw:CARD=sndrpihifiberry,DEV=0")

        assert (check.ok, check.detail) == (
            True,
            "snd_rpi_hifiberry_dac, HifiBerry DAC HiFi pcm5102a-hifi-0",
        )

    def test_configured_device_is_missing(self):
        """DAC が認識されていない（dtoverlay の設定漏れなど）."""
        devices = [
            device for device in FAKE_AUDIO_DEVICES if "hifiberry" not in device.name
        ]

        check = check_audio(devices, "plughw:CARD=sndrpihifiberry,DEV=0")

        assert (check.ok, check.detail) == (
            False,
            "出力デバイス plughw:CARD=sndrpihifiberry,DEV=0 が見つかりません",
        )

    def test_system_default_without_any_card(self):
        """システム既定は aplay -L の結果によらず候補に入るので、それだけでは合格にしない."""
        check = check_audio(FAKE_AUDIO_DEVICES[:1], "default")

        assert (check.ok, check.detail) == (
            False,
            "ALSA の再生デバイスが見つかりません",
        )
