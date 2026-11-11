"""`deploy/hardware.sh` の MCU firmware の選び方の契約テスト.

機種の雛形に [pnp] があれば pnp-motion、なければ Klipper を書き込む。
"""

import subprocess

import pytest

from pcbasm.paths import PROJECT_ROOT

HARDWARE = PROJECT_ROOT / "deploy" / "hardware.sh"


def run(command: str, answer: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{HARDWARE}" && {command}'],
        input=answer,
        capture_output=True,
        text=True,
        check=False,
    )


class TestMachineFirmware:
    @pytest.mark.parametrize(
        ("machine_type", "firmware"),
        [
            ("usaremino_pnp", "pnp-motion"),
            ("usaremino", "klipper"),
            ("kurousagi", "klipper"),
        ],
    )
    def test_firmware_follows_the_machine_template(self, machine_type, firmware):
        result = run(f"machine_firmware {machine_type}")

        assert result.returncode == 0, result.stderr
        assert result.stdout == firmware + "\n"

    def test_unknown_machine_type_fails(self):
        result = run("machine_firmware no_such_machine")

        assert result.returncode != 0
        assert "no_such_machine" in result.stderr


class TestChooseFirmware:
    @pytest.mark.parametrize(
        ("answer", "firmware"), [("1\n", "pnp-motion"), ("2\n", "")]
    )
    def test_asks_only_whether_to_write_the_machine_firmware(self, answer, firmware):
        result = run("choose_firmware usaremino_pnp", answer)

        assert result.returncode == 0, result.stderr
        assert result.stdout == firmware
        assert "pnp-motion を書き込む（機種 usaremino_pnp）" in result.stderr


class TestMain:
    def test_requires_a_machine_type(self):
        result = run("main")

        assert result.returncode != 0
        assert "機種を指定してください" in result.stderr
