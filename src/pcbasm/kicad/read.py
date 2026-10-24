"""KiCad からの部品・パッド情報抽出.

このモジュールは KiCad Python API (pcbnew) を使用して
PCB ファイルから部品・パッド情報を抽出する.

Note:
    pcbnew モジュールは KiCad 9.0 以降が必要.

    座標はすべて mm の基板座標へ正規化する（原点は外形 bbox の左上、Y 下向き）.

    DNP（Do Not Populate）の footprint は部品・パッド・銅箔のどれにも含めない.
"""

import logging
from collections import Counter
from pathlib import Path

import attrs
import pcbnew
from shapely import Point as ShapelyPoint, Polygon, get_parts
from shapely.affinity import translate
from shapely.ops import unary_union

from pcbasm.board import Board, Component, Copper, Layer, Outline, Pad
from pcbasm.geometry.polygon import merge_islands
from pcbasm.geometry.transform import Point2d

logger = logging.getLogger(__name__)


def load_board(path: Path) -> Board:
    """KiCad PCB ファイル (.kicad_pcb) を読み、左上原点の基板座標へ正規化した Board を返す.

    Example:
        >>> board = load_board(Path("board.kicad_pcb"))
        >>> print(board.outline.width, board.outline.height)
    """
    kicad = pcbnew.LoadBoard(str(path))
    outline = _outline_polygon(kicad)
    if outline.is_empty:
        origin_x, origin_y = 0.0, 0.0
    else:
        origin_x, origin_y, _, _ = outline.bounds
    return Board(
        outline=Outline(translate(outline, xoff=-origin_x, yoff=-origin_y)),
        components=_components(kicad, origin_x, origin_y),
        pads=_with_unique_refs(_pads(kicad, origin_x, origin_y)),
        copper=_copper(kicad, origin_x, origin_y),
    )


def _outline_polygon(kicad: pcbnew.BOARD) -> Polygon:
    """基板アウトラインのポリゴン（正規化前）."""
    outline_poly_set = pcbnew.SHAPE_POLY_SET()
    kicad.GetBoardPolygonOutlines(outline_poly_set)
    polygons = _shape_poly_set_to_polygons(outline_poly_set, 0.0, 0.0)
    return polygons[0] if polygons else Polygon()


def _components(
    kicad: pcbnew.BOARD, origin_x: float, origin_y: float
) -> tuple[Component, ...]:
    """部品情報（position は footprint 原点、rotation は KiCad の footprint 向き [度]）."""
    components: list[Component] = []

    for footprint in kicad.GetFootprints():
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
                designator=str(footprint.GetReference()),
                value=str(footprint.GetValue()),
                package=str(footprint.GetFPID().GetLibItemName()),
                position=Point2d(x=x, y=y),
                rotation=rotation,
                layer=layer,
            )
        )

    return tuple(components)


def _pads(kicad: pcbnew.BOARD, origin_x: float, origin_y: float) -> list[Pad]:
    """パッド情報.

    F.Paste / B.Paste を持つ pad だけを含める（ペーストを塗らない pad は除く）.

    ペースト形状が複数の輪郭に分かれる pad は、輪郭ごとに別の Pad になる.
    """
    pads: list[Pad] = []

    for footprint in kicad.GetFootprints():
        if footprint.IsDNP():
            continue

        for pad in footprint.Pads():
            layer_set = pad.GetLayerSet()

            if layer_set.Contains(pcbnew.F_Paste):
                target_layer = pcbnew.F_Paste
            elif layer_set.Contains(pcbnew.B_Paste):
                target_layer = pcbnew.B_Paste
            else:
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
                        designator=str(footprint.GetReference()),
                        pad_number=str(pad.GetNumber()),
                        net_name=str(pad.GetNetname()),
                        layer=Layer.TOP
                        if target_layer == pcbnew.F_Paste
                        else Layer.BOTTOM,
                        polygon=Polygon(points),
                        is_custom_shape=pad.GetShape() == pcbnew.PAD_SHAPE_CUSTOM,
                    )
                )

    return pads


