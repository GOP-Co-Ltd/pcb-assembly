"""初回パージ位置（座標）の解決."""

from collections.abc import Sequence
from typing import Literal

import attrs
from shapely import Point, Polygon

from pcbasm.geometry import Point2d
from pcbasm.pcb import Pad
from pcbasm.utils import is_finite_number

PurgeSource = Literal["explicit", "default"]


@attrs.frozen
class ResolvedInitialPurge:
    """実行対象として解決された初回パージ点塗布.

    パージは pad ではなく座標で扱う。明示指定が無ければ塗布順路先頭 pad の
    中心座標を使う。

    Attributes:
        amount_ul: 点塗布する量 [uL]
        point: 塗布点（board 座標）
        label: 表示用の座標文字列
        source: 明示指定か、順路先頭からの既定解決か
    """

    amount_ul: float
    point: Point2d
    label: str
    source: PurgeSource


def resolve_initial_purge(
    *,
    amount_ul: float,
    point: Point2d | None,
    routed_pads: Sequence[Pad],
    outline: Polygon,
) -> tuple[ResolvedInitialPurge | None, str | None]:
    """初回パージの塗布点を解決する.

    ``point`` を明示すればそこへ、無ければ通常 fill sequence の先頭 pad の中心へ
    パージする。``amount_ul == 0`` は機能無効として扱い、対象を解決しない。

    明示した ``point`` は基板外形の内側でなければならない。既定解決の座標は
    基板上の pad 中心なので改めて検証しない。
    """
    amount, error = _validate_amount(amount_ul)
    if amount is None:
        return None, error
    if amount == 0:
        return None, None

    if point is not None:
        error = _validate_point(point, outline)
        if error is not None:
            return None, error
        return _resolved(amount, point, "explicit"), None

    if not routed_pads:
        return None, None
    return _resolved(amount, routed_pads[0].center, "default"), None


def validate_initial_purge(
    *,
    amount_ul: float,
    point: Point2d | None,
    outline: Polygon,
) -> str | None:
    """初回パージ設定を検証し、不正なら日本語エラー文、正常なら ``None``.

    ``amount_ul == 0``（機能無効）でも明示された ``point`` は検証する
    （無効化中でも基板外の座標を保存させないため）。
    """
    amount, error = _validate_amount(amount_ul)
    if amount is None:
        return error
    if point is None:
        return None
    return _validate_point(point, outline)


@attrs.frozen
class InitialPurgeResolution:
    """UI 表示用にまとめた初回パージの解決結果.

    Attributes:
        resolved: 実行対象（無効・未解決なら ``None``）
        default_point: 明示指定が無いときに使う座標（順路が空なら ``None``）
        error: 解決できなかった理由（無ければ ``None``）
    """

    resolved: ResolvedInitialPurge | None
    default_point: Point2d | None
    error: str | None


def resolve_initial_purge_for(
    *,
    amount_ul: float,
    point: Point2d | None,
    routed_pads: Sequence[Pad],
    outline: Polygon,
) -> InitialPurgeResolution:
    """初回パージの解決と既定座標（順路先頭の中心）をまとめて返す."""
    resolved, error = resolve_initial_purge(
        amount_ul=amount_ul,
        point=point,
        routed_pads=routed_pads,
        outline=outline,
    )
    default_point = routed_pads[0].center if routed_pads else None
    return InitialPurgeResolution(resolved, default_point, error)


def purge_point_label(point: Point2d) -> str:
    """パージ座標の表示用文字列（サーバー側で組んで返す）."""
    return f"({point.x:.2f}, {point.y:.2f}) mm"


def _resolved(
    amount_ul: float, point: Point2d, source: PurgeSource
) -> ResolvedInitialPurge:
    return ResolvedInitialPurge(
        amount_ul=amount_ul,
        point=point,
        label=purge_point_label(point),
        source=source,
    )


def _validate_amount(amount_ul: float) -> tuple[float | None, str | None]:
    if not is_finite_number(amount_ul):
        return None, f"initial_purge_ulは数値で指定してください: {amount_ul!r}"
    amount = float(amount_ul)
    if amount < 0:
        return None, f"initial_purge_ulは0以上で指定してください: {amount}"
    return amount, None


def _validate_point(point: Point2d, outline: Polygon) -> str | None:
    """パージ点が有限で基板外形の内側かを検証する."""
    if not is_finite_number(point.x) or not is_finite_number(point.y):
        return f"パージ位置は有限な座標で指定してください: ({point.x}, {point.y})"
    if not outline.covers(Point(point.x, point.y)):
        return (
            "パージ位置は基板外形の内側で指定してください: "
            f"({point.x:.3f}, {point.y:.3f})"
        )
    return None
