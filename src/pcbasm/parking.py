"""タスク終了時のノズルキャップ駐機（PRESENT / M84 フォールバック付き）.

ペーストマシンはノズルの乾燥を防ぐため、タスク終了時に必ずノズルキャップ位置へ
移動してから脱力する。キャップ位置が使えない場合（未記録・可動域外・
printer.cfg の limits 設定不備）は従来どおり ``send_present_or_relax``
（PRESENT、無ければ M84）に退避する。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pcbasm import gcode
from pcbasm.config import Machine, NozzleCap
from pcbasm.hal import Klipper, Speed, XYZStage
from pcbasm.hal.klipper import PRESENT_TIMEOUT

logger = logging.getLogger(__name__)

# ノズルキャップ移動速度の上限 [mm/s]（PRESENT マクロの F1200 と同速）
CAP_PARK_VELOCITY = 20.0


def move_to_cap(stage: XYZStage, cap: NozzleCap) -> gcode.GCode:
    """ノズルキャップ位置への移動コマンドを生成する.

    Z を 0 へ退避してからキャップ XY へ移動し、最後にキャップ Z へ下ろす。
    各セグメントは stage の limits で検証する。M400 / M84 は含めない。

    Args:
        stage: 可動域検証に使う XYZ ステージ
        cap: ノズルキャップ位置

    Returns:
        移動のGCode（G90 + 3 段の G1）

    Raises:
        ValueError: キャップ位置が可動域外の場合
        KeyError: printer.cfg に limits 用のセクション・キーが無い場合
    """
    speed = Speed.absolute(min(CAP_PARK_VELOCITY, stage.max_velocity))
    return (
        gcode.GCode("G90")
        + stage.move(z=0.0, speed=speed)
        + stage.move(x=cap.x, y=cap.y, speed=speed)
        + stage.move(z=cap.z, speed=speed)
    )


def park_or_present(
    klipper: Klipper,
    machine: Machine,
    *,
    warn: Callable[[str], None] | None = None,
    timeout: float = PRESENT_TIMEOUT,
) -> None:
    """ノズルキャップへ駐機し、できなければ PRESENT / M84 にフォールバックする.

    paste マシンでキャップ位置が記録済みなら「Z0 → キャップ XY → キャップ Z →
    M400 → M84」を 1 回の send_gcode で送る。machine_type が欠落・不正、
    キャップ未記録、キャップ位置が可動域外、printer.cfg の limits 設定不備の
    場合は警告して ``send_present_or_relax`` に退避する。
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

    commands: gcode.GCode | None = None
    if cap is not None:
        # クリーンアップ経路のため、可動域外（ValueError）や printer.cfg の
        # limits 設定不備（KeyError）は例外にせずフォールバックする。
        # 接続系のエラーはフォールバック先も失敗するだけなので伝播させる
        try:
            commands = move_to_cap(XYZStage(klipper.readonly), cap)
        except (ValueError, KeyError) as exc:
            warning(
                f"ノズルキャップへ移動できません: {exc}。"
                "PRESENT / relax (M84) にフォールバックします"
            )

    if commands is None:
        klipper.send_present_or_relax(warn=warn, timeout=timeout)
        return

    klipper.send_gcode(
        commands + gcode.wait_for_done() + gcode.relax(),
        timeout=timeout,
    )
