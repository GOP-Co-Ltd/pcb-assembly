"""KiCadからの部品・パッド情報抽出.

このモジュールはKiCad Python API (pcbnew) を使用して
PCBファイルから部品・パッド情報を抽出する.

Note:
    pcbnewモジュールはKiCad 9.0以降が必要.
"""

from pathlib import Path

import pcbnew
from shapely import Polygon

from .component import Component, ComponentList
from .pad import Pad, PadList
from .utils import Layer


def extract_components(pcb_path: Path) -> ComponentList:
    """KiCad PCBファイルから部品情報を抽出.

    Args:
        pcb_path: KiCad PCBファイル (.kicad_pcb) のパス

    Returns:
        部品情報のリスト

    Raises:
        OSError: ファイルが開けない場合
    """
    board = pcbnew.LoadBoard(str(pcb_path))
    components = ComponentList()

    for footprint in board.GetFootprints():
        # レイヤー判定
        if footprint.IsFlipped():
            layer = Layer.BOTTOM
        else:
            layer = Layer.TOP

        # 位置取得 (nm -> mm変換)
        pos = footprint.GetPosition()
        x = pos.x / 1_000_000.0
        y = pos.y / 1_000_000.0

        # 回転角度取得 (KiCad 9ではEDA_ANGLE型)
        orientation = footprint.GetOrientation()
        rotation = orientation.AsDegrees()

        components.append(
            Component(
                designator=footprint.GetReference(),
                value=footprint.GetValue(),
                package=footprint.GetFPID().GetLibItemName(),
                x=x,
                y=y,
                rotation=rotation,
                layer=layer,
            )
        )

    return components


def extract_pads(pcb_path: Path) -> PadList:
    """KiCad PCBファイルからパッド情報を抽出.

    ペーストレイヤー (F_Paste/B_Paste) に存在するパッドのみ抽出する.

    Args:
        pcb_path: KiCad PCBファイル (.kicad_pcb) のパス

    Returns:
        パッド情報のリスト

    Raises:
        OSError: ファイルが開けない場合
    """
    board = pcbnew.LoadBoard(str(pcb_path))
    pads = PadList()

    for footprint in board.GetFootprints():
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

                # 頂点リスト (nm -> mm変換)
                points = []
                for point in outline.CPoints():
                    x = point.x / 1_000_000.0
                    y = point.y / 1_000_000.0
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
