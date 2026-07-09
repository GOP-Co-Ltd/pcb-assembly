"""`pcbasm.parking` の仕様テスト.

`park_or_present` の分岐契約:

- paste + キャップ記録済み → move_to_cap + M400 + M84 を **1 回の send_gcode** で
  送り、フォールバック（send_present_or_relax）は呼ばない
- キャップ未記録 → warn（"nozzle_cap" を含む文言）+ send_present_or_relax
- machine_type != "paste"（pnp）→ 警告なしで send_present_or_relax
- machine_type 欠落 / 不正 → warn（"machine_type" を含む文言）+
  send_present_or_relax（クリーンアップ経路のため例外にしない）
- キャップ位置が可動域外（ValueError）/ printer.cfg の limits 設定不備
  （KeyError）→ warn + send_present_or_relax（同上）

`move_to_cap` の G-code 列契約:

- 「Z を 0 へ → キャップ XY へ → キャップ Z へ」の 3 段シーケンス。
  中間セグメントに Z ワードを含めない（Z 先行の意味を壊さない）
- M400 / M84 は含めない（終了時経路が後置で合成する）
- 各セグメントを stage の limits で検証する

Klipper は自前 HAL クラスのため mocker.Mock（または get_config をパッチした
実 Klipper）を使い、Machine は tmp_path の実 toml から構築する
（skill `testing-strategy`）。
"""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from pcbasm.config import Machine, NozzleCap
from pcbasm.gcode import GCode
from pcbasm.hal import XYZStage
from pcbasm.hal.klipper import Klipper
from pcbasm.parking import move_to_cap, park_or_present

_PASTE_WITH_CAP = """\
machine_type = "paste"

[nozzle_cap]
x = 10.0
y = 20.0
z = 3.5
"""

_PASTE_WITH_CAP_OUT_OF_LIMITS = """\
machine_type = "paste"

[nozzle_cap]
x = 999.0
y = 20.0
z = 3.5
"""

_PASTE_WITHOUT_CAP = 'machine_type = "paste"\n'

_PNP = 'machine_type = "pnp"\n'

_MISSING_MACHINE_TYPE = "[klipper]\n"

_INVALID_MACHINE_TYPE = 'machine_type = "sander"\n'

_LIMITS_CONFIG = {
    "stepper_x": {"position_min": "0", "position_max": "100"},
    "stepper_y": {"position_min": "0", "position_max": "200"},
    "stepper_z": {"position_min": "0", "position_max": "50"},
    "printer": {"max_velocity": "300"},
}


def _machine(tmp_path: Path, content: str) -> Machine:
    path = tmp_path / "machine.toml"
    path.write_text(content, encoding="utf-8")
    return Machine(path)


class TestMoveToCap:
    """move_to_cap の G-code 列と limits 検証の契約."""

    @pytest.fixture
    def stage(self, mocker: MockerFixture) -> XYZStage:
        klipper = Klipper()
        mocker.patch.object(klipper.readonly, "get_config", return_value=_LIMITS_CONFIG)
        return XYZStage(klipper.readonly)

    def test_sequence_is_g90_then_z0_then_xy_then_z_at_present_speed(
        self, stage: XYZStage
    ):
        result = move_to_cap(stage, NozzleCap(x=10.0, y=20.0, z=3.5))

        assert result.to_list() == [
            "G90",
            "G1 Z0.0 F1200.0",
            "G1 X10.0 Y20.0 F1200.0",
            "G1 Z3.5 F1200.0",
        ]

    def test_sequence_contains_no_m400_or_m84(self, stage: XYZStage):
        lines = move_to_cap(stage, NozzleCap(x=10.0, y=20.0, z=3.5)).to_list()

        assert "M400" not in lines
        assert "M84" not in lines

    def test_out_of_limits_cap_raises(self, stage: XYZStage):
        # x=999 は x∈[0,100] の範囲外。
        with pytest.raises(ValueError, match="制限外"):
            move_to_cap(stage, NozzleCap(x=999.0, y=20.0, z=3.5))


