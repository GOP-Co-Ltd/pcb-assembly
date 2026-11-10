"""コントローラでピンセットを操作するジョブ（中止されるまで続ける）."""

from __future__ import annotations

from typing import NoReturn

from pcbasm.pnp.gamepad_control import GamepadControl
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition
from web.api.jobs.runtime.context import JobContext


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="gamepad_control",
            label="コントローラでピンセット操作",
            run=_run_gamepad_control,
            # サーボだけを動かし、XYZ には触れない。終了時のノズルキャップ駐機をせず、
            # そのため機体スピーカーの完了音も鳴らない
            uses_machine=False,
        )
    )


def _run_gamepad_control(ctx: JobContext) -> NoReturn:
    """Xbox コントローラの入力でピンセットを動かす（LT=開閉、LB / RB=回転）.

    中止で終わる。終わるときは（失敗でも）回転を止め、トルクは保持する（つまんだ部品を落とさない）。
    """
    tweezer = ctx.devices.tweezer()
    gamepad = ctx.devices.connect_gamepad()
    try:
        ctx.progress("操作中（終えるときは中止を押す）")
        ctx.log("LT: 開閉（押し込むほど開く） / LB・RB: 回転（押している間回る）")
        GamepadControl(gamepad, tweezer).run(ctx.checkpoint)
    finally:
        gamepad.close()
