"""`pcbasm.parking` の仕様テスト.

`park_or_present` の分岐契約:

- キャップ記録済み → move_to_cap + M400 + M84 を **1 回の send_gcode** で
  送り、フォールバックしない
- キャップ未記録 → warn（"nozzle_cap" を含む文言）+ フォールバック
- キャップ位置が可動域外（ValueError）/ printer.cfg の limits 設定不備
  （KeyError）→ warn + フォールバック（クリーンアップ経路のため例外にしない）
- フォールバックは PRESENT マクロがあれば PRESENT、無いか確かめられなければ warn + M84

`move_to_cap` の G-code 列契約:

- 「Z を 0 へ → キャップ XY へ → キャップ Z へ」の 3 段シーケンス。
  中間セグメントに Z ワードを含めない（Z 先行の意味を壊さない）
- M400 / M84 は含めない（終了時経路が後置で合成する）
- 各セグメントを stage の limits で検証する

Klipper は FakeKlipper を使う（マクロ確認の失敗だけ MockTransport の Moonraker）。

Machine は tmp_path の実 toml から構築する（skill `testing-strategy`）。
"""

from pathlib import Path

import httpx
import pytest

from pcbasm.config import MachineConfig, load_machine
from pcbasm.gcode import GCode
from pcbasm.geometry import Point3d
from pcbasm.hal import MoonrakerKlipper, XYZStage
from pcbasm.hal.fake import FakeKlipper, SentGCode
from pcbasm.parking import move_to_cap, park_or_present
from tests.support.paths import TESTING_DATA_DIR

_CAP = "[paste_dispenser.nozzle_cap]\nx = 10.0\ny = 20.0\nz = 3.5\n"

_CAP_OUT_OF_LIMITS = "[paste_dispenser.nozzle_cap]\nx = 999.0\ny = 20.0\nz = 3.5\n"

_LIMITS_CONFIG = {
    "stepper_x": {"position_min": "0", "position_max": "100"},
    "stepper_y": {"position_min": "0", "position_max": "200"},
    "stepper_z": {"position_min": "0", "position_max": "50"},
    "printer": {"max_velocity": "300"},
}

_PRESENT_MACRO = {"gcode_macro PRESENT": {"gcode": "G1 Y300"}}


def _machine(tmp_path: Path, extra: str = "") -> MachineConfig:
    """検証用 machine.toml の末尾に ``extra`` を足して読む."""
    source = (TESTING_DATA_DIR / "machine.toml").read_text()
    path = tmp_path / "machine.toml"
    path.write_text(f"{source}\n{extra}", encoding="utf-8")
    return load_machine(path)


class TestMoveToCap:
    """move_to_cap の G-code 列と limits 検証の契約."""

    @pytest.fixture
    def stage(self) -> XYZStage:
        return XYZStage(FakeKlipper(_LIMITS_CONFIG))

    @pytest.mark.parametrize(
        ("max_velocity", "expected_feed"),
        [(10, 600.0), (300, 1200.0)],
    )
    def test_sequence_caps_present_speed_at_stage_max_velocity(
        self, max_velocity: int, expected_feed: float
    ):
        limits_config = {
            **_LIMITS_CONFIG,
            "printer": {"max_velocity": str(max_velocity)},
        }
        stage = XYZStage(FakeKlipper(limits_config))

        result = move_to_cap(stage, Point3d(10.0, 20.0, 3.5))

        assert result.to_list() == [
            "G90",
            f"G1 Z0.0 F{expected_feed}",
            f"G1 X10.0 Y20.0 F{expected_feed}",
            f"G1 Z3.5 F{expected_feed}",
        ]

    def test_sequence_contains_no_m400_or_m84(self, stage: XYZStage):
        lines = move_to_cap(stage, Point3d(10.0, 20.0, 3.5)).to_list()

        assert "M400" not in lines
        assert "M84" not in lines

    def test_out_of_limits_cap_raises(self, stage: XYZStage):
        # x=999 は x∈[0,100] の範囲外。
        with pytest.raises(ValueError, match="制限外"):
            move_to_cap(stage, Point3d(999.0, 20.0, 3.5))


