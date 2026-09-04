"""ペーストローディングジョブ（ホーミング → 任意位置 → command 駆動ローディング）."""

from __future__ import annotations

from pcbasm.gcode import GCode
from pcbasm.hal import XYZStage
from pcbasm.pasting.applicator import build_applicator
from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.context import JobContext, JobResult
from web.api.jobs.machine_commands import create_command_klipper
from web.api.jobs.pasting.common import (
    LOADING_DEFAULT_AMOUNT,
    LOADING_DEFAULT_RETRACT_ROTATIONS,
    LOADING_DEFAULT_ROTATION_ACCEL,
    LOADING_DEFAULT_ROTATION_RATE,
    LOADING_DEFAULT_ROTATIONS,
    run_loading_loop,
)

_POSITION_PARAMS = (
    ("position_x", "x"),
    ("position_y", "y"),
    ("position_z", "z"),
)


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="loading",
            label="ペーストローディング",
            tab="pasting",
            run=_run_loading,
            params=(
                ParamSpec(
                    "amount",
                    "体積ローディング量",
                    "float",
                    LOADING_DEFAULT_AMOUNT,
                    unit="uL",
                ),
                ParamSpec(
                    "rotations",
                    "回転ローディング回転数",
                    "float",
                    LOADING_DEFAULT_ROTATIONS,
                    unit="rev",
                ),
                ParamSpec(
                    "rate",
                    "回転ローディング角速度",
                    "float",
                    LOADING_DEFAULT_ROTATION_RATE,
                    unit="rev/s",
                ),
                ParamSpec(
                    "accel",
                    "回転ローディング角加速度",
                    "float",
                    LOADING_DEFAULT_ROTATION_ACCEL,
                    unit="rev/s^2",
                ),
                ParamSpec(
                    "retract_rotations",
                    "回転ローディング引き戻し回転数",
                    "float",
                    LOADING_DEFAULT_RETRACT_ROTATIONS,
                    unit="rev",
                ),
                ParamSpec("position_x", "X", "float", unit="mm", optional=True),
                ParamSpec("position_y", "Y", "float", unit="mm", optional=True),
                ParamSpec("position_z", "Z", "float", unit="mm", optional=True),
            ),
            uses_machine=True,
            accepts_commands=True,
            persisted_params=(
                "amount",
                "rotations",
                "rate",
                "accel",
                "retract_rotations",
            ),
            loading_param="amount",
        )
    )


def _run_loading(ctx: JobContext) -> JobResult:
    """全軸 homing と任意位置への移動後、command 駆動ローディングを実行する."""
    klipper = create_command_klipper(ctx.machine)
    stage = XYZStage(klipper.readonly)

    target = {
        axis: float(value)
        for param, axis in _POSITION_PARAMS
        if (value := ctx.params.get(param)) is not None
    }

    ctx.progress("ホーミング")
    ctx.log("全軸ホーミングを実行します")
    klipper.send_gcode(GCode.homing(x=True, y=True, z=True) + GCode.wait_for_done())

    if target:
        position_label = ", ".join(
            f"{axis.upper()}={value:.3f} mm" for axis, value in target.items()
        )
        ctx.progress("ローディング位置へ移動")
        ctx.log(f"ローディング位置へ移動します: {position_label}")
        klipper.send_gcode(
            stage.move(x=target.get("x"), y=target.get("y"), z=target.get("z"))
            + GCode.wait_for_done()
        )

    with build_applicator(klipper, stage, ctx.machine.paste_dispenser) as applicator:
        total = run_loading_loop(ctx, klipper, stage, applicator)
    return JobResult(
        summary=(
            f"押出合計 {total.amount_ul:+.3f} uL / 回転合計 {total.rotations:+.3f} rev"
        )
    )
