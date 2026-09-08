"""初回パージの対象解決（任意点 / pad / 順路先頭）."""

from collections.abc import Sequence
from typing import Literal

import attrs
from shapely import Point, Polygon

from pcbasm.geometry import Point2d
from pcbasm.pcb import Layer, Pad, PadHierarchy
from pcbasm.utils import is_finite_number

PurgeSource = Literal["point", "pad", "default"]


@attrs.frozen
class ResolvedInitialPurge:
    """実行対象として解決された初回パージ点塗布.

    Attributes:
        amount_ul: 点塗布する量 [uL]
        point: 塗布点（board 座標。pad 由来なら pad 中心）
        label: 表示用の対象名（pad id か座標）
        pad: 対象 pad（任意点指定なら ``None``）
        pad_id: WebUI/API で使う pad id（任意点指定なら ``None``）
        source: 任意点指定・pad 指定・順路先頭の既定解決のいずれか
    """

    amount_ul: float
    point: Point2d
    label: str
    pad: Pad | None
    pad_id: str | None
    source: PurgeSource


def resolve_initial_purge(
    *,
    amount_ul: float,
    point: Point2d | None,
    pad_id: str | None,
    hierarchy: PadHierarchy,
    routed_pads: Sequence[Pad],
    outline: Polygon,
    layer: Layer = Layer.TOP,
) -> tuple[ResolvedInitialPurge | None, str | None]:
    """初回パージの塗布点を解決する.

    優先順位は ``point`` > ``pad_id`` > 通常 fill sequence の先頭 pad。
    ``amount_ul == 0`` は機能無効として扱い、対象を解決しない。

    ``point`` は基板外形の内側でなければならない。``pad_id`` の明示指定は
    disabled pad でもよいので、``routed_pads`` ではなく ``hierarchy`` 内の
    全 pad から探す。
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
        return (
            ResolvedInitialPurge(
                amount_ul=amount,
                point=point,
                label=_point_label(point),
                pad=None,
                pad_id=None,
                source="point",
            ),
            None,
        )

    if pad_id is None:
        if not routed_pads:
            return None, None
        pad = routed_pads[0]
        return _from_pad(amount, pad, hierarchy.pad_id_for_pad(pad), "default"), None

    pad, error = _resolve_pad_by_id(hierarchy, pad_id, layer)
    if pad is None:
        return None, error
    return _from_pad(amount, pad, pad_id, "pad"), None


def validate_initial_purge(
    *,
    amount_ul: float,
    point: Point2d | None,
    pad_id: str | None,
    hierarchy: PadHierarchy,
    routed_pads: Sequence[Pad],
    outline: Polygon,
    layer: Layer = Layer.TOP,
) -> str | None:
    """初回パージ設定を検証し、不正なら日本語エラー文、正常なら ``None``.

    :func:`resolve_initial_purge` と同一規則の None 返却バリデーション。ただし
    ``amount_ul == 0``（機能無効）でも対象指定があれば検証する（無効化中でも
    不正な指定を保存させないため）。
    """
    resolved, error = resolve_initial_purge(
        amount_ul=amount_ul,
        point=point,
        pad_id=pad_id,
        hierarchy=hierarchy,
        routed_pads=routed_pads,
        outline=outline,
        layer=layer,
    )
    if error is not None:
        return error
    if resolved is not None:
        return None
    if point is not None:
        return _validate_point(point, outline)
    if pad_id is not None:
        _, pad_error = _resolve_pad_by_id(hierarchy, pad_id, layer)
        return pad_error
    return None


@attrs.frozen
class InitialPurgeResolution:
    """UI 表示用にまとめた初回パージの解決結果.

    Attributes:
        resolved: 実行対象（無効・未解決なら ``None``）
        default_pad_id: 対象未指定時に自動選択される pad id（無ければ ``None``）
        error: 解決できなかった理由（無ければ ``None``）
    """

    resolved: ResolvedInitialPurge | None
    default_pad_id: str | None
    error: str | None


def resolve_initial_purge_for(
    *,
    amount_ul: float,
    point: Point2d | None,
    pad_id: str | None,
    hierarchy: PadHierarchy,
    routed_pads: Sequence[Pad],
    outline: Polygon,
) -> InitialPurgeResolution:
    """初回パージの解決と既定 pad（順路先頭）をまとめて返す."""
    resolved, error = resolve_initial_purge(
        amount_ul=amount_ul,
        point=point,
        pad_id=pad_id,
        hierarchy=hierarchy,
        routed_pads=routed_pads,
        outline=outline,
        layer=Layer.TOP,
    )
    default_pad_id = hierarchy.pad_id_for_pad(routed_pads[0]) if routed_pads else None
    return InitialPurgeResolution(resolved, default_pad_id, error)


def _from_pad(
    amount_ul: float, pad: Pad, pad_id: str, source: PurgeSource
) -> ResolvedInitialPurge:
    return ResolvedInitialPurge(
        amount_ul=amount_ul,
        point=pad.center,
        label=pad_id,
        pad=pad,
        pad_id=pad_id,
        source=source,
    )


def _point_label(point: Point2d) -> str:
    return f"({point.x:.2f}, {point.y:.2f}) mm"


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


def _resolve_pad_by_id(
    hierarchy: PadHierarchy, pad_id: str, layer: Layer
) -> tuple[Pad | None, str | None]:
    """Pad id を検証付きで解決する（不在／レイヤ不一致はエラー文を返す）."""
    pad = _find_pad_by_id(hierarchy, pad_id)
    if pad is None:
        return None, f"未知の初回パージ pad です: {pad_id}"
    if pad.layer is not layer:
        return (
            None,
            f"初回パージ pad は {layer.value} レイヤから選択してください: {pad_id}",
        )
    return pad, None


def _find_pad_by_id(hierarchy: PadHierarchy, pad_id: str) -> Pad | None:
    """Pad id に対応する pad を返す（見つからなければ None）."""
    for pad in hierarchy.iter_pads():
        if hierarchy.pad_id_for_pad(pad) == pad_id:
            return pad
    return None
