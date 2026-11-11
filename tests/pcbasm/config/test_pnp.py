"""`pcbasm.config.pnp` の仕様テスト.

[pnp.servo] はピンセットのサーボのバスと役割（回転・開閉）、[pnp.motion] は新ファームの MCU と XYZ
ステージ、[pnp.gamepad] はコントローラのデッドゾーン。検証は machine.toml の読み込み
（load_machine）で行う。[klipper] と [pnp.motion] のどちらかは要る。
"""

from pathlib import Path

import attrs
import pytest
import tomlkit

from pcbasm.config import ConfigError, load_machine, load_motion
from pcbasm.config.klipper import KlipperConfig
from pcbasm.config.pnp import (
    GamepadConfig,
    MotionAxisConfig,
    MotionConfig,
    PnpConfig,
    ServoConfig,
    open_close_span_error,
)
from pcbasm.paths import PROJECT_ROOT
from tests.support.machine_toml import motion_table, pnp_motion, pnp_only
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

    def test_rotation_speed_defaults_to_45_degrees_per_second(self, tmp_path: Path):
        machine = load_machine(_pnp_machine(tmp_path, _servo()))

        assert machine.pnp is not None
        assert machine.pnp.servo is not None
        assert machine.pnp.servo.rotation_speed == 45.0

    @pytest.mark.parametrize("value", [0.1, 90, 720])
    def test_rotation_speed_is_read(self, tmp_path: Path, value: float):
        machine = load_machine(_pnp_machine(tmp_path, _servo(rotation_speed=value)))

        assert machine.pnp is not None
        assert machine.pnp.servo is not None
        assert machine.pnp.servo.rotation_speed == value

    @pytest.mark.parametrize("value", [0, -10, 720.5, '"fast"'])
    def test_rotation_speed_outside_0_to_720_is_a_config_error(
        self, tmp_path: Path, value: object
    ):
        path = _pnp_machine(tmp_path, _servo(rotation_speed=value))

        with pytest.raises(ConfigError) as raised:
            load_machine(path)

        assert "rotation_speed" in str(raised.value)

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


def _motion_machine(
    tmp_path: Path, motion: dict | None = None, *, klipper: bool = False
) -> Path:
    """[pnp.motion]（省略すると kurousagi の値）を持つ部品実装だけの machine.toml を書く."""
    path = tmp_path / "machine.toml"
    path.write_text(
        pnp_motion(MINIMAL_MACHINE_TOML.read_text(), motion, klipper=klipper),
        encoding="utf-8",
    )
    return path


def _motion_with(axis: str | None = None, **values: object) -> dict:
    """Kurousagi の [pnp.motion] の値を置き換えた表（``axis`` を渡すとその軸の表を置き換える）."""
    motion = motion_table()
    (motion if axis is None else motion[axis]).update(values)
    return motion


def _kurousagi_axis(**values: object) -> MotionAxisConfig:
    defaults: dict = {
        "microsteps": 64,
        "full_steps_per_rotation": 200,
        "rotation_distance": 32.0,
        "position_min": 0.0,
        "position_endstop": 0.0,
        "homing_positive": False,
        "homing_speed": 20.0,
        "second_homing_speed": None,
        "homing_retract": 5.0,
        "max_velocity": 100.0,
        "max_accel": 500.0,
        "run_current": 0.4,
        "hold_current": None,
        "sense_resistor": 0.110,
    }
    return MotionAxisConfig(**{**defaults, **values})


_KUROUSAGI_X = _kurousagi_axis(
    step_pin="gpio11",
    dir_pin="!gpio10",
    enable_pin="!gpio12",
    endstop_pin="^gpio4",
    position_max=68.0,
    tmc_address=0,
)
_KUROUSAGI_Y = _kurousagi_axis(
    step_pin="gpio6",
    dir_pin="!gpio5",
    enable_pin="!gpio7",
    endstop_pin="^gpio3",
    position_max=50.0,
    tmc_address=2,
)
_KUROUSAGI_Z = _kurousagi_axis(
    step_pin="gpio19",
    dir_pin="!gpio28",
    enable_pin="!gpio2",
    endstop_pin="^gpio25",
    position_min=-40.0,
    position_max=0.0,
    homing_positive=True,
    homing_speed=10.0,
    homing_retract=3.0,
    max_velocity=50.0,
    max_accel=250.0,
    tmc_address=1,
)


