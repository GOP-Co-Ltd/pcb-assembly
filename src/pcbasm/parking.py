"""タスク終了時のノズルキャップ駐機（PRESENT / M84 フォールバック付き）.

ペーストマシンはノズルの乾燥を防ぐため、タスク終了時に必ずノズルキャップ位置へ移動してから脱力する。

キャップ位置が使えないとき（未記録・可動域外・printer.cfg の不備）は PRESENT に退避する。

PRESENT マクロが無ければ M84 で脱力する。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pcbasm.config import MachineConfig
from pcbasm.gcode import PRESENT_MACRO, GCode
from pcbasm.geometry import Point3d
from pcbasm.hal import KlipperClient, Speed, XYZStage

logger = logging.getLogger(__name__)

PRESENT_TIMEOUT = 30.0

# ノズルキャップ移動速度の上限 [mm/s]（PRESENT マクロの F1200 と同速）
CAP_PARK_VELOCITY = 20.0


def move_to_cap(stage: XYZStage, cap: Point3d) -> GCode:
    """ノズルキャップ位置への移動コマンドを生成する.

    Z を 0 へ退避してからキャップ XY へ移動し、最後にキャップ Z へ下ろす。
    各セグメントは stage の limits で検証する。M400 / M84 は含めない。

    Args:
        stage: 可動域検証に使う XYZ ステージ
        cap: ノズルキャップ位置

    Returns:
        移動の GCode（G90 + 3 段の G1）

    Raises:
        ValueError: キャップ位置が可動域外の場合
        KeyError: printer.cfg に limits 用のセクション・キーが無い場合
    """
    speed = Speed.absolute(min(CAP_PARK_VELOCITY, stage.max_velocity))
    return (
        GCode("G90")
        + stage.move(z=0.0, speed=speed)
        + stage.move(x=cap.x, y=cap.y, speed=speed)
        + stage.move(z=cap.z, speed=speed)
    )


def park_or_present(
    klipper: KlipperClient,
    machine: MachineConfig,
    *,
    warn: Callable[[str], None] | None = None,
    timeout: float = PRESENT_TIMEOUT,
) -> None:
    """ノズルキャップへ駐機し、できなければ PRESENT / M84 にフォールバックする.

    キャップ位置が記録済みなら「Z0 → キャップ XY → キャップ Z → M400 → M84」を
    1 回の send_gcode で送る。キャップ未記録、キャップ位置が可動域外、
    printer.cfg の limits 設定不備の場合は警告して PRESENT（マクロが無ければ M84）に退避する。

    Args:
        klipper: 送信先の Klipper クライアント
        machine: マシン設定
        warn: 警告の通知先（None なら logger.warning）
        timeout: G-code 送信のタイムアウト秒数
    """
    warning = warn if warn is not None else logger.warning
    cap = machine.paste_dispenser.nozzle_cap.position
    if cap is None:
        warning("nozzle_cap が未記録です。PRESENT / relax (M84) にフォールバックします")

    commands: GCode | None = None
    if cap is not None:
        # クリーンアップ経路のため、可動域外（ValueError）や printer.cfg の
        # limits 設定不備（KeyError）は例外にせずフォールバックする。
        # 接続系のエラーはフォールバック先も失敗するだけなので伝播させる
        try:
            commands = move_to_cap(XYZStage(klipper), cap)
        except (ValueError, KeyError) as exc:
            warning(
                f"ノズルキャップへ移動できません: {exc}。"
                "PRESENT / relax (M84) にフォールバックします"
            )

    if commands is None:
        _present_or_relax(klipper, warning, timeout)
        return

    klipper.send_gcode(
        commands + GCode.wait_for_done() + GCode.relax(),
        timeout=timeout,
    )


def _present_or_relax(
    klipper: KlipperClient, warning: Callable[[str], None], timeout: float
) -> None:
    """PRESENT マクロがあれば実行し、無ければ警告して M84 にフォールバックする."""
    try:
        has_present = f"gcode_macro {PRESENT_MACRO}" in klipper.get_config()
    except Exception as exc:
        warning(
            f"{PRESENT_MACRO} マクロ確認に失敗しました: {exc}。"
            "relax (M84) にフォールバックします"
        )
    else:
        if has_present:
            klipper.send_gcode(GCode.present(), timeout=timeout)
            return
        warning(
            f"{PRESENT_MACRO} マクロが見つかりません。"
            "relax (M84) にフォールバックします"
        )

    klipper.send_gcode(GCode.relax(), timeout=timeout)
