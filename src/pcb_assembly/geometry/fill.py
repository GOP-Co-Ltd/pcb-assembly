"""ポリゴン塗りつぶしパスの生成."""

from __future__ import annotations

import math

from shapely import LineString, MultiLineString, Polygon
from shapely.affinity import rotate
from shapely.geometry import GeometryCollection
from shapely.geometry.base import BaseGeometry

from .transform import Point2d


def generate_fill_path(
    polygon: Polygon,
    line_spacing: float,
    perimeters: int = 1,
    inset: float = 0.0,
    angle: float = 0.0,
) -> list[Point2d]:
    """ポリゴンの塗りつぶしパスを生成する.

    外周をperimeters回周回した後、残った内部をジグザグ走査線で塗りつぶす。
    1周目はinset分内側にオフセットされ、以降はline_spacingずつ内側にオフセットされる。

    Args:
        polygon: 塗りつぶし対象のポリゴン（mm単位）
        line_spacing: 走査線間隔（mm）
        perimeters: 外周の周回数（デフォルト: 1）
        inset: 1周目の外周からのオフセット（mm、デフォルト: 0.0）
        angle: ジグザグ走査線の角度（度、デフォルト: 0.0 = 水平）

    Returns:
        外周パス＋ジグザグ塗りつぶしパスの座標リスト

    Raises:
        ValueError: line_spacingが0以下、またはperimetersが0未満の場合
    """
    if line_spacing <= 0:
        raise ValueError(f"line_spacingは正の値である必要があります: {line_spacing}")
    if perimeters < 0:
        raise ValueError(f"perimetersは0以上である必要があります: {perimeters}")

    if polygon.is_empty or not polygon.is_valid:
        return []

    # 外周の周回パスを生成
    contour_path = _generate_contours(polygon, line_spacing, perimeters, inset)

    # 最後の周回の内側がジグザグ領域
    total_offset = inset + perimeters * line_spacing
    remaining = polygon.buffer(-total_offset)
    if remaining.is_empty:
        return contour_path

    # ジグザグの開始点を決定
    cx = polygon.centroid.x
    cy = polygon.centroid.y
    if contour_path:
        start_point = contour_path[-1]
    else:
        # perimeters=0の場合、ポリゴンの最初の頂点を基準にする
        coords = list(polygon.exterior.coords)
        start_point = Point2d(x=coords[0][0], y=coords[0][1])

    # 角度対応: ジグザグ計算用に回転
    if angle != 0:
        rotated_remaining = rotate(remaining, -angle, origin="centroid")
        rad_neg = math.radians(-angle)
        start_point_rotated = _rotate_point(start_point, cx, cy, rad_neg)
    else:
        rotated_remaining = remaining
        start_point_rotated = start_point

    zigzag_path = _generate_zigzag(rotated_remaining, line_spacing, start_point_rotated)

    # 角度対応: ジグザグパスを元の角度に戻す
    if angle != 0 and zigzag_path:
        rad = math.radians(angle)
        zigzag_path = [_rotate_point(p, cx, cy, rad) for p in zigzag_path]

    return contour_path + zigzag_path


def generate_fill_path_for_nozzle(
    polygon: Polygon,
    nozzle_diameter: float,
    overlap_ratio: float = 0.2,
    perimeters: int = 1,
    angle: float = 0.0,
) -> list[Point2d]:
    """ノズル直径から塗りつぶしパスを生成する.

    ノズル直径とオーバーラップ率からline_spacingとinsetを自動計算する。
    insetはノズル半径（nozzle_diameter / 2）に設定される。

    Args:
        polygon: 塗りつぶし対象のポリゴン（mm単位）
        nozzle_diameter: ノズル直径（mm）
        overlap_ratio: 走査線のオーバーラップ率（0.0〜1.0未満、デフォルト: 0.2）
        perimeters: 外周の周回数（デフォルト: 1）
        angle: ジグザグ走査線の角度（度、デフォルト: 0.0 = 水平）

    Returns:
        外周パス＋ジグザグ塗りつぶしパスの座標リスト

    Raises:
        ValueError: nozzle_diameterが0以下、またはoverlap_ratioが範囲外の場合
    """
    if nozzle_diameter <= 0:
        raise ValueError(
            f"nozzle_diameterは正の値である必要があります: {nozzle_diameter}"
        )
    if not (0.0 <= overlap_ratio < 1.0):
        raise ValueError(
            f"overlap_ratioは0.0以上1.0未満である必要があります: {overlap_ratio}"
        )

    line_spacing = nozzle_diameter * (1.0 - overlap_ratio)
    inset = nozzle_diameter / 2.0

    return generate_fill_path(
        polygon, line_spacing, perimeters=perimeters, inset=inset, angle=angle
    )