class TestParkOrPresent:
    """park_or_present の分岐契約."""

    def test_paste_with_cap_sends_park_sequence_in_single_send_gcode(
        self, tmp_path: Path
    ):
        """Paste + cap 記録済みは Z0→XY→Z→M400→M84 を 1 回の send_gcode で送る."""
        klipper = FakeKlipper(_LIMITS_CONFIG)
        machine = _machine(tmp_path, _CAP)
        warnings: list[str] = []

        park_or_present(klipper, machine, warn=warnings.append, timeout=12.5)

        park = [
            "G90",
            "G1 Z0.0 F1200.0",
            "G1 X10.0 Y20.0 F1200.0",
            "G1 Z3.5 F1200.0",
            "M400",
            "M84",
        ]
        assert klipper.sent == (SentGCode(GCode(park), 12.5),)
        assert warnings == []

    def test_paste_without_cap_warns_and_falls_back_to_present(self, tmp_path: Path):
        """キャップ未記録は "nozzle_cap" を含む警告を出して PRESENT へ退避する."""
        klipper = FakeKlipper({**_LIMITS_CONFIG, **_PRESENT_MACRO})
        machine = _machine(tmp_path)
        warnings: list[str] = []

        park_or_present(klipper, machine, warn=warnings.append, timeout=12.5)

        assert klipper.sent == (SentGCode(GCode("PRESENT"), 12.5),)
        assert any("nozzle_cap" in message for message in warnings)

    def test_cap_out_of_limits_warns_and_falls_back_without_raising(
        self, tmp_path: Path
    ):
        """可動域外のキャップ位置は例外にせず警告して退避する."""
        klipper = FakeKlipper({**_LIMITS_CONFIG, **_PRESENT_MACRO})
        machine = _machine(tmp_path, _CAP_OUT_OF_LIMITS)
        warnings: list[str] = []

        park_or_present(klipper, machine, warn=warnings.append)

        assert klipper.sent_lines == ["PRESENT"]
        assert any("制限外" in message for message in warnings)

    def test_missing_limits_config_warns_and_falls_back_without_raising(
        self, tmp_path: Path
    ):
        """printer.cfg の limits 不備（KeyError）も例外にせず警告して退避する."""
        klipper = FakeKlipper(_PRESENT_MACRO)
        machine = _machine(tmp_path, _CAP)
        warnings: list[str] = []

        park_or_present(klipper, machine, warn=warnings.append)

        assert klipper.sent_lines == ["PRESENT"]
        assert any("printer.cfg" in message for message in warnings)

    def test_falls_back_to_relax_without_present_macro(self, tmp_path: Path):
        klipper = FakeKlipper(_LIMITS_CONFIG)
        warnings: list[str] = []

        park_or_present(klipper, _machine(tmp_path), warn=warnings.append, timeout=12.5)

        assert klipper.sent == (SentGCode(GCode("M84"), 12.5),)
        assert "PRESENT マクロが見つかりません" in warnings[-1]
        assert "M84" in warnings[-1]

    def test_falls_back_to_relax_when_macro_check_fails(self, tmp_path: Path):
        scripts: list[str] = []

        def moonraker(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/printer/objects/query":
                return httpx.Response(503)
            scripts.append(request.url.params["script"])
            return httpx.Response(200, json={"result": "ok"})

        klipper = MoonrakerKlipper(
            "moonraker", 7125, transport=httpx.MockTransport(moonraker)
        )
        warnings: list[str] = []

        park_or_present(klipper, _machine(tmp_path), warn=warnings.append)

        assert scripts == ["M84"]
        assert "確認に失敗しました" in warnings[-1]
        assert "503" in warnings[-1]