def _with_unique_refs(pads: list[Pad]) -> tuple[Pad, ...]:
    """同じ designator と pad_number の pad の ref に、読んだ順の ``#n`` を付ける.

    露出パッドをペースト面だけ分割する KiCad データや、輪郭が分かれた pad がこれに当たる。
    pad_number が空なら ``"<designator>.#n"`` になる。
    """
    counts = Counter((pad.designator, pad.pad_number) for pad in pads)
    seen: Counter[tuple[str, str]] = Counter()
    numbered: list[Pad] = []
    for pad in pads:
        key = (pad.designator, pad.pad_number)
        if counts[key] > 1:
            seen[key] += 1
            pad = attrs.evolve(pad, ref=f"{pad.ref}#{seen[key]}")
        numbered.append(pad)
    return tuple(numbered)


def _copper(
    kicad: pcbnew.BOARD, origin_x: float, origin_y: float
) -> tuple[Copper, ...]:
    """電気的・物理的に接続された銅箔島.

    各レイヤー（Top/Bottom）について、塗りつぶし済みゾーン・トラック・ビア （pcbnew では Track
    扱い）・パッドのポリゴンを集約し、結合・closing 後の 連結成分からビアのドリル穴を除いたものを 1 つの Copper
    として返す.
    """
    # ゾーンの fill キャッシュが stale な場合に備え、読み込み時に 1 回だけ再 fill する
    try:
        pcbnew.ZONE_FILLER(kicad).Fill(kicad.Zones())
    except Exception:
        logger.warning(
            "zoneの再fillに失敗したため、ファイル内のfillキャッシュを使用する",
            exc_info=True,
        )

    max_error = kicad.GetDesignSettings().m_MaxError
    coppers: list[Copper] = []

    def to_polys(sps: pcbnew.SHAPE_POLY_SET) -> list[Polygon]:
        return _shape_poly_set_to_polygons(sps, origin_x, origin_y)

    for kicad_layer, layer in (
        (pcbnew.F_Cu, Layer.TOP),
        (pcbnew.B_Cu, Layer.BOTTOM),
    ):
        polygons: list[Polygon] = []
        via_drill_holes: list[Polygon] = []

        has_zone = False
        zone_polygon_count = 0
        for zone in kicad.Zones():
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

        for track in kicad.GetTracks():
            if not track.GetLayerSet().Contains(kicad_layer):
                continue
            sps = pcbnew.SHAPE_POLY_SET()
            track.TransformShapeToPolygon(
                sps, kicad_layer, 0, max_error, pcbnew.ERROR_INSIDE
            )
            polygons.extend(to_polys(sps))
            if isinstance(track, pcbnew.PCB_VIA) and track.HasDrilledHole():
                via_drill_holes.append(
                    _via_drill_polygon(track, origin_x, origin_y, max_error)
                )

        for footprint in kicad.GetFootprints():
            if footprint.IsDNP():
                continue
            for pad in footprint.Pads():
                if pad.GetLayerSet().Contains(kicad_layer):
                    polygons.extend(to_polys(pad.GetEffectivePolygon(kicad_layer)))

        if not polygons:
            continue

        islands = merge_islands(polygons, snap_mm=2 * _nm_to_mm(max_error))
        drill_area = unary_union(via_drill_holes)
        for island in islands:
            copper = island.difference(drill_area)
            for part in get_parts(copper):
                if isinstance(part, Polygon) and not part.is_empty:
                    coppers.append(Copper(layer=layer, polygon=part))

    return tuple(coppers)


def _via_drill_polygon(
    via: "pcbnew.PCB_VIA",
    origin_x: float,
    origin_y: float,
    max_error: float,
) -> Polygon:
    """ビアの円形ドリル穴を正規化済み shapely Polygon へ変換する."""
    position = via.GetPosition()
    center = ShapelyPoint(
        _nm_to_mm(position.x) - origin_x,
        _nm_to_mm(position.y) - origin_y,
    )
    radius = _nm_to_mm(via.GetDrillValue()) / 2.0
    return center.buffer(radius + _nm_to_mm(max_error), quad_segs=32)


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
