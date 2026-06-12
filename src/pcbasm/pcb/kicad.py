"""KiCadからの部品・パッド情報抽出.

このモジュールはKiCad Python API (pcbnew) を使用して
PCBファイルから部品・パッド情報を抽出する.

Note:
    pcbnewモジュールはKiCad 9.0以降が必要.
    すべての座標は基板アウトラインの左上を原点として正規化される.
"""

import logging
from functools import cached_property
from pathlib import Path

import pcbnew
from shapely import Polygon
from shapely.affinity import translate

from pcbasm.geometry.polygon import merge_islands
from pcbasm.geometry.transform import Point2d

from .board import (
    Component,
    ComponentList,
    Copper,
    CopperList,
    Layer,
    Outline,
    Pad,
    PadList,
)

logger = logging.getLogger(__name__)


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

    @cached_property
    def copper(self) -> CopperList:
        """電気的・物理的に接続された銅箔島のリスト（左上原点に正規化済み）.

        各レイヤー（Top/Bottom）について、塗りつぶし済みゾーン・トラック・ビア
        （pcbnewではTrack扱い）・パッドのポリゴンを集約し、結合・closing後の
        連結成分ひとつを1つのCopperとして返す.
        """
        # ゾーンのfillキャッシュがstaleな場合に備え、読み込み時に1回だけ再fillする
        try:
            pcbnew.ZONE_FILLER(self._board).Fill(self._board.Zones())
        except Exception:
            logger.warning(
                "zoneの再fillに失敗したため、ファイル内のfillキャッシュを使用する",
                exc_info=True,
            )

        origin_x, origin_y = self._origin
        max_error = self._board.GetDesignSettings().m_MaxError
        coppers = CopperList()

        def to_polys(sps: pcbnew.SHAPE_POLY_SET) -> list[Polygon]:
            return _shape_poly_set_to_polygons(sps, origin_x, origin_y)

        for kicad_layer, layer in (
            (pcbnew.F_Cu, Layer.TOP),
            (pcbnew.B_Cu, Layer.BOTTOM),
        ):
            polygons: list[Polygon] = []

            has_zone = False
            zone_polygon_count = 0
            for zone in self._board.Zones():
                if zone.IsOnLayer(kicad_layer):
                    has_zone = True
                    zone_polygons = to_polys(zone.GetFilledPolysList(kicad_layer))
                    zone_polygon_count += len(zone_polygons)
                    polygons.extend(zone_polygons)

            if has_zone and zone_polygon_count == 0:
                logger.warning(
                    "レイヤー %s にzoneがあるがfillポリゴンが空 "
                    "(zone fillが空 = PCBデータ不整合の可能性)",
                    layer.name,
                )

            for track in self._board.GetTracks():
                if not track.GetLayerSet().Contains(kicad_layer):
                    continue
                sps = pcbnew.SHAPE_POLY_SET()
                track.TransformShapeToPolygon(
                    sps, kicad_layer, 0, max_error, pcbnew.ERROR_INSIDE
                )
                polygons.extend(to_polys(sps))

            for footprint in self._board.GetFootprints():
                if footprint.IsDNP():
                    continue
                for pad in footprint.Pads():
                    if pad.GetLayerSet().Contains(kicad_layer):
                        polygons.extend(to_polys(pad.GetEffectivePolygon(kicad_layer)))

            if not polygons:
                continue

            islands = merge_islands(polygons, snap_mm=2 * _nm_to_mm(max_error))
            for island in islands:
                coppers.append(Copper(layer=layer, polygon=island))

        return coppers


def _shape_poly_set_to_polygons(
    sps: "pcbnew.SHAPE_POLY_SET", origin_x: float, origin_y: float
) -> list[Polygon]:
    """SHAPE_POLY_SET を shapely Polygon のリストに変換（nm→mm、原点平行移動）."""

    def ring(outline: "pcbnew.SHAPE_LINE_CHAIN") -> list[tuple[float, float]]:
        points = [
            (_nm_to_mm(p.x) - origin_x, _nm_to_mm(p.y) - origin_y)
            for p in outline.CPoints()
        ]
        if len(points) >= 3 and points[0] != points[-1]:
            points.append(points[0])
        return points

    polygons: list[Polygon] = []
    for i in range(sps.OutlineCount()):
        exterior = ring(sps.Outline(i))
        if len(exterior) < 3:
            continue
        holes = [
            h
            for h in (ring(sps.CHole(i, j)) for j in range(sps.HoleCount(i)))
            if len(h) >= 3
        ]
        polygon = Polygon(exterior, holes=holes)
        if not polygon.is_empty:
            polygons.append(polygon)

    return polygons


def _nm_to_mm(nm: float) -> float:
    """ナノメートルをミリメートルに変換."""
    return nm / 1_000_000.0