class TestPnpMotion:
    """[pnp.motion] は新ファーム（pnp-motion）の MCU と XYZ ステージ。値は printer.cfg と同じ意味."""

    def test_reads_the_kurousagi_values(self, tmp_path: Path):
        machine = load_machine(_motion_machine(tmp_path))

        assert machine.pnp is not None
        assert machine.pnp.motion == MotionConfig(
            serial="/dev/ttyACM0",
            kinematics="inversed_corexy",
            tmc_rx_pin="gpio9",
            tmc_tx_pin="gpio8",
            x=_KUROUSAGI_X,
            y=_KUROUSAGI_Y,
            z=_KUROUSAGI_Z,
        )

    def test_steps_per_mm_follows_the_drive_train(self):
        """GT2 16T（32 mm/回転）、200 歩/回転、64 マイクロステップで 400 steps/mm."""
        assert _KUROUSAGI_X.steps_per_mm == 400.0
        assert attrs.evolve(_KUROUSAGI_X, microsteps=16).steps_per_mm == 100.0

    def test_machine_without_the_table_has_no_motion(self, tmp_path: Path):
        path = tmp_path / "machine.toml"
        path.write_text(pnp_only(MINIMAL_MACHINE_TOML.read_text()), encoding="utf-8")

        machine = load_machine(path)

        assert machine.pnp is not None
        assert machine.pnp.motion is None

    @pytest.mark.parametrize("kinematics", ["cartesian", "corexy", "inversed_corexy"])
    def test_accepts_every_kinematics(self, tmp_path: Path, kinematics: str):
        machine = load_machine(
            _motion_machine(tmp_path, _motion_with(kinematics=kinematics))
        )

        assert machine.pnp is not None and machine.pnp.motion is not None
        assert machine.pnp.motion.kinematics == kinematics

    def test_unknown_kinematics_is_a_config_error(self, tmp_path: Path):
        path = _motion_machine(tmp_path, _motion_with(kinematics="delta"))

        with pytest.raises(ConfigError) as raised:
            load_machine(path)

        assert "pnp.motion.kinematics" in str(raised.value)
        assert raised.value.sections == {"pnp"}

    @pytest.mark.parametrize(
        "key", ["serial", "kinematics", "tmc_rx_pin", "tmc_tx_pin", "x", "y", "z"]
    )
    def test_table_keys_are_required(self, tmp_path: Path, key: str):
        motion = motion_table()
        del motion[key]

        with pytest.raises(ConfigError, match=f"必須の項目がありません @ .*{key}"):
            load_machine(_motion_machine(tmp_path, motion))

    @pytest.mark.parametrize(
        "key",
        [
            "step_pin",
            "dir_pin",
            "enable_pin",
            "endstop_pin",
            "rotation_distance",
            "position_min",
            "position_max",
            "position_endstop",
            "max_velocity",
            "max_accel",
            "tmc_address",
            "run_current",
        ],
    )
    def test_axis_keys_without_a_default_are_required(self, tmp_path: Path, key: str):
        motion = motion_table()
        del motion["y"][key]

        with pytest.raises(ConfigError, match=f"pnp.motion.y.{key}"):
            load_machine(_motion_machine(tmp_path, motion))

    def test_unknown_axis_key_is_a_config_error(self, tmp_path: Path):
        path = _motion_machine(tmp_path, _motion_with("x", uart_pin="gpio9"))

        with pytest.raises(ConfigError, match="未知のキーです: uart_pin"):
            load_machine(path)

    def test_optional_axis_keys_are_read(self, tmp_path: Path):
        path = _motion_machine(
            tmp_path,
            _motion_with(
                "z",
                full_steps_per_rotation=400,
                second_homing_speed=2.5,
                hold_current=0.2,
                sense_resistor=0.075,
            ),
        )

        machine = load_machine(path)

        assert machine.pnp is not None and machine.pnp.motion is not None
        assert machine.pnp.motion.z == attrs.evolve(
            _KUROUSAGI_Z,
            full_steps_per_rotation=400,
            second_homing_speed=2.5,
            hold_current=0.2,
            sense_resistor=0.075,
        )

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("microsteps", 0),
            ("microsteps", 3),
            ("microsteps", 512),
            ("full_steps_per_rotation", 0),
            ("rotation_distance", 0),
            ("rotation_distance", -32.0),
            ("homing_speed", 0),
            ("second_homing_speed", 0),
            ("homing_retract", -1.0),
            ("max_velocity", 0),
            ("max_accel", 0),
            ("tmc_address", -1),
            ("tmc_address", 4),
            ("run_current", 0),
            ("hold_current", 0),
            ("sense_resistor", 0),
        ],
    )
    def test_axis_values_out_of_range_are_config_errors(
        self, tmp_path: Path, key: str, value: object
    ):
        path = _motion_machine(tmp_path, _motion_with("z", **{key: value}))

        with pytest.raises(ConfigError) as raised:
            load_machine(path)

        assert key in str(raised.value)
        assert "pnp.motion.z" in str(raised.value)

    @pytest.mark.parametrize("value", [1, 2, 128, 256])
    def test_microsteps_are_powers_of_two_up_to_256(self, tmp_path: Path, value: int):
        machine = load_machine(
            _motion_machine(tmp_path, _motion_with("z", microsteps=value))
        )

        assert machine.pnp is not None and machine.pnp.motion is not None
        assert machine.pnp.motion.z.microsteps == value

    @pytest.mark.parametrize("value", [0, 3])
    def test_tmc_address_accepts_both_ends(self, tmp_path: Path, value: int):
        machine = load_machine(
            _motion_machine(tmp_path, _motion_with("z", tmc_address=value))
        )

        assert machine.pnp is not None and machine.pnp.motion is not None
        assert machine.pnp.motion.z.tmc_address == value

    @pytest.mark.parametrize(
        ("position_min", "position_max"), [(10.0, 10.0), (20.0, 10.0)]
    )
    def test_empty_travel_is_a_config_error(
        self, tmp_path: Path, position_min: float, position_max: float
    ):
        path = _motion_machine(
            tmp_path,
            _motion_with(
                "x",
                position_min=position_min,
                position_max=position_max,
                position_endstop=10.0,
            ),
        )

        with pytest.raises(ConfigError, match="position_min"):
            load_machine(path)

    @pytest.mark.parametrize("endstop", [-0.1, 68.1])
    def test_endstop_outside_the_travel_is_a_config_error(
        self, tmp_path: Path, endstop: float
    ):
        """ファームは位置の設定で可動域の外を受け付けず shutdown する."""
        path = _motion_machine(tmp_path, _motion_with("x", position_endstop=endstop))

        with pytest.raises(ConfigError) as raised:
            load_machine(path)

        assert "position_endstop" in str(raised.value)
        assert "pnp.motion.x" in str(raised.value)

    @pytest.mark.parametrize("endstop", [0.0, 68.0])
    def test_endstop_on_either_end_of_the_travel_is_accepted(
        self, tmp_path: Path, endstop: float
    ):
        machine = load_machine(
            _motion_machine(tmp_path, _motion_with("x", position_endstop=endstop))
        )

        assert machine.pnp is not None and machine.pnp.motion is not None
        assert machine.pnp.motion.x.position_endstop == endstop

    def test_axis_speed_over_the_firmware_limit_is_a_config_error(self, tmp_path: Path):
        """ファームの max_velocity は 500000 steps/s まで（400 steps/mm で 1250 mm/s）."""
        path = _motion_machine(tmp_path, _motion_with("z", max_velocity=1250.5))

        with pytest.raises(ConfigError, match="max_velocity"):
            load_machine(path)

    def test_axis_accel_over_the_firmware_limit_is_a_config_error(self, tmp_path: Path):
        """ファームの accel は 10⁹ steps/s² まで（400 steps/mm で 2.5e6 mm/s²）."""
        path = _motion_machine(tmp_path, _motion_with("z", max_accel=2.6e6))

        with pytest.raises(ConfigError, match="max_accel"):
            load_machine(path)

    @pytest.mark.parametrize("kinematics", ["corexy", "inversed_corexy"])
    def test_corexy_needs_the_same_steps_per_mm_on_x_and_y(
        self, tmp_path: Path, kinematics: str
    ):
        """Corexy は X と Y の 1 ステップをモーターの 1 ステップとして写す."""
        motion = _motion_with(kinematics=kinematics)
        motion["y"]["rotation_distance"] = 40.0

        with pytest.raises(ConfigError) as raised:
            load_machine(_motion_machine(tmp_path, motion))

        assert "steps/mm" in str(raised.value)
        assert raised.value.sections == {"pnp"}

    def test_cartesian_allows_different_steps_per_mm(self, tmp_path: Path):
        motion = _motion_with(kinematics="cartesian")
        motion["y"]["rotation_distance"] = 40.0

        machine = load_machine(_motion_machine(tmp_path, motion))

        assert machine.pnp is not None and machine.pnp.motion is not None
        assert machine.pnp.motion.y.steps_per_mm == 320.0

    @pytest.mark.parametrize(
        ("kinematics", "accepted"),
        [("cartesian", True), ("corexy", False), ("inversed_corexy", False)],
    )
    def test_corexy_limits_the_sum_of_x_and_y_speeds(
        self, tmp_path: Path, kinematics: str, accepted: bool
    ):
        """Corexy のモーターは X と Y の速さの和で回る（和が 500000 steps/s まで）."""
        # 400 steps/mm で 700 mm/s は 280000 steps/s。1 軸なら収まり、和は超える
        motion = _motion_with(kinematics=kinematics)
        motion["x"]["max_velocity"] = 700.0
        motion["y"]["max_velocity"] = 700.0
        path = _motion_machine(tmp_path, motion)

        if accepted:
            load_machine(path)
        else:
            with pytest.raises(ConfigError, match="max_velocity"):
                load_machine(path)


