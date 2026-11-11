"""テスト用の machine.toml を機能の節で書き換える."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import tomlkit
from tomlkit.items import Table


def pnp_only(text: str) -> str:
    """``[solder]`` を外して空の ``[pnp]`` を足した内容（部品実装だけの機体）."""
    document = tomlkit.parse(text)
    del document["solder"]
    document.add("pnp", tomlkit.table())
    return tomlkit.dumps(document)


def motion_table() -> dict[str, Any]:
    """Kurousagi（inversed corexy）の値の ``[pnp.motion]`` の表（呼ぶたびに新しい dict）.

    値は deploy/templates/kurousagi/printer.cfg から移したもの。serial だけはテスト用の名前。
    """

    def axis(**values: Any) -> dict[str, Any]:
        return {
            "microsteps": 64,
            "rotation_distance": 32.0,
            "position_min": 0.0,
            "position_endstop": 0.0,
            "homing_speed": 20.0,
            "homing_retract": 5.0,
            "max_velocity": 100.0,
            "max_accel": 500.0,
            "run_current": 0.4,
            **values,
        }

    return {
        "serial": "/dev/ttyACM0",
        "kinematics": "inversed_corexy",
        "tmc_rx_pin": "gpio9",
        "tmc_tx_pin": "gpio8",
        "x": axis(
            step_pin="gpio11",
            dir_pin="!gpio10",
            enable_pin="!gpio12",
            endstop_pin="^gpio4",
            position_max=68.0,
            tmc_address=0,
        ),
        "y": axis(
            step_pin="gpio6",
            dir_pin="!gpio5",
            enable_pin="!gpio7",
            endstop_pin="^gpio3",
            position_max=50.0,
            tmc_address=2,
        ),
        "z": axis(
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
        ),
    }


def pnp_motion(
    text: str, motion: Mapping[str, Any] | None = None, *, klipper: bool = False
) -> str:
    """部品実装だけの機体にして ``[pnp.motion]`` を足した内容（新ファームの XYZ ステージを持つ機体）.

    Args:
        text: 元の machine.toml（``[solder]`` と ``[klipper]`` を持つ検証用のもの）
        motion: ``[pnp.motion]`` の表（省略すると :func:`motion_table` の値）
        klipper: True なら ``[klipper]`` を残す（既定では外す）
    """
    document = tomlkit.parse(pnp_only(text))
    if not klipper:
        del document["klipper"]
    pnp = document["pnp"]
    assert isinstance(pnp, Table)
    pnp.add("motion", motion_table() if motion is None else dict(motion))
    return tomlkit.dumps(document)
