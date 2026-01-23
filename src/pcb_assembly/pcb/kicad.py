"""KiCadからの部品・パッド情報抽出.

このモジュールはKiCad Python API (pcbnew) を使用して
PCBファイルから部品・パッド情報を抽出する.

Note:
    pcbnewモジュールはKiCad 9.0以降が必要.
    すべての座標は基板アウトラインの左上を原点として正規化される.
"""

from pathlib import Path

import pcbnew
from shapely import Polygon
from shapely.affinity import translate

from pcb_assembly.geometry.transform import Point2d

from .board import Component, ComponentList, Layer, Outline, Pad, PadList


def _nm_to_mm(nm: float) -> float:
    """ナノメートルをミリメートルに変換."""
    return nm / 1_000_000.0


def _get_original_outline_polygon(board: pcbnew.BOARD) -> Polygon:
    """基板アウトラインのポリゴンを取得（正規化前）.

    Args:
        board: pcbnewのボードオブジェクト

    Returns:
        基板アウトラインのポリゴン (mm単位、KiCad座標系)
    """
    outline_poly_set = pcbnew.SHAPE_POLY_SET()
    board.GetBoardPolygonOutlines(outline_poly_set)

    if outline_poly_set.OutlineCount() == 0:
        return Polygon()

    outline = outline_poly_set.Outline(0)
    points = [(_nm_to_mm(p.x), _nm_to_mm(p.y)) for p in outline.CPoints()]

    if not points:
        return Polygon()

    if points[0] != points[-1]:
        points.append(points[0])

    return Polygon(points)


def extract_outline(pcb_path: Path) -> Outline:
    """KiCad PCBファイルから基板アウトラインを抽出.

    座標は基板の左上を原点として正規化される.

    Args:
        pcb_path: KiCad PCBファイル (.kicad_pcb) のパス

    Returns:
        基板アウトライン（左上原点に正規化済み）

    Raises:
        OSError: ファイルが開けない場合
    """
    board = pcbnew.LoadBoard(str(pcb_path))
    polygon = _get_original_outline_polygon(board)

    if polygon.is_empty:
        return Outline(Polygon())

    origin_x, origin_y, _, _ = polygon.bounds
    normalized_polygon = translate(polygon, xoff=-origin_x, yoff=-origin_y)

    return Outline(normalized_polygon)


def extract_components(pcb_path: Path) -> ComponentList:
    """KiCad PCBファイルから部品情報を抽出.

    座標は基板の左上を原点として正規化される.

    Args:
        pcb_path: KiCad PCBファイル (.kicad_pcb) のパス

    Returns:
        部品情報のリスト（左上原点に正規化済み）

    Raises:
        OSError: ファイルが開けない場合
    """
    board = pcbnew.LoadBoard(str(pcb_path))
    outline_polygon = _get_original_outline_polygon(board)
    origin_x, origin_y, _, _ = (
        outline_polygon.bounds if not outline_polygon.is_empty else (0, 0, 0, 0)
    )
    components = ComponentList()

    for footprint in board.GetFootprints():
        # DNP部品はスキップ
        if footprint.IsDNP():
            continue

        # レイヤー判定
        if footprint.IsFlipped():
            layer = Layer.BOTTOM
        else:
            layer = Layer.TOP

        # 位置取得 (nm -> mm変換、原点補正)
        pos = footprint.GetPosition()
        x = _nm_to_mm(pos.x) - origin_x
        y = _nm_to_mm(pos.y) - origin_y

        # 回転角度取得 (KiCad 9ではEDA_ANGLE型)
        orientation = footprint.GetOrientation()
        rotation = orientation.AsDegrees()

        components.append(
            Component(
                designator=footprint.GetReference(),
                value=footprint.GetValue(),
                package=footprint.GetFPID().GetLibItemName(),
                position=Point2d(x=x, y=y),
                rotation=rotation,
                layer=layer,
            )
        )

    return components


def extract_pads(pcb_path: Path) -> PadList:
    """KiCad PCBファイルからパッド情報を抽出.

    ペーストレイヤー (F_Paste/B_Paste) に存在するパッドのみ抽出する.
    座標は基板の左上を原点として正規化される.

    Args:
        pcb_path: KiCad PCBファイル (.kicad_pcb) のパス

    Returns:
        パッド情報のリスト（左上原点に正規化済み）

    Raises:
        OSError: ファイルが開けない場合
    """
    board = pcbnew.LoadBoard(str(pcb_path))
    outline_polygon = _get_original_outline_polygon(board)
    origin_x, origin_y, _, _ = (
        outline_polygon.bounds if not outline_polygon.is_empty else (0, 0, 0, 0)
    )
    pads = PadList()

    for footprint in board.GetFootprints():
        # DNP部品はスキップ
        if footprint.IsDNP():
            continue

        for pad in footprint.Pads():
            # パッドが所属するレイヤーセットを取得
            layer_set = pad.GetLayerSet()

            # F_Paste または B_Paste のどちらに含まれているか確認
            target_layer = None
            if layer_set.Contains(pcbnew.F_Paste):
                target_layer = pcbnew.F_Paste
            elif layer_set.Contains(pcbnew.B_Paste):
                target_layer = pcbnew.B_Paste

            # ペーストレイヤーがないパッドは無視
            if target_layer is None:
                continue

            # ポリゴン取得 (レイヤーごとのソルダーペースト設定を反映)
            shape_poly_set = pad.GetEffectivePolygon(target_layer)

            # ポリゴンセット内の各輪郭を取り出す
            for outline_idx in range(shape_poly_set.OutlineCount()):
                outline = shape_poly_set.Outline(outline_idx)

                # 頂点リスト (nm -> mm変換、原点補正)
                points = []
                for point in outline.CPoints():
                    x = _nm_to_mm(point.x) - origin_x
                    y = _nm_to_mm(point.y) - origin_y
                    points.append((x, y))

                # 閉じたポリゴンにする
                if points and points[0] != points[-1]:
                    points.append(points[0])

                pads.append(
                    Pad(
                        designator=footprint.GetReference(),
                        pad_number=pad.GetNumber(),
                        net_name=pad.GetNetname(),
                        layer=Layer.TOP
                        if target_layer == pcbnew.F_Paste
                        else Layer.BOTTOM,
                        polygon=Polygon(points),
                        is_custom_shape=pad.GetShape() == pcbnew.PAD_SHAPE_CUSTOM,
                    )
                )

    return pads
