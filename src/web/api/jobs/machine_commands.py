"""ジョブモードのマシン操作コマンド（jog / home / move / relax / focus_z / move_to_cap）の共有処理."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pcbasm.config import Machine
from pcbasm.gcode import GCode
from pcbasm.hal import Klipper, XYZStage
from pcbasm.parking import move_to_cap
from web.api.jobs.context import JobContext

# wait_for_done (M400) を含む移動完了待ちのため長め（machine_control と同値）
COMMAND_TIMEOUT = 60.0


def create_command_klipper(machine: Machine) -> Klipper:
    """選択マシンの設定で移動コマンド用 Klipper クライアントを生成する."""
    return Klipper(
        host=machine.klipper.host,
        port=machine.klipper.port,
        timeout=COMMAND_TIMEOUT,
    )


def handle_machine_command(
    ctx: JobContext,
    klipper: Klipper,
    stage: XYZStage,
    command: Mapping[str, Any],
    *,
    focus_z: float | None,
) -> bool:
    """マシン操作パネル（ジョブモード）の WS command を 1 件処理する.

    Args:
        ctx: 実行中ジョブのコンテキスト（ログ出力用）
        klipper: 移動コマンド送信先
        stage: XYZ ステージ
        command: WS command（"type" キー必須）
        focus_z: focus_z コマンドの移動先 Z。None なら log のみで移動しない

    Returns:
        コマンドを処理したら True（実行失敗の ValueError は log して True）。
        未知 type は False（log は呼び出し側の責務）
    """
    try:
        match command:
            case {"type": "jog", "axis": str(axis), "dist": dist} if axis in (
                "x",
                "y",
                "z",
            ):
                distance = float(dist)
                klipper.send_gcode(
                    stage.move(
                        x=distance if axis == "x" else None,
                        y=distance if axis == "y" else None,
                        z=distance if axis == "z" else None,
                        relative=True,
                    )
                    + GCode.wait_for_done()
                )
            case {"type": "home", "axes": list(axes)}:
                klipper.send_gcode(
                    GCode.homing(x="x" in axes, y="y" in axes, z="z" in axes)
                    + GCode.wait_for_done()
                )
            case {"type": "move"}:
                klipper.send_gcode(
                    stage.move(
                        x=command.get("x"), y=command.get("y"), z=command.get("z")
                    )
                    + GCode.wait_for_done()
                )
            case {"type": "relax"}:
                klipper.send_gcode(GCode.relax())
            case {"type": "focus_z"}:
                if focus_z is None:
                    ctx.log("フォーカスZが未設定のため移動しません")
                else:
                    klipper.send_gcode(stage.move(z=focus_z) + GCode.wait_for_done())
            case {"type": "move_to_cap"}:
                cap = ctx.machine.nozzle_cap
                if cap is None:
                    ctx.log("ノズルキャップ位置が未記録のため移動しません")
                else:
                    klipper.send_gcode(move_to_cap(stage, cap) + GCode.wait_for_done())
            case _:
                return False
    except ValueError as exc:
        ctx.log(f"コマンドを実行できません: {exc}")
    return True