class TestParkOrPresent:
    """park_or_present の分岐契約."""

    def test_paste_with_cap_sends_park_sequence_in_single_send_gcode(
        self, mocker: MockerFixture, tmp_path: Path
    ):
        """Paste + cap 記録済みは Z0→XY→Z→M400→M84 を 1 回の send_gcode で送る."""
        klipper = mocker.Mock()
        klipper.readonly.get_config.return_value = _LIMITS_CONFIG
        machine = _machine(tmp_path, _PASTE_WITH_CAP)
        warnings: list[str] = []

        park_or_present(klipper, machine, warn=warnings.append, timeout=12.5)

        klipper.send_gcode.assert_called_once()
        sent = GCode(klipper.send_gcode.call_args.args[0])
        assert sent.to_list() == [
            "G90",
            "G1 Z0.0 F1200.0",
            "G1 X10.0 Y20.0 F1200.0",
            "G1 Z3.5 F1200.0",
            "M400",
            "M84",
        ]
        assert klipper.send_gcode.call_args.kwargs.get("timeout") == 12.5
        klipper.send_present_or_relax.assert_not_called()
        assert warnings == []

    def test_paste_without_cap_warns_and_falls_back_to_present(
        self, mocker: MockerFixture, tmp_path: Path
    ):
        """キャップ未記録は "nozzle_cap" を含む警告を出して PRESENT にフォールバックする."""
        klipper = mocker.Mock()
        machine = _machine(tmp_path, _PASTE_WITHOUT_CAP)
        warnings: list[str] = []

        park_or_present(klipper, machine, warn=warnings.append)

        klipper.send_present_or_relax.assert_called_once()
        klipper.send_gcode.assert_not_called()
        assert any("nozzle_cap" in message for message in warnings)

    def test_pnp_machine_falls_back_to_present_without_warning(
        self, mocker: MockerFixture, tmp_path: Path
    ):
        """Pnp マシンは正常系としてキャップ駐機せず PRESENT 経路を使う（警告なし）."""
        klipper = mocker.Mock()
        machine = _machine(tmp_path, _PNP)
        warnings: list[str] = []

        park_or_present(klipper, machine, warn=warnings.append)

        klipper.send_present_or_relax.assert_called_once()
        klipper.send_gcode.assert_not_called()
        assert warnings == []

    def test_missing_machine_type_warns_and_falls_back_without_raising(
        self, mocker: MockerFixture, tmp_path: Path
    ):
        """machine_type 欠落はクリーンアップ経路のため例外にせず警告 + フォールバックする."""
        klipper = mocker.Mock()
        machine = _machine(tmp_path, _MISSING_MACHINE_TYPE)
        warnings: list[str] = []

        park_or_present(klipper, machine, warn=warnings.append)

        klipper.send_present_or_relax.assert_called_once()
        klipper.send_gcode.assert_not_called()
        assert any("machine_type" in message for message in warnings)

    def test_invalid_machine_type_warns_and_falls_back_without_raising(
        self, mocker: MockerFixture, tmp_path: Path
    ):
        """machine_type 不正値も例外にせず警告 + フォールバックする."""
        klipper = mocker.Mock()
        machine = _machine(tmp_path, _INVALID_MACHINE_TYPE)
        warnings: list[str] = []

        park_or_present(klipper, machine, warn=warnings.append)

        klipper.send_present_or_relax.assert_called_once()
        klipper.send_gcode.assert_not_called()
        assert any("machine_type" in message for message in warnings)

    def test_cap_out_of_limits_warns_and_falls_back_without_raising(
        self, mocker: MockerFixture, tmp_path: Path
    ):
        """可動域外のキャップ位置は例外にせず警告 + フォールバックする."""
        klipper = mocker.Mock()
        klipper.readonly.get_config.return_value = _LIMITS_CONFIG
        machine = _machine(tmp_path, _PASTE_WITH_CAP_OUT_OF_LIMITS)
        warnings: list[str] = []

        park_or_present(klipper, machine, warn=warnings.append)

        klipper.send_present_or_relax.assert_called_once()
        klipper.send_gcode.assert_not_called()
        assert any("制限外" in message for message in warnings)

    def test_missing_limits_config_warns_and_falls_back_without_raising(
        self, mocker: MockerFixture, tmp_path: Path
    ):
        """printer.cfg の limits 設定不備（KeyError）も例外にせず警告 + フォールバックする."""
        klipper = mocker.Mock()
        klipper.readonly.get_config.return_value = {}
        machine = _machine(tmp_path, _PASTE_WITH_CAP)
        warnings: list[str] = []

        park_or_present(klipper, machine, warn=warnings.append)

        klipper.send_present_or_relax.assert_called_once()
        klipper.send_gcode.assert_not_called()
        assert any("printer.cfg" in message for message in warnings)
