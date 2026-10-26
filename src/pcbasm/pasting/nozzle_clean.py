"""ノズル先端のクリーニング（パージ → 十字往復でこすり）.

ノズル先端の外周に残ったペーストは固まると最初の吐出量を乱し、塊のまま pad へ落ちる。
クリーニング位置でその場に少量パージして先端内部の固まりを押し出してから、シリコン
クリーナーへ押しつけて十字に往復し、外周の付着分を拭き取る。

塗布ジョブでこれを呼ぶのは、初回パージを撮って塗布が写らなかったときだけ（詰まりの
疑い）。判断とパージのやり直しは
:func:`~pcbasm.pasting.purge_check.purge_with_cleaning` が持つ。

位置は :class:`~pcbasm.config.dispenser.NozzleCleanConfig` の ``position`` がマシン座標で持ち、基板とは
無関係なので board 補正を通さない。接近はノズルキャップへの駐機と同じ :meth:`~pcbasm.hal.XYZStage.travel_to` で、
移動 G-code を組む関数は ``M400`` / ``M84`` を含めない。ジョブの開始時に :func:`resolve_nozzle_clean` が組んで可動域を確かめ、
送信は :func:`clean_nozzle` が行う。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import attrs

from pcbasm.config import MachineConfig
from pcbasm.config.solder import NozzleCleanConfig
from pcbasm.gcode import GCode
from pcbasm.geometry import Path, Point3d
from pcbasm.hal import KlipperClient, Speed, XYZStage
from pcbasm.hal.stage import TRAVEL_Z
from pcbasm.pasting.applicator import PasteApplicator
from pcbasm.pasting.labels import clean_position_label

logger = logging.getLogger(__name__)

# クリーニングを行わない理由（WebUI の「未記録」表示と文言を揃える）
_NOT_RECORDED = "ノズルクリーニング: 位置が未記録のためスキップします"


def _press_point(position: Point3d, clean: NozzleCleanConfig) -> Point3d:
    """クリーニング面から ``press_depth`` だけ押し込んだ点（Z は 0 が上・負が下）."""
    return Point3d(position.x, position.y, position.z - clean.press_depth)


def wipe_points(position: Point3d, clean: NozzleCleanConfig) -> Path:
    """十字往復の点列を返す（マシン座標、全点が押し込み Z）.

    中心から X 方向へ ``±stroke`` 往復し、続いて Y 方向へ ``±stroke`` 往復する。これを
    ``passes`` 回繰り返す。各 pass の末尾の中心が次 pass の始点を兼ねるので中心は重複
    しない。始点と終点はどちらも中心なので、退避は XY を動かさずに済む。

    ``stroke`` か ``passes`` が 0 ならこすらない設定なので、中心 1 点だけを返す。
    """
    center = _press_point(position, clean)
    if clean.stroke <= 0 or clean.passes <= 0:
        return Path((center,))

    stroke = clean.stroke
    points = [center]
    for _ in range(clean.passes):
        points += [
            center + Point3d(stroke, 0.0, 0.0),
            center - Point3d(stroke, 0.0, 0.0),
            center,
            center + Point3d(0.0, stroke, 0.0),
            center - Point3d(0.0, stroke, 0.0),
            center,
        ]
    return Path(points)


def wipe_gcode(stage: XYZStage, position: Point3d, clean: NozzleCleanConfig) -> GCode:
    """面 Z から押し込んで十字往復する移動コマンドを生成する.

    押し込みの下降だけは退避移動の速度で行う（こすり速度とは独立に保つ）。こすりは
    ``wipe_speed`` を stage の ``max_velocity`` で clamp した速度で走る。

    Raises:
        ValueError: 押し込み Z かこすり経路が可動域外の場合
    """
    wipe_speed = Speed.absolute(min(clean.wipe_speed, stage.max_velocity))
    return stage.move(
        z=_press_point(position, clean).z, speed=stage.travel_speed
    ) + stage.to_gcode(wipe_points(position, clean), speed=wipe_speed)


def depart_gcode(stage: XYZStage) -> GCode:
    """こすり後に退避 Z へ戻す移動コマンドを生成する.

    XY は動かさない。塗布シーケンスは「最初の点の上空」までしか上げないので、押し込み Z
    のまま次工程へ渡すと基板の上をその高さで走ってしまう。
    """
    return stage.move(z=TRAVEL_Z, speed=stage.travel_speed)


@attrs.frozen
class NozzleCleanPlan:
    """可動域を検証済みのクリーニング G-code.

    Attributes:
        position: クリーニング面の位置
        clean: クリーニング動作の設定
        approach: 面 Z までの接近
        wipe: 押し込み・十字往復・退避
    """

    position: Point3d
    clean: NozzleCleanConfig
    approach: GCode
    wipe: GCode


def plan_nozzle_clean(
    stage: XYZStage, position: Point3d, clean: NozzleCleanConfig
) -> NozzleCleanPlan:
    """接近・こすり・退避の G-code を組む（各移動の可動域は stage が検証する）.

    パージより前に組んで可動域を確かめておく。
    パージしてからこすりの段で可動域外と判定されると、シリコン上にペーストの塊を残したままジョブが落ちるため。

    Raises:
        ValueError: クリーニングの経由点が可動域外の場合
        KeyError: printer.cfg に limits 用のセクション・キーが無い場合
    """
    try:
        approach = stage.travel_to(position)
        wipe = wipe_gcode(stage, position, clean) + depart_gcode(stage)
    except ValueError as exc:
        raise ValueError(f"クリーニング位置が可動域外です（{exc}）") from exc
    return NozzleCleanPlan(position, clean, approach, wipe)


def resolve_nozzle_clean(
    machine: MachineConfig,
    stage: XYZStage,
    *,
    log: Callable[[str], None] | None = None,
) -> NozzleCleanPlan | None:
    """ジョブの開始時にクリーニングの G-code を組んで返す（未教示なら ``None``）.

    位置が未教示なら、クリーニングを飛ばすことをログに残す。これは立ち上げ途中の正常な状態で、
    クリーニングは衛生目的の補助工程なので、塗布ジョブ全体を失敗させない。

    教示済みなら G-code を組み、届かない設定は例外にする。教示ミスがあればそのまま進めず、
    機械を動かす前に気づかせる。実際に掃除するかどうかは、初回パージが写ったかで後から決まる。

    Raises:
        ValueError: クリーニングの経由点が可動域外の場合
    """
    clean = machine.require_solder().nozzle.clean
    if clean.position is None:
        (log or logger.info)(_NOT_RECORDED)
        return None
    return plan_nozzle_clean(stage, clean.position, clean)


def clean_nozzle(
    klipper: KlipperClient,
    applicator: PasteApplicator,
    plan: NozzleCleanPlan,
    *,
    log: Callable[[str], None] | None = None,
) -> None:
    """クリーニング位置でパージし、十字往復でこすってから退避する（ブロッキング）.

    ディスペンサーが有効な状態（``PasteApplicator`` の ``with`` の内側）で呼ぶ。
    リトラクトはしない。引き戻しは呼び出し側の :meth:`PasteApplicator.retract` が担い、
    ここでも引くと塗布シーケンスのプライム量と収支が合わなくなる。

    Args:
        klipper: G-code の送信先
        applicator: パージに使うディスペンサー（有効化済み）
        plan: :func:`plan_nozzle_clean` で組んだクリーニング
        log: 実施内容の 1 行通知先（未指定ならロガーへ）
    """
    clean = plan.clean
    klipper.send_gcode(plan.approach + GCode.wait_for_done())
    if clean.purge_ul > 0:
        applicator.load(clean.purge_ul)
    # こすりと退避を 1 回で送り、押し込んだ状態を跨いで送信が分割されないようにする。
    klipper.send_gcode(plan.wipe + GCode.wait_for_done())

    message = (
        f"ノズルクリーニング: {clean_position_label(plan.position)}"
        f" / パージ {clean.purge_ul:.3f} uL"
        f" / 十字往復 ±{clean.stroke:g} mm × {clean.passes} 回"
    )
    (log or logger.info)(message)
