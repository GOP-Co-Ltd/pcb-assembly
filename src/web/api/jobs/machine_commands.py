"""ジョブモードのマシン操作コマンド（jog / home / move / relax / focus_z / move_to_cap）の共有処理."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pcbasm.gcode import GCode
from pcbasm.hal import KlipperClient, XYZStage
from pcbasm.parking import move_to_cap
from pcbasm.vision import load_focus_z
from web.api.jobs.context import JobContext

# wait_for_done (M400) を含む移動完了待ちのため長め（machine_control と同値）
COMMAND_TIMEOUT = 60.0


def handle_machine_command(
    ctx: JobContext,
    klipper: KlipperClient,
    stage: XYZStage,
    command: Mapping[str, Any],
) -> bool:
    """マシン操作パネル（ジョブモード）の WS command を 1 件処理する.

    送る列と timeout は REST の /api/machine-control と同じにする（relax 以外は M400 で到達を待つ）。

    Args:
        ctx: 実行中ジョブのコンテキスト（ログ出力用）
        klipper: 移動コマンド送信先
        stage: XYZ ステージ
        command: WS command（"type" キー必須）

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
                commands = (
                    stage.move(
                        x=distance if axis == "x" else None,
                        y=distance if axis == "y" else None,
                        z=distance if axis == "z" else None,
                        relative=True,
                    )
                    + GCode.wait_for_done()
                )
            case {"type": "home", "axes": list(axes)}:
                commands = (
                    GCode.homing(x="x" in axes, y="y" in axes, z="z" in axes)
                    + GCode.wait_for_done()
                )
            case {"type": "move"}:
                commands = (
                    stage.move(
                        x=command.get("x"), y=command.get("y"), z=command.get("z")
                    )
                    + GCode.wait_for_done()
                )
            case {"type": "relax"}:
                commands = GCode.relax()
            case {"type": "focus_z"}:
                z = load_focus_z(ctx.machine.camera.calibration_file)
                if z is None:
                    raise ValueError(
                        "フォーカスZが取得できません（カメラキャリブレーション未設定）"
                    )
                commands = stage.move(z=z) + GCode.wait_for_done()
            case {"type": "move_to_cap"}:
                cap = ctx.machine.paste_dispenser.nozzle_cap.position
                if cap is None:
                    raise ValueError("ノズルキャップ位置が未記録です")
                commands = move_to_cap(stage, cap) + GCode.wait_for_done()
            case _:
                return False
    except ValueError as exc:
        ctx.log(f"コマンドを実行できません: {exc}")
        return True
    klipper.send_gcode(commands, timeout=COMMAND_TIMEOUT)
    return True
