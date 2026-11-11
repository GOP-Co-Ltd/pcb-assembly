"""コントローラと画面の仮想スティックで XYZ とピンセットを操作するジョブ（新ファームの機体だけ。中止されるまで続ける）."""

from __future__ import annotations

from typing import NoReturn

from pydantic import ValidationError

from pcbasm.config import ConfigError
from pcbasm.config.pnp import GamepadConfig
from pcbasm.hal import Gamepad, GamepadError, MotionStage, ServoError, Tweezer
from pcbasm.pnp.gamepad_control import GamepadControl
from pcbasm.pnp.stick import StickInput
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.steps import handle_motion_command, validation_reason
from web.contract.models import StickCommand


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="gamepad_motion",
            label="コントローラ操作（XYZ とピンセット）",
            run=_run_gamepad_motion,
            # 仮想スティックとマシン操作パネルの命令を WS command で受ける
            accepts_commands=True,
            # XYZ を動かす。終わるとステージを止め（新ファームの機体の駐機）、完了音を鳴らす
            uses_machine=True,
        )
    )


def _run_gamepad_motion(ctx: JobContext) -> NoReturn:
    """コントローラ（左スティック = XY、右スティックの縦 = Z、LT = 開閉、LB / RB = 回転）と画面の仮想スティックで操作する.

    ジョブの中でマシン操作パネルの命令（原点出し・ジョグ・移動・脱力）も受け、届いた順に実行する。
    コントローラが無くても、途中で外れても、画面の仮想スティックで続ける。[pnp.servo] が使えなければピンセットは操作しない。
    中止で終わる。終わるときは（失敗でも）ステージと回転を止め、トルクは保持する（つまんだ部品を落とさない）。
    """
    stage = ctx.devices.motion_stage()
    tweezer = _tweezer(ctx)
    gamepad = _gamepad(ctx)
    pnp = ctx.machine.pnp
    control = GamepadControl(
        gamepad,
        tweezer,
        motion=stage,
        settings=pnp.gamepad if pnp is not None else GamepadConfig(),
    )
    try:
        ctx.progress("操作中（終えるときは中止を押す）")
        ctx.log(
            "左スティック: XY / 右スティックの上下: Z / LT: 開閉 / LB・RB: 回転。"
            "原点出しをしていない軸は動かない"
        )
        control.run(lambda: _take_commands(ctx, stage, control))
    finally:
        if gamepad is not None:
            gamepad.close()


def _tweezer(ctx: JobContext) -> Tweezer | None:
    """ピンセット（[pnp.servo] が無いか、サーボと話せなければ None）."""
    try:
        return ctx.devices.tweezer()
    except (ConfigError, ServoError) as exc:
        ctx.log(f"ピンセットは操作しません（{exc}）")
        return None


def _gamepad(ctx: JobContext) -> Gamepad | None:
    """コントローラ（見つからなければ None）."""
    try:
        return ctx.devices.connect_gamepad()
    except GamepadError as exc:
        ctx.log(f"コントローラがありません（{exc}）。画面の仮想スティックで操作します")
        return None


def _take_commands(
    ctx: JobContext, stage: MotionStage, control: GamepadControl
) -> None:
    """中止を確かめ、届いた command を読み切る（周期ごとに GamepadControl.run が呼ぶ）.

    stick は最後の 1 件だけを仮想スティックへ渡す。マシン操作の命令は届いた順に実行する。
    マシン操作の命令（Home / Jog / Move / Relax のどれでも）を実行した周期の stick は捨て、
    仮想スティックを中立にする。原点出しや移動の間に溜まった stick は古く、受け取った時刻から
    期限を数えると、送り手がもういなくても動いてしまうため。
    ブロックしない Jog でも捨てるので、仮想パッドを押しながらジョグすると、次の stick が
    届くまで XY の速度がいったん 0 になる（安全側）。

    Raises:
        JobAborted: 中止を求められた場合
    """
    ctx.checkpoint()
    stick = None
    handled_panel = False
    while (command := ctx.next_command(timeout=0)) is not None:
        if command.get("type") == "stick":
            stick = command
        elif handle_motion_command(ctx, stage, command):
            handled_panel = True
        else:
            ctx.log(f"知らないコマンドです: {command.get('type')!r}")
    if handled_panel:
        control.set_virtual(StickInput())
        return
    if stick is None:
        return
    try:
        latest = StickCommand.model_validate(stick)
    except ValidationError as exc:
        ctx.log(f"仮想スティックの入力を読めません: {validation_reason(exc)}")
        return
    control.set_virtual(StickInput(x=latest.x, y=latest.y, z=latest.z))
