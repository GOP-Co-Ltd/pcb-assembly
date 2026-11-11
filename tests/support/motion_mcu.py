"""Pnp-motion ファームのステージのテストが共有する設定と MCU 役.

疑似端末の向こうの MCU 役（``tests/support/wire_mcu.py``）に、3 軸のリミットスイッチと TMC2209 を持たせる。
軸の値は、テストが速く終わるよう kurousagi より可動域を狭く、原点出しを速くしてある。
向きは kurousagi と同じで、X と Y は負の向き、Z は正の向きに原点を出す。
"""

from pcbasm.hal.mcu.settings import AxisSettings, McuMotionSettings
from pcbasm.hal.mcu.tmc2209 import TmcSettings
from tests.support.mcu_dictionary import dictionary_json
from tests.support.wire_mcu import WireMcu

STEPS_PER_MM = 80.0

# 物理的な位置（ステップ）。X は 10 mm、Y は 5 mm、Z は -5 mm の位置から始まる
START_POSITIONS = [800, 400, -400]

# リミットスイッチ。X と Y は物理的な 0 以下、Z は 0 以上で押される（ピンの値は 1）
SWITCHES = [
    {"pin": 4, "axis": 0, "at": 0, "above": False},
    {"pin": 3, "axis": 1, "at": 0, "above": False},
    {"pin": 25, "axis": 2, "at": 0, "above": True},
]


def axis_settings(
    *,
    step_pin: str,
    dir_pin: str,
    enable_pin: str,
    endstop_pin: str,
    position_min: float,
    position_max: float,
    homing_positive: bool,
    tmc_address: int,
    homing_retract: float = 1.0,
) -> AxisSettings:
    return AxisSettings(
        step_pin=step_pin,
        dir_pin=dir_pin,
        invert_dir=True,
        enable_pin=enable_pin,
        invert_enable=True,
        endstop_pin=endstop_pin,
        endstop_pull_up=True,
        endstop_invert=False,
        steps_per_mm=STEPS_PER_MM,
        position_min=position_min,
        position_max=position_max,
        position_endstop=0.0,
        homing_positive=homing_positive,
        homing_speed=50.0,
        second_homing_speed=25.0,
        homing_retract=homing_retract,
        max_velocity=100.0,
        max_accel=1000.0,
        tmc_address=tmc_address,
        tmc=TmcSettings(run_current=0.4, microsteps=16),
    )


def motion_settings(device: str) -> McuMotionSettings:
    return McuMotionSettings(
        device=device,
        kinematics="cartesian",
        x=axis_settings(
            step_pin="gpio11",
            dir_pin="gpio10",
            enable_pin="gpio12",
            endstop_pin="gpio4",
            position_min=0.0,
            position_max=20.0,
            homing_positive=False,
            tmc_address=0,
        ),
        y=axis_settings(
            step_pin="gpio6",
            dir_pin="gpio5",
            enable_pin="gpio7",
            endstop_pin="gpio3",
            position_min=0.0,
            position_max=20.0,
            homing_positive=False,
            tmc_address=2,
        ),
        z=axis_settings(
            step_pin="gpio19",
            dir_pin="gpio28",
            enable_pin="gpio2",
            endstop_pin="gpio25",
            position_min=-10.0,
            position_max=0.0,
            homing_positive=True,
            tmc_address=1,
        ),
        tmc_rx_pin="gpio9",
        tmc_tx_pin="gpio8",
        report_interval=0.05,
    )


def open_wire(**motion: object) -> WireMcu:
    """3 軸のスイッチと TMC2209 を持つ MCU 役を起動する（``motion`` で模擬の設定を上書きする）."""
    return WireMcu(
        dictionary_json(),
        motion={"positions": START_POSITIONS, "switches": SWITCHES, **motion},
    )