class TestPnpGamepad:
    """[pnp.gamepad] はコントローラのスティックのデッドゾーン（設定ページに出る）."""

    def test_defaults_when_the_table_is_absent(self, tmp_path: Path):
        path = tmp_path / "machine.toml"
        path.write_text(pnp_only(MINIMAL_MACHINE_TOML.read_text()), encoding="utf-8")

        machine = load_machine(path)

        assert machine.pnp is not None
        assert machine.pnp.gamepad == GamepadConfig(deadzone_xy=0.15, deadzone_z=0.15)

    def test_reads_the_table(self, tmp_path: Path):
        path = _pnp_machine(
            tmp_path, "[pnp.gamepad]\ndeadzone_xy = 0.1\ndeadzone_z = 0.2\n"
        )

        machine = load_machine(path)

        assert machine.pnp is not None
        assert machine.pnp.gamepad == GamepadConfig(deadzone_xy=0.1, deadzone_z=0.2)

    @pytest.mark.parametrize("key", ["deadzone_xy", "deadzone_z"])
    @pytest.mark.parametrize("value", [0, 0.5])
    def test_accepts_both_ends_of_the_range(
        self, tmp_path: Path, key: str, value: float
    ):
        machine = load_machine(
            _pnp_machine(tmp_path, f"[pnp.gamepad]\n{key} = {value}\n")
        )

        assert machine.pnp is not None
        assert getattr(machine.pnp.gamepad, key) == value

    @pytest.mark.parametrize("key", ["deadzone_xy", "deadzone_z"])
    @pytest.mark.parametrize("value", [-0.01, 0.51, '"wide"', "true"])
    def test_outside_0_to_half_is_a_config_error(
        self, tmp_path: Path, key: str, value: str | float
    ):
        path = _pnp_machine(tmp_path, f"[pnp.gamepad]\n{key} = {value}\n")

        with pytest.raises(ConfigError) as raised:
            load_machine(path)

        assert key in str(raised.value)
        assert "pnp.gamepad" in str(raised.value)

    def test_unknown_key_is_a_config_error(self, tmp_path: Path):
        path = _pnp_machine(tmp_path, "[pnp.gamepad]\nmax_speed = 1.0\n")

        with pytest.raises(ConfigError, match="未知のキーです: max_speed"):
            load_machine(path)


