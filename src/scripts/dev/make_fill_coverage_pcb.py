#!/usr/bin/env python3
"""Fill ロジック要件網羅用の KiCad PCB フィクスチャを生成するスクリプト.

``build_paste_fill_path`` の面 / 凹形 / 線 / 点 の各塗布分岐を網羅する
F.Paste パッドを並べた基板を ``pcbnew`` API で生成する。出力先::

    data/testing/fill_coverage/fill_coverage.kicad_pcb

含めるパッド（すべて F.Paste レイヤ）:

- 面塗布: 大矩形 / roundrect
- 凹形: L字 / ダンベル（custom shape + gr_poly primitives で表現）
- 線塗布: 細長矩形（buffer(-inset) が空になり線フォールバックへ）
- 点塗布: 極小パッド（buffer(-inset) も中心線も作れず点フォールバックへ）

実行::

    uv run python -m scripts.dev.make_fill_coverage_pcb
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pcbnew

from pcbasm.utils import PROJECT_ROOT

_OUTPUT = (
    PROJECT_ROOT / "data" / "testing" / "fill_coverage" / "fill_coverage.kicad_pcb"
)


def _mm(value: float) -> int:
    """Mm を内部単位（nm）に変換する."""
    return cast(int, pcbnew.FromMM(value))


def _v(x: float, y: float) -> pcbnew.VECTOR2I:
    """Mm 座標を ``VECTOR2I`` に変換する."""
    return pcbnew.VECTOR2I(_mm(x), _mm(y))


def _add_footprint(
    board: pcbnew.BOARD, ref: str, x: float, y: float
) -> pcbnew.FOOTPRINT:
    """指定位置に空のフットプリントを追加する."""
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetPosition(_v(x, y))
    board.Add(fp)
    return fp


def _paste_pad(fp: pcbnew.FOOTPRINT, number: str) -> pcbnew.PAD:
    """F.Paste のみを有効にした SMD パッドを作って返す（サイズ等は呼び出し側）."""
    pad = pcbnew.PAD(fp)
    pad.SetNumber(number)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    layers = pcbnew.LSET()
    layers.AddLayer(pcbnew.F_Paste)
    pad.SetLayerSet(layers)
    return pad


def _add_rect_pad(
    board: pcbnew.BOARD,
    ref: str,
    x: float,
    y: float,
    w: float,
    h: float,
) -> None:
    """矩形パッドを1つ持つフットプリントを追加する."""
    fp = _add_footprint(board, ref, x, y)
    pad = _paste_pad(fp, "1")
    pad.SetShape(pcbnew.PAD_SHAPE_RECTANGLE)
    pad.SetSize(pcbnew.VECTOR2I(_mm(w), _mm(h)))
    pad.SetPosition(_v(x, y))
    fp.Add(pad)


def _add_roundrect_pad(
    board: pcbnew.BOARD,
    ref: str,
    x: float,
    y: float,
    w: float,
    h: float,
    radius: float,
) -> None:
    """Roundrect パッドを1つ持つフットプリントを追加する."""
    fp = _add_footprint(board, ref, x, y)
    pad = _paste_pad(fp, "1")
    pad.SetShape(pcbnew.PAD_SHAPE_ROUNDRECT)
    pad.SetSize(pcbnew.VECTOR2I(_mm(w), _mm(h)))
    pad.SetRoundRectCornerRadius(_mm(radius))
    pad.SetPosition(_v(x, y))
    fp.Add(pad)


def _add_custom_pad(
    board: pcbnew.BOARD,
    ref: str,
    x: float,
    y: float,
    rings: list[list[tuple[float, float]]],
) -> None:
    """Custom shape パッド（gr_poly primitives）を持つフットプリントを追加する.

    ``rings`` の各要素はパッド中心からの相対 mm 座標で表した閉ポリゴン。
    凹形（L字・ダンベル）を表現するために使う。アンカーは極小円とする。
    """
    fp = _add_footprint(board, ref, x, y)
    pad = _paste_pad(fp, "1")
    pad.SetShape(pcbnew.PAD_SHAPE_CUSTOM)
    pad.SetAnchorPadShape(pcbnew.F_Paste, pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(pcbnew.VECTOR2I(_mm(0.1), _mm(0.1)))
    pad.SetPosition(_v(x, y))
    for ring in rings:
        poly = pcbnew.SHAPE_POLY_SET()
        chain = pcbnew.SHAPE_LINE_CHAIN()
        for px, py in ring:
            chain.Append(_mm(px), _mm(py))
        chain.SetClosed(True)
        poly.AddOutline(chain)
        pad.AddPrimitivePoly(pcbnew.F_Paste, poly, 0, True)
    fp.Add(pad)


def _add_outline(board: pcbnew.BOARD, width: float, height: float) -> None:
    """Edge.Cuts に矩形アウトラインを追加する."""
    corners = [(0.0, 0.0), (width, 0.0), (width, height), (0.0, height)]
    for i in range(4):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetStart(_v(*corners[i]))
        seg.SetEnd(_v(*corners[(i + 1) % 4]))
        board.Add(seg)


def build_board() -> pcbnew.BOARD:
    """要件網羅フィクスチャの ``BOARD`` を構築する."""
    board = pcbnew.BOARD()
    _add_outline(board, width=60.0, height=40.0)

    # 面塗布: 大矩形
    _add_rect_pad(board, "AREA_RECT", x=12.0, y=10.0, w=12.0, h=8.0)
    # 面塗布: roundrect
    _add_roundrect_pad(board, "AREA_RR", x=30.0, y=10.0, w=10.0, h=7.0, radius=2.0)

    # 凹形: L字（custom shape）
    l_shape = [
        (-5.0, -5.0),
        (5.0, -5.0),
        (5.0, -1.0),
        (-1.0, -1.0),
        (-1.0, 5.0),
        (-5.0, 5.0),
    ]
    _add_custom_pad(board, "CONCAVE_L", x=12.0, y=28.0, rings=[l_shape])

    # 凹形: ダンベル（中央がくびれた custom shape）
    dumbbell = [
        (-6.0, -3.0),
        (-2.0, -3.0),
        (-2.0, -1.0),
        (2.0, -1.0),
        (2.0, -3.0),
        (6.0, -3.0),
        (6.0, 3.0),
        (2.0, 3.0),
        (2.0, 1.0),
        (-2.0, 1.0),
        (-2.0, 3.0),
        (-6.0, 3.0),
    ]
    _add_custom_pad(board, "CONCAVE_DB", x=34.0, y=28.0, rings=[dumbbell])

    # 凹形（細首ダンベル）: ネック幅 0.3mm で d=0.4(inset=0.2) の buffer(-inset) が
    # ネック中央で途切れ、連結成分が 2 つに分裂する。成分分割の目視確認用。
    thin_dumbbell = [
        (-5.0, -3.0),
        (-1.0, -3.0),
        (-1.0, -0.15),
        (1.0, -0.15),
        (1.0, -3.0),
        (5.0, -3.0),
        (5.0, 3.0),
        (1.0, 3.0),
        (1.0, 0.15),
        (-1.0, 0.15),
        (-1.0, 3.0),
        (-5.0, 3.0),
    ]
    _add_custom_pad(board, "SPLIT_DB", x=18.0, y=36.0, rings=[thin_dumbbell])

    # 線塗布: 細長矩形（短辺が小さく buffer(-inset) が空になる）
    _add_rect_pad(board, "LINE_THIN", x=50.0, y=10.0, w=8.0, h=0.3)

    # 点塗布: 極小パッド
    _add_rect_pad(board, "DOT_TINY", x=50.0, y=22.0, w=0.2, h=0.2)

    return board


def main() -> None:
    """フィクスチャ PCB を生成して保存する."""
    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    board = build_board()
    pcbnew.SaveBoard(str(_OUTPUT), board)
    print(f"保存しました: {_OUTPUT}")


if __name__ == "__main__":
    main()
