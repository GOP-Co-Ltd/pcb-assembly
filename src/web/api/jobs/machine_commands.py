"""対話ジョブの WS command のうち、マシン操作パネルの命令を処理する."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from pcbasm.hal import KlipperClient, XYZStage
from pcbasm.machine.commands import COMMAND_TIMEOUT, to_gcode
from web.api.jobs.context import JobContext
from web.api.machine_commands import to_command
from web.contract.models import MachineCommandRequest

# command の type がマシン操作でない（ジョブ固有の command）ときの検証エラー
_NOT_MACHINE_COMMAND = frozenset({"union_tag_invalid", "union_tag_not_found"})


def handle_machine_command(
    ctx: JobContext,
    klipper: KlipperClient,
    stage: XYZStage,
    command: Mapping[str, Any],
) -> bool:
    """対話ジョブの WS command がマシン操作なら、REST と同じ列を同じ timeout で送る.

    Returns:
        マシン操作なら True（形の誤りや実行できない理由は log してジョブを続ける）。
        type がマシン操作でない command は False（log は呼び出し側の責務）。
        送信の失敗はジョブを失敗させるため握りつぶさない
    """
    try:
        request = MachineCommandRequest.model_validate(command)
    except ValidationError as exc:
        errors = exc.errors()
        if errors[0]["type"] in _NOT_MACHINE_COMMAND:
            return False
        reason = ", ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errors
        )
        ctx.log(f"コマンドを実行できません: {reason}")
        return True
    try:
        commands = to_gcode(to_command(request), stage, lambda: ctx.machine)
    except ValueError as exc:
        ctx.log(f"コマンドを実行できません: {exc}")
        return True
    klipper.send_gcode(commands, timeout=COMMAND_TIMEOUT)
    return True
