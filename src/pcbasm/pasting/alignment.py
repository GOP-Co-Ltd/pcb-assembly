"""塗布向けの位置合わせ合成（posctrl の薄い塗布固有ラッパー）.

領域照合（``RegionAlignmentSession.align``）と pad 中心の精密照合（``refine``）を
組み合わせ、塗布座標変換に必要な :class:`PasteCorrection` を組み立てるための部品。
ループ・進捗・ログ文言は呼び出し側（web ジョブ）が持つ。
"""

from __future__ import annotations

from collections.abc import Sequence

import attrs

from pcbasm.geometry import Point2d, Transform
from pcbasm.pcb import Pad
from pcbasm.posctrl import (
    BoardAlignment,
    RegionAlignment,
    RegionAlignmentSession,
    is_pad_refinement_target,
)


@attrs.frozen
class PasteCorrection:
    """塗布座標変換に必要な計測済み補正（位置合わせ + 高さ面）."""

    alignment: BoardAlignment
    height_plane: Transform


@attrs.frozen
class PadRefinement:
    """1 pad の中心精密照合の結果.

    Attributes:
        pad: 対象 pad
        result: 収束した照合結果（収束しなければ ``None`` = 領域補正を使う）
        residual: 領域補正に対する残差 [mm]（収束しなければ ``None``）
    """

    pad: Pad
    result: RegionAlignment | None
    residual: Point2d | None


def refinement_targets(
    pads: Sequence[Pad], *, max_short_side_mm: float
) -> tuple[Pad, ...]:
    """Pad 中心精密照合の対象（短辺が ``max_short_side_mm`` 以下の pad）を返す."""
    return tuple(
        pad
        for pad in pads
        if is_pad_refinement_target(pad, max_short_side_mm=max_short_side_mm)
    )


def refine_pad(
    session: RegionAlignmentSession,
    *,
    board_transform: Transform,
    alignment: BoardAlignment,
    pad: Pad,
) -> PadRefinement:
    """領域補正を初期値に pad 中心で照合を収束させ、残差付きの結果を返す."""
    initial_correction = alignment.correction_for(pad.center, designator=pad.designator)
    refined = session.refine(pad.center, initial_correction, pad.polygon)
    if refined is None:
        return PadRefinement(pad=pad, result=None, residual=None)
    base = board_transform.apply(pad.center)
    initial_displacement = initial_correction.apply(base) - base
    return PadRefinement(
        pad=pad, result=refined, residual=refined.displacement - initial_displacement
    )
