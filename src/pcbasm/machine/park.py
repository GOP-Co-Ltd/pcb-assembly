"""ノズルキャップへの駐機（キャップ → PRESENT → M84）.

ペーストマシンはノズルの乾燥を防ぐため、タスクの終わりにノズルキャップ位置へ移動してから脱力する。

キャップ位置が使えないとき（未記録・可動域外・printer.cfg の不備）は PRESENT に退避する。

PRESENT マクロが無ければ M84 で脱力する。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pcbasm.gcode import PRESENT_MACRO, GCode
from pcbasm.geometry import Point3d
from pcbasm.hal import KlipperClient, XYZStage

logger = logging.getLogger(__name__)

# printer.cfg で PRESENT マクロを定義する section
PRESENT_SECTION = f"gcode_macro {PRESENT_MACRO}"

# ジョブ終了時の駐機の timeout [s]
PRESENT_TIMEOUT = 30.0


def park(
    klipper: KlipperClient,
    stage: XYZStage,
    cap: Point3d | None,
    *,
    warn: Callable[[str], None] = logger.warning,
) -> None:
    """ノズルキャップへ移動して到達を待ち、脱力する（M84）.

    キャップへ行けないときは理由を warn へ渡し、代わりに PRESENT（マクロが無ければ M84）へ退避する。
    片付けの経路なので可動域外や printer.cfg の不備は例外にしない。接続の失敗は退避先でも失敗するだけなので伝える。
    """
    if cap is None:
        reason = "ノズルキャップ位置が未記録です"
    else:
        try:
            commands = stage.travel_to(cap) + GCode.wait_for_done() + GCode.relax()
        except (ValueError, KeyError) as exc:
            reason = f"ノズルキャップへ移動できません: {exc}"
        else:
            klipper.send_gcode(commands)
            return
    warn(f"{reason}。PRESENT / relax (M84) にフォールバックします")
    _present_or_relax(klipper, warn)


def _present_or_relax(klipper: KlipperClient, warn: Callable[[str], None]) -> None:
    """PRESENT マクロがあれば実行し、無ければ警告して M84 にフォールバックする."""
    try:
        has_present = PRESENT_SECTION in klipper.get_config()
    except Exception as exc:
        warn(
            f"{PRESENT_MACRO} マクロ確認に失敗しました: {exc}。"
            "relax (M84) にフォールバックします"
        )
    else:
        if has_present:
            klipper.send_gcode(GCode.present())
            return
        warn(
            f"{PRESENT_MACRO} マクロが見つかりません。"
            "relax (M84) にフォールバックします"
        )
    klipper.send_gcode(GCode.relax())
