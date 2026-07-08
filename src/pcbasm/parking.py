"""タスク終了時のノズルキャップ駐機（PRESENT / M84 フォールバック付き）.

ペーストマシンはノズルの乾燥を防ぐため、タスク終了時に必ずノズルキャップ位置へ
移動してから脱力する。キャップ位置が使えない場合は従来どおり
``send_present_or_relax``（PRESENT、無ければ M84）に退避する。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pcbasm import gcode
from pcbasm.config import Machine, NozzleCap
from pcbasm.hal import Klipper
from pcbasm.hal.klipper import PRESENT_TIMEOUT

logger = logging.getLogger(__name__)


def park_or_present(
    klipper: Klipper,
    machine: Machine,
    *,
    warn: Callable[[str], None] | None = None,
    timeout: float = PRESENT_TIMEOUT,
) -> None:
    """ノズルキャップへ駐機し、できなければ PRESENT / M84 にフォールバックする.

    paste マシンでキャップ位置が記録済みなら「Z0 → キャップ XY → キャップ Z →
    M400 → M84」を 1 回の send_gcode で送る。machine_type が欠落・不正、または
    キャップ未記録の場合は警告して ``send_present_or_relax`` に退避する。
    paste 以外のマシンは警告なしで ``send_present_or_relax`` を使う。

    Args:
        klipper: 送信先の Klipper クライアント
        machine: マシン設定
        warn: 警告の通知先（None なら logger.warning）
        timeout: G-code 送信のタイムアウト秒数
    """
    warning = warn if warn is not None else logger.warning
    cap: NozzleCap | None = None
    try:
        machine_type = machine.machine_type
    except (KeyError, ValueError) as exc:
        warning(
            f"machine_type が取得できません: {exc}。"
            "PRESENT / relax (M84) にフォールバックします"
        )
    else:
        # paste 以外（pnp 等）は正常系としてフォールバック（警告なし）
        if machine_type == "paste":
            cap = machine.nozzle_cap
            if cap is None:
                warning(
                    "nozzle_cap が未記録です。"
                    "PRESENT / relax (M84) にフォールバックします"
                )

    if cap is None:
        klipper.send_present_or_relax(warn=warn, timeout=timeout)
        return

    klipper.send_gcode(
        gcode.move_to_cap(cap.x, cap.y, cap.z) + gcode.wait_for_done() + gcode.relax(),
        timeout=timeout,
    )
