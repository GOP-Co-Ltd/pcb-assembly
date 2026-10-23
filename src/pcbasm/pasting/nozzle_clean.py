"""ノズル先端のクリーニング（パージ → 十字往復でこすり）.

ノズル先端の外周に残ったペーストは固まると最初の吐出量を乱し、塊のまま pad へ落ちる。
クリーニング位置でその場に少量パージして先端内部の固まりを押し出してから、シリコン
クリーナーへ押しつけて十字に往復し、外周の付着分を拭き取る。

塗布ジョブでこれを呼ぶのは、初回パージを撮って塗布が写らなかったときだけ（詰まりの
疑い）。判断とパージのやり直しは
:func:`~web.api.jobs.pasting.purge_check.purge_with_cleaning` が持つ。

位置は :class:`~pcbasm.config.NozzleClean` の ``position`` がマシン座標で持ち、基板とは
無関係なので board 補正を通さない。移動 G-code を組む関数は :func:`~pcbasm.parking.move_to_cap` と
同じく ``M400`` / ``M84`` を含めず、合成は :func:`clean_nozzle` が行う。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pcbasm.config import Machine, NozzleClean
from pcbasm.gcode import GCode
from pcbasm.geometry import Path, Point3d
from pcbasm.hal import Klipper, Limits, Speed, XYZStage
from pcbasm.pasting.applicator import PasteApplicator

logger = logging.getLogger(__name__)

# クリーニング位置への移動速度上限 [mm/s]（parking.CAP_PARK_VELOCITY と同趣旨）
CLEAN_TRAVEL_VELOCITY = 20.0

# XY 移動する退避 Z [mm]（move_to_cap と同じ規約）
TRAVEL_Z = 0.0

# クリーニングを行わない理由（WebUI の「未記録」表示と文言を揃える）
_NOT_RECORDED = "ノズルクリーニング: 位置が未記録のためスキップします"


def _press_point(position: Point3d, clean: NozzleClean) -> Point3d:
    """クリーニング面から ``press_depth`` だけ押し込んだ点（Z は 0 が上・負が下）."""
    return Point3d(position.x, position.y, position.z - clean.press_depth)


def wipe_points(position: Point3d, clean: NozzleClean) -> Path:
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


def approach_gcode(stage: XYZStage, position: Point3d) -> GCode:
    """クリーニング位置の面 Z まで接近する移動コマンドを生成する.

    Z を退避高さへ上げてからクリーニング XY へ移動し、最後に面 Z へ下ろす。各セグメント
    は stage の limits で検証する。``M400`` / ``M84`` は含めない。

    Raises:
        ValueError: クリーニング位置が可動域外の場合
        KeyError: printer.cfg に limits 用のセクション・キーが無い場合
    """
    speed = _travel_speed(stage)
    return (
        GCode("G90")
        + stage.move(z=TRAVEL_Z, speed=speed)
        + stage.move(x=position.x, y=position.y, speed=speed)
        + stage.move(z=position.z, speed=speed)
    )


def wipe_gcode(stage: XYZStage, position: Point3d, clean: NozzleClean) -> GCode:
    """面 Z から押し込んで十字往復する移動コマンドを生成する.

    押し込みの下降だけは travel 速度で行う（こすり速度とは独立に保つ）。こすりは
    ``wipe_speed`` を stage の ``max_velocity`` で clamp した速度で走る。

    Raises:
        ValueError: 押し込み Z かこすり経路が可動域外の場合
    """
    wipe_speed = Speed.absolute(min(clean.wipe_speed, stage.max_velocity))
    return stage.move(
        z=_press_point(position, clean).z, speed=_travel_speed(stage)
    ) + stage.to_gcode(wipe_points(position, clean), speed=wipe_speed)


def depart_gcode(stage: XYZStage) -> GCode:
    """こすり後に退避 Z へ戻す移動コマンドを生成する.

    XY は動かさない。塗布シーケンスは「最初の点の上空」までしか上げないので、押し込み Z
    のまま次工程へ渡すと基板の上をその高さで走ってしまう。
    """
    return stage.move(z=TRAVEL_Z, speed=_travel_speed(stage))


def validate_reach(
    stage: XYZStage, position: Point3d, clean: NozzleClean
) -> str | None:
    """クリーニングの全経由点が可動域に入るかを検証する（正常なら None）.

    退避 Z・面 Z・押し込み Z・こすり経路の全点と、travel / wipe 双方の送り速度を見る。
    :func:`clean_nozzle` はパージより前にこれを通す。パージしてからこすりの段で可動域外と判定されると、
    シリコン上にペーストの塊を残したままジョブが落ちるため。

    Raises:
        KeyError: printer.cfg に limits 用のセクション・キーが無い場合
    """
    limits = stage.limits
    travel_feed = _travel_speed(stage).resolve(stage.max_velocity)
    wipe_feed = min(clean.wipe_speed, stage.max_velocity)

    points = [
        (Point3d(position.x, position.y, TRAVEL_Z), travel_feed),
        (position, travel_feed),
        (_press_point(position, clean), travel_feed),
    ]
    points += [(point, wipe_feed) for point in wipe_points(position, clean)]

    # 1 軸が外れると往復の両端も揃って外れるので、軸ごとに最初の違反値だけ挙げる
    violations: dict[str, float] = {}
    for point, feed in points:
        if limits.contains(point, feed):
            continue
        for name, value in _violating_axes(limits, point, feed):
            violations.setdefault(name, value)
    if violations:
        listed = ", ".join(f"{name}={value}" for name, value in violations.items())
        return f"クリーニング位置が可動域外です: {listed}"
    return None


def _violating_axes(
    limits: Limits, point: Point3d, feed: float
) -> list[tuple[str, float]]:
    """可動域を外れた軸を (軸名, 値) で返す（``stage.move`` と同じ書式に使う）."""
    axes = [
        ("x", point.x, limits.x),
        ("y", point.y, limits.y),
        ("z", point.z, limits.z),
    ]
    violations = [(name, value) for name, value, scalar in axes if value not in scalar]
    if feed not in limits.v:
        violations.append(("feed", feed))
    return violations


def resolve_nozzle_clean(
    machine: Machine,
    stage: XYZStage,
    *,
    log: Callable[[str], None] | None = None,
) -> NozzleClean:
    """ジョブの開始時にクリーニング設定を確かめて返す.

    位置が未教示なら、クリーニングを飛ばすことをログに残す。これは立ち上げ途中の正常な状態で、
    クリーニングは衛生目的の補助工程なので、塗布ジョブ全体を失敗させない。

    教示済みなら可動域を検証し、届かない設定は例外にする。教示ミスがあればそのまま進めず、
    機械を動かす前に気づかせる。実際に掃除するかどうかは、初回パージが写ったかで後から決まる。

    Raises:
        ValueError: クリーニングの経由点が可動域外の場合
    """
    clean = machine.paste_dispenser.nozzle_clean
    if clean.position is None:
        (log or logger.info)(_NOT_RECORDED)
    elif error := validate_reach(stage, clean.position, clean):
        raise ValueError(error)
    return clean


def clean_position_label(position: Point3d) -> str:
    """記録したクリーニング面の位置を表す文字列（サーバー側で組んで返す）.

    押し込み量は同じ画面の設定欄で編集できるので、ここには含めない。 含めると設定を即保存したとき、表示だけが古いまま残る。
    """
    return f"({position.x:.2f}, {position.y:.2f}, {position.z:.2f}) mm"


def clean_nozzle(
    klipper: Klipper,
    stage: XYZStage,
    applicator: PasteApplicator,
    position: Point3d,
    clean: NozzleClean,
    *,
    log: Callable[[str], None] | None = None,
) -> None:
    """クリーニング位置でパージし、十字往復でこすってから退避する（ブロッキング）.

    ディスペンサーが有効な状態（``PasteApplicator`` の ``with`` の内側）で呼ぶ。
    リトラクトはしない。引き戻しは呼び出し側の :meth:`PasteApplicator.retract` が担い、
    ここでも引くと塗布シーケンスのプライム量と収支が合わなくなる。

    Args:
        klipper: G-code の送信先
        stage: 移動コマンドの生成と可動域検証に使う XYZ ステージ
        applicator: パージに使うディスペンサー（有効化済み）
        position: クリーニング面の位置
        clean: クリーニング動作の設定
        log: 実施内容の 1 行通知先（未指定ならロガーへ）

    Raises:
        ValueError: クリーニングの経由点が可動域外の場合（G-code を何も送らずに送出する）
    """
    if error := validate_reach(stage, position, clean):
        raise ValueError(error)

    klipper.send_gcode(approach_gcode(stage, position) + GCode.wait_for_done())
    if clean.purge_ul > 0:
        applicator.load(clean.purge_ul)
    # こすりと退避を 1 回で送り、押し込んだ状態を跨いで送信が分割されないようにする。
    klipper.send_gcode(
        wipe_gcode(stage, position, clean) + depart_gcode(stage) + GCode.wait_for_done()
    )

    message = (
        f"ノズルクリーニング: {clean_position_label(position)}"
        f" / パージ {clean.purge_ul:.3f} uL"
        f" / 十字往復 ±{clean.stroke:g} mm × {clean.passes} 回"
    )
    (log or logger.info)(message)


def _travel_speed(stage: XYZStage) -> Speed:
    return Speed.absolute(min(CLEAN_TRAVEL_VELOCITY, stage.max_velocity))
