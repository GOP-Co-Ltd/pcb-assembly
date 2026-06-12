"""Pcbnew によるテスト用 PCB の生成（グリッド基板 / fill 網羅フィクスチャ）.

モジュールレベルで ``pcbnew``（KiCAD の Python API）を import するため、
KiCAD 未導入環境では import できない。``pcbasm.pcb`` パッケージからは
re-export しない（利用側が明示的に ``pcbasm.pcb.generate`` を import する）。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pcbnew


def save_board(board: pcbnew.BOARD, output: Path) -> None:
    """BOARD を .kicad_pcb として保存する（親ディレクトリは作成する）."""
    output.parent.mkdir(parents=True, exist_ok=True)
    pcbnew.SaveBoard(str(output), board)


def generate_grid_pcb(
    size: float, divisions: int, pad_size: float, output: Path
) -> None:
    """正方形テストPCBにn^2個のグリッドパッドを配置して保存する."""
    board = pcbnew.BOARD()

    # 1. Board outline on Edge.Cuts (4 line segments forming a square)
    corners_mm = [(0, 0), (size, 0), (size, size), (0, size)]
    for k in range(4):
        x1, y1 = corners_mm[k]
        x2, y2 = corners_mm[(k + 1) % 4]
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
        seg.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x2), pcbnew.FromMM(y2)))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(pcbnew.FromMM(0.1))
        board.Add(seg)

    # 2. Create pads at grid intersections
    n = divisions
    step = size / (n + 1)
    pad_idx = 1
    for j in range(1, n + 1):  # row (Y)
        for i in range(1, n + 1):  # col (X)
            x_mm = i * step
            y_mm = j * step
            designator = f"P{pad_idx}"

            fp = pcbnew.FOOTPRINT(board)
            fp.SetReference(designator)
            fp.SetValue("GridPad")
            fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm)))
            fp.SetLayer(pcbnew.F_Cu)

            pad = pcbnew.PAD(fp)
            pad.SetNumber("1")
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(
                pcbnew.VECTOR2I(pcbnew.FromMM(pad_size), pcbnew.FromMM(pad_size))
            )
            pad.SetLayerSet(pad.SMDMask())  # F.Cu + F.Paste + F.Mask
            fp.Add(pad)

            board.Add(fp)
            pad_idx += 1

    # 3. Save
    save_board(board, output)

    print(f"Generated: {output}")
    print(f"Board: {size}x{size} mm")
    print(f"Grid: {n}x{n} = {n * n} pads (step={step:.1f} mm)")
    print(f"Pad size: {pad_size}x{pad_size} mm")


def build_fill_coverage_board() -> pcbnew.BOARD:
    """Fill ロジック要件網羅フィクスチャの ``BOARD`` を構築する.

    ``build_paste_fill_path`` の面 / 凹形 / 線 / 点 の各塗布分岐を網羅する
    F.Paste パッドを並べた基板を返す。

    含めるパッド（すべて F.Paste レイヤ）:

    - 面塗布: 大矩形 / roundrect
    - 凹形: L字 / ダンベル（custom shape + gr_poly primitives で表現）
    - 線塗布: 細長矩形（buffer(-inset) が空になり線フォールバックへ）
    - 点塗布: 極小パッド（buffer(-inset) も中心線も作れず点フォールバックへ）
    """
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
