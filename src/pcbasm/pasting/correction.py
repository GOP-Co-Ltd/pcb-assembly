"""塗布の前の補正（高さ面と位置合わせ）の計測."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import attrs

from pcbasm.align import AlignEvent, BoardCorrection, align_board
from pcbasm.board import Pad
from pcbasm.geometry import Transform
from pcbasm.vision import FrameSink

if TYPE_CHECKING:
    from pcbasm.pasting.session import PasteSession


@attrs.frozen
class PasteCorrection:
    """塗布座標変換に必要な計測済み補正（位置合わせ + 高さ面）."""

    alignment: BoardCorrection
    height_plane: Transform


def measure_correction(
    session: PasteSession,
    pads: Sequence[Pad],
    *,
    on_event: Callable[[AlignEvent], None],
    frame_sink: FrameSink | None = None,
) -> PasteCorrection:
    """高さ面を測り、pads を位置合わせして塗布の補正を返す.

    Raises:
        KlipperError: 高さ面の計測中または位置合わせ中に Klipper が要求を断った場合
        ValueError: 位置合わせに成功した領域が 1 件も無い場合
    """
    height_plane = session.measure_height_plane()
    alignment = align_board(
        session.alignment_session(frame_sink=frame_sink),
        pads,
        max_short_side_mm=session.machine.alignment.refine_max_short_side,
        on_event=on_event,
    )
    return PasteCorrection(alignment=alignment, height_plane=height_plane)
