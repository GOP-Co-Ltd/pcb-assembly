"""塗布の表示文字列（座標の書式と、ドメインの値のラベル）.

表示文字列はサーバー側で組み、JS はそのまま出す。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from pcbasm.geometry import Point2d, Point3d

if TYPE_CHECKING:
    # tact は plan 経由で labels を import する
    from pcbasm.pasting.tact import TactEstimate


def fmt_xy(point: Point2d, digits: int = 2) -> str:
    """``(x, y)`` を小数 ``digits`` 桁で書く."""
    return f"({point.x:.{digits}f}, {point.y:.{digits}f})"


def fmt_xyz(point: Point3d, digits: int = 2) -> str:
    """``(x, y, z)`` を小数 ``digits`` 桁で書く."""
    return f"({point.x:.{digits}f}, {point.y:.{digits}f}, {point.z:.{digits}f})"


def purge_point_label(point: Point2d) -> str:
    """初回パージの座標."""
    return f"{fmt_xy(point)} mm"


def purge_selection_label(point: Point2d | None, default_point: Point2d | None) -> str:
    """初回パージの位置の指定内容（明示した点か、自動で選んだ点か）."""
    if point is not None:
        return purge_point_label(point)
    if default_point is not None:
        return f"自動 {purge_point_label(default_point)}"
    return "自動 (順路が空です)"


def clean_position_label(position: Point3d) -> str:
    """記録したクリーニング面の位置.

    押し込み量は同じ画面の設定欄で編集できるので、ここには含めない。 含めると設定を即保存したとき、表示だけが古いまま残る。
    """
    return f"{fmt_xyz(position)} mm"


def flow_calibration_label(
    points: Sequence[Point2d], *, planned_amount_ul: float | None, error: str | None
) -> str:
    """運転時補正の状態（補正しないときはその理由まで）.

    Args:
        points: 基板設定の測定位置
        planned_amount_ul: 補正を走らせるときの 1 点あたりの量（走らせないなら ``None``）
        error: 設定が不正な理由
    """
    if not points:
        return "未設定（補正しません）"
    listed = "、".join(fmt_xy(point) for point in points)
    placed = f"{len(points)} 点 {listed} mm"
    if planned_amount_ul is not None:
        return f"{placed} x {planned_amount_ul:.3f} uL"
    if error is not None:
        return f"{placed}（{error}）"
    return f"{placed}（校正ファイル未設定のため補正しません）"


def duration_label(seconds: float) -> str:
    """所要時間を「h時間m分s秒」で書く（1 秒未満は切り捨てる）."""
    total = int(seconds)
    return f"{total // 3600}時間{total % 3600 // 60}分{total % 60}秒"


def tact_breakdown_label(estimate: TactEstimate) -> str:
    """予測所要時間の内訳（塗る pad の数と、前段・塗布の時間）."""
    return (
        f"{estimate.pad_count} pad（前段 {duration_label(estimate.setup_seconds)}"
        f" + 塗布 {duration_label(estimate.paste_seconds)}）"
    )
