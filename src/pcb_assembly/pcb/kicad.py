"""KiCadからの部品・パッド情報抽出.

このモジュールはKiCad Python API (pcbnew) を使用して
PCBファイルから部品・パッド情報を抽出する.

Note:
    pcbnewモジュールはKiCad 9.0以降が必要.
    すべての座標は基板アウトラインの左上を原点として正規化される.
"""

from functools import cached_property
from pathlib import Path

import pcbnew
from shapely import Polygon
from shapely.affinity import translate

from pcb_assembly.geometry.transform import Point2d

from .board import Component, ComponentList, Layer, Outline, Pad, PadList


class PcbFile:
    """KiCad PCBファイル.

    PCBファイルから部品・パッド・アウトライン情報を抽出する.
    各プロパティは遅延読み込みされ、結果はキャッシュされる.

    Args:
        pcb_path: KiCad PCBファイル (.kicad_pcb) のパス

    Example:
        >>> pcb = PcbFile(Path("board.kicad_pcb"))
        >>> print(pcb.outline.width, pcb.outline.height)
        >>> for comp in pcb.components:
        ...     print(comp.designator)
    """

    def __init__(self, pcb_path: Path) -> None:
        self._board: pcbnew.BOARD = pcbnew.LoadBoard(str(pcb_path))

    @cached_property
    def _original_outline_polygon(self) -> Polygon:
        """基板アウトラインのポリゴン（正規化前）."""
        outline_poly_set = pcbnew.SHAPE_POLY_SET()
        self._board.GetBoardPolygonOutlines(outline_poly_set)

        if outline_poly_set.OutlineCount() == 0:
            return Polygon()

        outline = outline_poly_set.Outline(0)
        points = [(_nm_to_mm(p.x), _nm_to_mm(p.y)) for p in outline.CPoints()]

        if not points:
            return Polygon()

        if points[0] != points[-1]:
            points.append(points[0])

        return Polygon(points)

    @cached_property
    def _origin(self) -> tuple[float, float]:
        """基板アウトラインの原点（左上座標）."""
        if self._original_outline_polygon.is_empty:
            return (0.0, 0.0)
        origin_x, origin_y, _, _ = self._original_outline_polygon.bounds
        return (origin_x, origin_y)

    @cached_property
    def outline(self) -> Outline:
        """基板アウトライン（左上原点に正規化済み）."""
        polygon = self._original_outline_polygon
        if polygon.is_empty:
            return Outline(Polygon())

        origin_x, origin_y = self._origin
        normalized_polygon = translate(polygon, xoff=-origin_x, yoff=-origin_y)
        return Outline(normalized_polygon)

    @cached_property
    def components(self) -> ComponentList:
        """部品情報のリスト（左上原点に正規化済み）."""
        origin_x, origin_y = self._origin
        components = ComponentList()

        for footprint in self._board.GetFootprints():
            if footprint.IsDNP():
                continue

            if footprint.IsFlipped():
                layer = Layer.BOTTOM
            else:
                layer = Layer.TOP

            pos = footprint.GetPosition()
            x = _nm_to_mm(pos.x) - origin_x
            y = _nm_to_mm(pos.y) - origin_y

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

    @cached_property
    def pads(self) -> PadList:
        """パッド情報のリスト（左上原点に正規化済み）."""
        origin_x, origin_y = self._origin
        pads = PadList()

        for footprint in self._board.GetFootprints():
            if footprint.IsDNP():
                continue

            for pad in footprint.Pads():
                layer_set = pad.GetLayerSet()

                target_layer = None
                if layer_set.Contains(pcbnew.F_Paste):
                    target_layer = pcbnew.F_Paste
                elif layer_set.Contains(pcbnew.B_Paste):
                    target_layer = pcbnew.B_Paste

                if target_layer is None:
                    continue

                shape_poly_set = pad.GetEffectivePolygon(target_layer)

                for outline_idx in range(shape_poly_set.OutlineCount()):
                    outline = shape_poly_set.Outline(outline_idx)

                    points = []
                    for point in outline.CPoints():
                        x = _nm_to_mm(point.x) - origin_x
                        y = _nm_to_mm(point.y) - origin_y
                        points.append((x, y))

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


def _nm_to_mm(nm: float) -> float:
    """ナノメートルをミリメートルに変換."""
    return nm / 1_000_000.0
