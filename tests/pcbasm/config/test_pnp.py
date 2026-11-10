"""`pcbasm.config.pnp` の仕様テスト.

[pnp.servo] はピンセットのサーボのバスと役割（回転・開閉）。検証は machine.toml
の読み込み（load_machine）で行う。
"""

from pathlib import Path

import pytest

from pcbasm.config import ConfigError, load_machine
from pcbasm.config.pnp import PnpConfig, ServoConfig, open_close_span_error
from tests.support.machine_toml import pnp_only
from tests.support.paths import MINIMAL_MACHINE_TOML


def _pnp_machine(tmp_path: Path, pnp: str) -> Path:
    """部品実装だけの検証用 machine.toml の末尾に ``pnp``（[pnp] 以下の表）を足して書く."""
    path = tmp_path / "machine.toml"
    path.write_text(
        f"{pnp_only(MINIMAL_MACHINE_TOML.read_text())}\n{pnp}", encoding="utf-8"
    )
    return path


def _servo(**values: object) -> str:
    """``[pnp.servo]`` の表（port は既定で /dev/ttyACM0）."""
    lines = ['port = "/dev/ttyACM0"'] + [
        f"{key} = {value}" for key, value in values.items()
    ]
    return "[pnp.servo]\n" + "\n".join(lines) + "\n"


class TestPnpServo:
    def test_empty_pnp_section_still_reads_without_servo(self, tmp_path: Path):
        path = tmp_path / "machine.toml"
        path.write_text(pnp_only(MINIMAL_MACHINE_TOML.read_text()), encoding="utf-8")

        machine = load_machine(path)

        assert machine.pnp == PnpConfig(servo=None)

    def test_port_alone_leaves_roles_unset_at_the_default_baudrate(
        self, tmp_path: Path
    ):
        machine = load_machine(_pnp_machine(tmp_path, _servo()))

        assert machine.pnp is not None
        assert machine.pnp.servo == ServoConfig(
            port="/dev/ttyACM0",
            baudrate=1_000_000,
            rotation_id=None,
            open_close_id=None,
            closed_angle=None,
            open_angle=None,
        )

    def test_reads_every_key(self, tmp_path: Path):
        text = _servo(
            baudrate=500000,
            rotation_id=1,
            open_close_id=2,
            closed_angle=87.89,
            open_angle=131.84,
        )

        machine = load_machine(_pnp_machine(tmp_path, text))

        assert machine.pnp is not None
        assert machine.pnp.servo == ServoConfig(
            port="/dev/ttyACM0",
            baudrate=500000,
            rotation_id=1,
            open_close_id=2,
            closed_angle=87.89,
            open_angle=131.84,
        )

    def test_integer_angles_are_read(self, tmp_path: Path):
        machine = load_machine(
            _pnp_machine(tmp_path, _servo(closed_angle=90, open_angle=120))
        )

        assert machine.pnp is not None
        assert machine.pnp.servo is not None
        assert (machine.pnp.servo.closed_angle, machine.pnp.servo.open_angle) == (
            90,
            120,
        )

    def test_port_is_required(self, tmp_path: Path):
        path = _pnp_machine(tmp_path, "[pnp.servo]\nrotation_id = 1\n")

        with pytest.raises(ConfigError) as raised:
            load_machine(path)

        assert "port" in str(raised.value)

    @pytest.mark.parametrize("key", ["rotation_id", "open_close_id"])
    @pytest.mark.parametrize("value", [-1, 254])
    def test_id_outside_0_to_253_is_a_config_error(
        self, tmp_path: Path, key: str, value: int
    ):
        path = _pnp_machine(tmp_path, _servo(**{key: value}))

        with pytest.raises(ConfigError) as raised:
            load_machine(path)

        assert key in str(raised.value)

    @pytest.mark.parametrize("value", [0, 253])
    def test_id_accepts_both_ends_of_the_range(self, tmp_path: Path, value: int):
        machine = load_machine(_pnp_machine(tmp_path, _servo(rotation_id=value)))

        assert machine.pnp is not None
        assert machine.pnp.servo is not None
        assert machine.pnp.servo.rotation_id == value

    def test_same_id_for_both_roles_is_a_config_error(self, tmp_path: Path):
        path = _pnp_machine(tmp_path, _servo(rotation_id=1, open_close_id=1))

        with pytest.raises(ConfigError) as raised:
            load_machine(path)

        assert "pnp.servo" in str(raised.value)

    @pytest.mark.parametrize("key", ["closed_angle", "open_angle"])
    @pytest.mark.parametrize("value", [-1, -0.01, 360, 360.5])
    def test_angle_outside_0_to_360_is_a_config_error(
        self, tmp_path: Path, key: str, value: float
    ):
        path = _pnp_machine(tmp_path, _servo(**{key: value}))

        with pytest.raises(ConfigError) as raised:
            load_machine(path)

        assert key in str(raised.value)

    def test_same_closed_and_open_angle_is_a_config_error(self, tmp_path: Path):
        path = _pnp_machine(tmp_path, _servo(closed_angle=90.0, open_angle=90.0))

        with pytest.raises(ConfigError) as raised:
            load_machine(path)

        assert "pnp.servo" in str(raised.value)

    @pytest.mark.parametrize(("closed", "open_"), [(10.0, 190.5), (350.0, 169.5)])
    def test_closed_to_open_over_half_a_turn_is_crossing_the_wrap(
        self, tmp_path: Path, closed: float, open_: float
    ):
        """360° と 0° の境目をまたぐ開閉は、短い側が逆向きの長い経路に読まれる."""
        path = _pnp_machine(tmp_path, _servo(closed_angle=closed, open_angle=open_))

        with pytest.raises(ConfigError) as raised:
            load_machine(path)

        assert "境目" in str(raised.value)

    @pytest.mark.parametrize(("closed", "open_"), [(10.0, 190.0), (263.67, 83.67)])
    def test_closed_to_open_of_half_a_turn_is_accepted(
        self, tmp_path: Path, closed: float, open_: float
    ):
        machine = load_machine(
            _pnp_machine(tmp_path, _servo(closed_angle=closed, open_angle=open_))
        )

        assert machine.pnp is not None
        assert machine.pnp.servo is not None
        assert machine.pnp.servo.open_angle == open_


class TestOpenCloseSpanError:
    """設定の読み込みとサーボ設定ジョブが共有する、閉じ切り・開き切りの角度の組の規則."""

    @pytest.mark.parametrize(
        ("closed", "opened"),
        [(87.89, 87.98), (114.26, 87.89), (10.0, 190.0), (263.67, 83.67)],
    )
    def test_usable_pair_is_none(self, closed: float, opened: float):
        assert open_close_span_error(closed, opened) is None

    @pytest.mark.parametrize(
        ("closed", "opened", "message"),
        [(87.89, 87.89, "同じ"), (10.0, 190.5, "境目"), (350.0, 169.5, "境目")],
        ids=["same", "wraps_up", "wraps_down"],
    )
    def test_unusable_pair_tells_why(self, closed: float, opened: float, message: str):
        error = open_close_span_error(closed, opened)

        assert error is not None and message in error