def _generate_contours(
    polygon: Polygon, line_spacing: float, perimeters: int, inset: float
) -> list[Point2d]:
    """外周の周回パスを生成する.

    1周目はinset分内側にオフセットされ、以降はline_spacingずつ内側にオフセットされる。
    前の周回の終端に近い点から開始して連続パスを形成する。
    """
    path: list[Point2d] = []

    for i in range(perimeters):
        offset = inset + i * line_spacing
        if offset == 0:
            contour_polygon = polygon
        else:
            contour_polygon = polygon.buffer(-offset)
            if contour_polygon.is_empty:
                break

        coords = list(contour_polygon.exterior.coords)
        ring = [Point2d(x=x, y=y) for x, y in coords]

        if path:
            # 前の周回の終端に近い点から開始するよう回転
            ring = _rotate_ring_to_nearest(ring, path[-1])

        path.extend(ring)

    return path


def _rotate_ring_to_nearest(ring: list[Point2d], target: Point2d) -> list[Point2d]:
    """閉じたリングを、targetに最も近い点から開始するように回転する."""
    # 最後の点は最初の点と同じ（閉ループ）なので除外して検索
    points = ring[:-1]
    if not points:
        return ring

    min_idx = 0
    min_dist = (points[0] - target).norm
    for i, p in enumerate(points[1:], 1):
        d = (p - target).norm
        if d < min_dist:
            min_dist = d
            min_idx = i

    # 回転して閉ループを再構成
    rotated = points[min_idx:] + points[:min_idx]
    rotated.append(rotated[0])
    return rotated


def _rotate_point(p: Point2d, cx: float, cy: float, rad: float) -> Point2d:
    """点を(cx, cy)を中心にrad[ラジアン]回転する."""
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)
    return Point2d(
        x=cos_a * (p.x - cx) - sin_a * (p.y - cy) + cx,
        y=sin_a * (p.x - cx) + cos_a * (p.y - cy) + cy,
    )


def _generate_zigzag(
    fill_polygon: Polygon, line_spacing: float, start_point: Point2d
) -> list[Point2d]:
    """ポリゴン内部のジグザグパスを生成する.

    外周の終端に近い端からジグザグを開始し、一筆書きを実現する。
    """
    min_x, min_y, max_x, max_y = fill_polygon.bounds

    # 走査線のy座標を生成
    scan_y_values: list[float] = []
    y = min_y + line_spacing
    while y < max_y:
        scan_y_values.append(y)
        y += line_spacing

    if not scan_y_values:
        return []

    # 外周終端が上側に近ければ上から走査、下側に近ければ下から走査
    dist_to_bottom = abs(start_point.y - scan_y_values[0])
    dist_to_top = abs(start_point.y - scan_y_values[-1])
    if dist_to_top < dist_to_bottom:
        scan_y_values = list(reversed(scan_y_values))

    # 最初の走査線の開始方向を決定
    first_segments = _first_nonempty_segments(fill_polygon, scan_y_values, min_x, max_x)
    if first_segments:
        left_x = first_segments[0][0]
        right_x = first_segments[-1][1]
        start_left = abs(start_point.x - left_x) <= abs(start_point.x - right_x)
    else:
        start_left = True

    zigzag: list[Point2d] = []
    for i, y in enumerate(scan_y_values):
        line = LineString([(min_x - 1.0, y), (max_x + 1.0, y)])
        intersection = fill_polygon.intersection(line)
        segments = _extract_segments(intersection)

        if not segments:
            continue

        left_to_right = (i % 2 == 0) == start_left
        if left_to_right:
            for start_x, end_x in segments:
                zigzag.append(Point2d(x=start_x, y=y))
                zigzag.append(Point2d(x=end_x, y=y))
        else:
            for start_x, end_x in reversed(segments):
                zigzag.append(Point2d(x=end_x, y=y))
                zigzag.append(Point2d(x=start_x, y=y))

    return zigzag


def _first_nonempty_segments(
    fill_polygon: Polygon,
    scan_y_values: list[float],
    min_x: float,
    max_x: float,
) -> list[tuple[float, float]]:
    """走査線リストの中で最初に交差セグメントがある結果を返す."""
    for y in scan_y_values:
        line = LineString([(min_x - 1.0, y), (max_x + 1.0, y)])
        segments = _extract_segments(fill_polygon.intersection(line))
        if segments:
            return segments
    return []


def _extract_segments(
    geometry: BaseGeometry,
) -> list[tuple[float, float]]:
    """交差結果から走査線セグメントの(start_x, end_x)リストを抽出する."""
    if geometry.is_empty:
        return []

    if isinstance(geometry, LineString):
        coords = list(geometry.coords)
        if len(coords) >= 2:
            xs = [c[0] for c in coords]
            return [(min(xs), max(xs))]
        return []

    if isinstance(geometry, (MultiLineString, GeometryCollection)):
        result: list[tuple[float, float]] = []
        for geom in geometry.geoms:
            if isinstance(geom, LineString):
                coords = list(geom.coords)
                if len(coords) >= 2:
                    xs = [c[0] for c in coords]
                    result.append((min(xs), max(xs)))
        result.sort()
        return result

    return []
