"""`pcbasm.parking.park_or_present` の仕様テスト.

計画書 memory/agents/implementation-planner/nozzle-cap-parking.md
「src/pcbasm/parking.py」節が契約:

- paste + キャップ記録済み → move_to_cap + M400 + M84 を **1 回の send_gcode** で
  送り、フォールバック（send_present_or_relax）は呼ばない
- キャップ未記録 → warn（"nozzle_cap" を含む文言）+ send_present_or_relax
- machine_type != "paste"（pnp）→ 警告なしで send_present_or_relax
- machine_type 欠落 / 不正 → warn（"machine_type" を含む文言）+
  send_present_or_relax（クリーンアップ経路のため例外にしない）

Klipper は自前 HAL クラスのため mocker.Mock を使い、Machine は tmp_path の
実 toml から構築する（skill `testing-strategy`）。
"""

from pathlib import Path

from pytest_mock import MockerFixture

from pcbasm.config import Machine
from pcbasm.gcode import GCode
from pcbasm.parking import park_or_present

_PASTE_WITH_CAP = """\
machine_type = "paste"

[nozzle_cap]
x = 10.0
y = 20.0
z = 3.5
"""

_PASTE_WITHOUT_CAP = 'machine_type = "paste"\n'

_PNP = 'machine_type = "pnp"\n'

_MISSING_MACHINE_TYPE = "[klipper]\n"

_INVALID_MACHINE_TYPE = 'machine_type = "sander"\n'


def _machine(tmp_path: Path, content: str) -> Machine:
    path = tmp_path / "machine.toml"
    path.write_text(content, encoding="utf-8")
    return Machine(path)


class TestParkOrPresent:
    """park_or_present の分岐契約."""

    def test_paste_with_cap_sends_park_sequence_in_single_send_gcode(
        self, mocker: MockerFixture, tmp_path: Path
    ):
        """Paste + cap 記録済みは Z0→XY→Z→M400→M84 を 1 回の send_gcode で送る."""
        klipper = mocker.Mock()
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