class TestKlipperOrMotion:
    """XYZ は Klipper（[klipper]）か新ファーム（[pnp.motion]）で動かす。どちらかは要る."""

    def test_motion_machine_needs_no_klipper(self, tmp_path: Path):
        machine = load_machine(_motion_machine(tmp_path))

        assert machine.klipper is None
        assert machine.pnp is not None and machine.pnp.motion is not None

    def test_klipper_and_motion_may_be_written_together(self, tmp_path: Path):
        machine = load_machine(_motion_machine(tmp_path, klipper=True))

        assert machine.klipper == KlipperConfig(host="127.0.0.1", port=7126)
        assert machine.pnp is not None and machine.pnp.motion is not None

    def test_solder_machine_keeps_reading_klipper(self):
        machine = load_machine(MINIMAL_MACHINE_TOML)

        assert machine.klipper == KlipperConfig(host="127.0.0.1", port=7126)

    @pytest.mark.parametrize("capability", ["solder", "pnp"])
    def test_neither_is_a_config_error(self, tmp_path: Path, capability: str):
        text = MINIMAL_MACHINE_TOML.read_text()
        if capability == "pnp":
            text = pnp_only(text)
        document = tomlkit.parse(text)
        del document["klipper"]
        path = tmp_path / "machine.toml"
        path.write_text(tomlkit.dumps(document), encoding="utf-8")

        with pytest.raises(ConfigError) as raised:
            load_machine(path)

        assert r"[klipper]" in str(raised.value)
        assert r"[pnp.motion]" in str(raised.value)
        assert "klipper" in raised.value.sections


