"""初回パージ点塗布の pad 解決ロジック."""

from collections.abc import Sequence
from typing import Literal

import attrs

from pcbasm.pcb import Layer, Pad, PadHierarchy
from pcbasm.utils import is_finite_number

DATASET_PURGE_PAD_ID = "PURGE"

InitialPurgePurpose = Literal["paste_solder", "paste_dataset_collection"]


@attrs.frozen
class ResolvedInitialPurge:
    """実行対象として解決された初回パージ点塗布.

    Attributes:
        amount_ul: 点塗布する量 [uL]
        pad: 対象 pad
        pad_id: WebUI/API で使う pad id
        source: 明示指定か、fill sequence 先頭からの既定解決か
    """

    amount_ul: float
    pad: Pad
    pad_id: str
    source: Literal["explicit", "default"]


def resolve_initial_purge(
    *,
    amount_ul: float,
    pad_id: str | None,
    hierarchy: PadHierarchy,
    routed_pads: Sequence[Pad],
    layer: Layer = Layer.TOP,
) -> tuple[ResolvedInitialPurge | None, str | None]:
    """初回パージ点塗布の対象 pad を解決する.

    ``amount_ul == 0`` は機能無効として扱い、対象 pad を解決しない。
    ``pad_id`` 未指定時は通常 fill sequence の先頭 pad を使う。
    明示指定時は disabled pad でもよいので、``routed_pads`` ではなく
    ``hierarchy`` 内の全 pad から探す。
    """
    amount, error = _validate_amount(amount_ul)
    if amount is None:
        return None, error
    if amount == 0:
        return None, None

    if pad_id is None:
        if not routed_pads:
            return None, None
        pad = routed_pads[0]
        return (
            ResolvedInitialPurge(
                amount_ul=amount,
                pad=pad,
                pad_id=hierarchy.pad_id_for_pad(pad),
                source="default",
            ),
            None,
        )

    pad, error = _resolve_pad_by_id(hierarchy, pad_id, layer)
    if pad is None:
        return None, error
    return (
        ResolvedInitialPurge(
            amount_ul=amount,
            pad=pad,
            pad_id=pad_id,
            source="explicit",
        ),
        None,
    )


def resolve_dataset_initial_purge(
    *,
    amount_ul: float,
    pad_id: str | None,
    hierarchy: PadHierarchy,
) -> tuple[ResolvedInitialPurge | None, str | None]:
    """Dataset収集用の初回パージpadを解決する.

    基板設定で ``pad_id`` が明示されていれば通常塗布と同じ選択を使う。
    未指定時だけ、Top面でdesignatorが ``PURGE`` の一意なpadを自動選択する。
    """
    if pad_id is not None:
        return resolve_initial_purge(
            amount_ul=amount_ul,
            pad_id=pad_id,
            hierarchy=hierarchy,
            routed_pads=(),
            layer=Layer.TOP,
        )

    amount, error = _validate_amount(amount_ul)
    if amount is None or amount == 0:
        return None, error

    matches = [
        pad
        for pad in hierarchy.iter_pads()
        if pad.layer is Layer.TOP and pad.designator == DATASET_PURGE_PAD_ID
    ]
    if not matches:
        return None, f"未知のdataset purge padです: {DATASET_PURGE_PAD_ID}"
    if len(matches) != 1:
        return (
            None,
            f"dataset purge pad {DATASET_PURGE_PAD_ID} が一意ではありません: "
            f"{len(matches)} pads",
        )
    return (
        ResolvedInitialPurge(
            amount_ul=amount,
            pad=matches[0],
            pad_id=DATASET_PURGE_PAD_ID,
            source="default",
        ),
        None,
    )


def validate_initial_purge(
    *,
    amount_ul: float,
    pad_id: str | None,
    hierarchy: PadHierarchy,
    routed_pads: Sequence[Pad],
    layer: Layer = Layer.TOP,
) -> str | None:
    """初回パージ設定を検証し、不正なら日本語エラー文、正常なら ``None``.

    :func:`resolve_initial_purge` と同一規則の None 返却バリデーション。ただし
    ``amount_ul == 0``（機能無効）でも ``pad_id`` 指定時は pad の存在とレイヤを
    検証する（無効化中でも不正な pad 指定を保存させないため）。
    """
    resolved, error = resolve_initial_purge(
        amount_ul=amount_ul,
        pad_id=pad_id,
        hierarchy=hierarchy,
        routed_pads=routed_pads,
        layer=layer,
    )
    if error is not None:
        return error
    if resolved is None and pad_id is not None:
        _, pad_error = _resolve_pad_by_id(hierarchy, pad_id, layer)
        return pad_error
    return None


@attrs.frozen
class InitialPurgeResolution:
    """UI 表示用にまとめた初回パージの解決結果.

    Attributes:
        resolved: 実行対象（無効・未解決なら ``None``）
        default_pad_id: ``pad_id`` 未指定時に自動選択される pad id（無ければ ``None``）
        error: 解決できなかった理由（無ければ ``None``）
    """

    resolved: ResolvedInitialPurge | None
    default_pad_id: str | None
    error: str | None


def resolve_initial_purge_for(
    purpose: InitialPurgePurpose,
    *,
    amount_ul: float,
    pad_id: str | None,
    hierarchy: PadHierarchy,
    routed_pads: Sequence[Pad],
) -> InitialPurgeResolution:
    """用途（通常塗布 / dataset 収集）に応じた初回パージ解決と既定 pad をまとめて返す.

    通常塗布の既定 pad は順路先頭。dataset 収集の既定 pad は ``PURGE`` designator の
    一意な pad で、``amount_ul == 0`` でも既定 pad の候補は表示のために求める。
    """
    if purpose == "paste_dataset_collection":
        resolved, error = resolve_dataset_initial_purge(
            amount_ul=amount_ul, pad_id=pad_id, hierarchy=hierarchy
        )
        default_pad_id = None
        if pad_id is None:
            default, default_error = resolve_dataset_initial_purge(
                amount_ul=1.0, pad_id=None, hierarchy=hierarchy
            )
            default_pad_id = default.pad_id if default is not None else None
            error = error or default_error
        return InitialPurgeResolution(resolved, default_pad_id, error)

    resolved, error = resolve_initial_purge(
        amount_ul=amount_ul,
        pad_id=pad_id,
        hierarchy=hierarchy,
        routed_pads=routed_pads,
        layer=Layer.TOP,
    )
    default_pad_id = hierarchy.pad_id_for_pad(routed_pads[0]) if routed_pads else None
    return InitialPurgeResolution(resolved, default_pad_id, error)


def _validate_amount(amount_ul: float) -> tuple[float | None, str | None]:
    if not is_finite_number(amount_ul):
        return None, f"initial_purge_ulは数値で指定してください: {amount_ul!r}"
    amount = float(amount_ul)
    if amount < 0:
        return None, f"initial_purge_ulは0以上で指定してください: {amount}"
    return amount, None


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
