"""machine.toml を通さずに `SolderConfig` を組む（塗布の単体テストが共用する）."""

from __future__ import annotations

from typing import Any

from pcbasm.config.solder import (
    DispenserConfig,
    NozzleConfig,
    NozzleOffsetConfig,
    SolderConfig,
    SolderMotionConfig,
)
from pcbasm.pasting.params import PasteParams


def solder_config(
    *,
    defaults: PasteParams,
    rotations_per_ul: float = 45.0,
    nozzle_diameter: float = 0.34,
    nozzle_outer_diameter: float | None = None,
    max_dispense_rate: float = 5.0,
    dispense_accel: float = 10.0,
    retract_amount: float = 10.0,
    retract_rate: float = 10.0,
    retract_accel_factor: float = 2.0,
    max_fill_speed: float = 2.0,
    lift_height: float = 2.0,
    micro_line_settle_seconds: float = 0.1,
    post_dispense_settle_seconds: float = 0.1,
    **solder: Any,
) -> SolderConfig:
    """テストに要る値だけを渡して SolderConfig を組む（ノズルはカメラ中心にある）.

    キーワードは ``[solder]`` の下位表の項目名（ノズルの径だけは ``nozzle_`` を付ける）。
    ``solder`` は ``[solder]`` の直下と下位表（``paste_density``、``auto`` など）をそのまま渡す。
    """
    return SolderConfig(
        dispenser=DispenserConfig(
            rotations_per_ul=rotations_per_ul,
            max_dispense_rate=max_dispense_rate,
            dispense_accel=dispense_accel,
            retract_amount=retract_amount,
            retract_rate=retract_rate,
            retract_accel_factor=retract_accel_factor,
        ),
        nozzle=NozzleConfig(
            diameter=nozzle_diameter,
            outer_diameter=nozzle_outer_diameter,
            offset=NozzleOffsetConfig(x=0.0, y=0.0),
        ),
        motion=SolderMotionConfig(
            max_fill_speed=max_fill_speed,
            lift_height=lift_height,
            micro_line_settle_seconds=micro_line_settle_seconds,
            post_dispense_settle_seconds=post_dispense_settle_seconds,
        ),
        defaults=defaults,
        **solder,
    )