class TestLoadMotion:
    """設定エラーの間も緊急停止を送れるよう、[pnp.motion] の表だけを読む."""

    def test_reads_motion_even_when_the_rest_is_invalid(self, tmp_path: Path):
        path = _motion_machine(tmp_path)
        path.write_text(
            f"{path.read_text()}\n[settle]\nmove_sec = -1.0\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError):
            load_machine(path)

        motion = load_motion(path)

        assert motion is not None
        assert motion.z == _KUROUSAGI_Z

    def test_is_none_without_the_table(self, tmp_path: Path):
        path = tmp_path / "machine.toml"
        path.write_text(pnp_only(MINIMAL_MACHINE_TOML.read_text()), encoding="utf-8")

        assert load_motion(MINIMAL_MACHINE_TOML) is None
        assert load_motion(path) is None

    def test_invalid_table_is_a_config_error(self, tmp_path: Path):
        path = _motion_machine(tmp_path, _motion_with("x", position_endstop=100.0))

        with pytest.raises(ConfigError, match="pnp.motion.x"):
            load_motion(path)

    def test_unparsable_toml_is_a_config_error(self, tmp_path: Path):
        path = tmp_path / "machine.toml"
        path.write_text('[pnp.motion]\nserial = "unterminated\n', encoding="utf-8")

        with pytest.raises(ConfigError, match="TOML として読めません"):
            load_motion(path)


class TestKurousagiTemplate:
    """部品実装機 kurousagi の雛形は、Klipper を使わず新ファームで XYZ を動かす."""

    def test_moves_xyz_with_the_new_firmware(self):
        template = PROJECT_ROOT / "deploy" / "templates" / "kurousagi" / "machine.toml"

        machine = load_machine(template)

        assert machine.klipper is None
        assert machine.pnp is not None and machine.pnp.motion is not None
        motion = machine.pnp.motion
        assert motion.serial.startswith("/dev/serial/by-id/")
        assert attrs.evolve(motion, serial="/dev/ttyACM0") == MotionConfig(
            serial="/dev/ttyACM0",
            kinematics="inversed_corexy",
            tmc_rx_pin="gpio9",
            tmc_tx_pin="gpio8",
            x=_KUROUSAGI_X,
            y=_KUROUSAGI_Y,
            z=_KUROUSAGI_Z,
        )
