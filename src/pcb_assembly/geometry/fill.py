"""ポリゴン塗りつぶしパスの生成."""

from __future__ import annotations

from shapely import LineString, MultiLineString, Polygon
from shapely.affinity import rotate
from shapely.geometry import GeometryCollection
from shapely.geometry.base import BaseGeometry

from .transform import Compose, Point2d, Rotation, Shift


def generate_fill_path(
    polygon: Polygon,
    nozzle_diameter: float,
    perimeters: int = 1,
    angle: float = 0.0,
) -> list[Point2d]:
    """ポリゴンの塗りつぶしパスを生成する.

    走査線間隔とインセットはノズル径から自動決定される
    （line_spacing = nozzle_diameter, inset = nozzle_diameter / 2）。
    外周をperimeters回周回した後、残った内部をジグザグ走査線で塗りつぶす。

    `polygon.buffer(-nozzle_diameter)` が空となる細長いポリゴンでは、
    zigzag を生成すると経路が短く中心にはんだが過多になるため、
    早期に最長軸に沿った直線フォールバックへ切り替える。

    Args:
        polygon: 塗りつぶし対象のポリゴン（mm単位）
        nozzle_diameter: ノズル内径（mm）。線間とインセットの基準。
        perimeters: 外周の周回数（デフォルト: 1）
        angle: ジグザグ走査線の角度（度、デフォルト: 0.0 = 水平）

    Returns:
        外周パス＋ジグザグ塗りつぶしパスの座標リスト

    Raises:
        ValueError: nozzle_diameterが0以下、またはperimetersが0未満の場合
    """
    if nozzle_diameter <= 0:
        raise ValueError(
            f"nozzle_diameterは正の値である必要があります: {nozzle_diameter}"
        )
    if perimeters < 0:
        raise ValueError(f"perimetersは0以上である必要があります: {perimeters}")

    if polygon.is_empty or not polygon.is_valid:
        return []

    line_spacing = nozzle_diameter
    inset = nozzle_diameter / 2

    # 細長ポリゴンは zigzag が短く中心にはんだが過多になるため、最長軸の直線で代替する
    if polygon.buffer(-nozzle_diameter).is_empty:
        return _generate_linear_fallback(polygon, nozzle_diameter)

    # 外周の周回パスを生成
    contour_path = _generate_contours(polygon, line_spacing, perimeters, inset)

    # 最後の周回の内側がジグザグ領域。perimeters=0 など早期 fallback を抜けても
    # ジグザグ領域が空となる稀なケースのセーフティネットとして fallback を残す
    total_offset = inset + perimeters * line_spacing
    remaining = polygon.buffer(-total_offset)
    if remaining.is_empty:
        return contour_path or _generate_linear_fallback(polygon, nozzle_diameter)

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
        rot = Compose([Shift(-cx, -cy), Rotation(degrees=angle), Shift(cx, cy)])
        rotated_remaining = rotate(remaining, -angle, origin="centroid")
        start_point_rotated = rot.inverse().apply(start_point)
    else:
        rot = None
        rotated_remaining = remaining
        start_point_rotated = start_point

    zigzag_path = _generate_zigzag(rotated_remaining, line_spacing, start_point_rotated)

    # 角度対応: ジグザグパスを元の角度に戻す
    if rot is not None and zigzag_path:
        zigzag_path = list(map(rot.apply, zigzag_path))

    # 上記分岐をすべてすり抜けて空になる稀なケース（数値誤差等）の最終セーフティネット
    result = contour_path + zigzag_path
    if not result:
        return _generate_linear_fallback(polygon, nozzle_diameter)
    return result


def _generate_linear_fallback(
    polygon: Polygon, nozzle_diameter: float
) -> list[Point2d]:
    """ポリゴンの最長軸に沿った直線パスを生成する.

    通常のcontour+zigzagが空になる細長いポリゴン向けのフォールバック。
    minimum_rotated_rectangleの短辺中点を結ぶ直線を、両端をnozzle_diameter/2ずつ
    内側に補正して返す。長辺がnozzle_diameter未満の場合は空リストを返す。
    """
    mrr = polygon.minimum_rotated_rectangle
    if not isinstance(mrr, Polygon):
        mrr = Polygon(mrr.coords)
    coords = list(mrr.exterior.coords)
    mids = [
        Point2d(
            (coords[i][0] + coords[i + 1][0]) / 2, (coords[i][1] + coords[i + 1][1]) / 2
        )
        for i in range(4)
    ]
    # 対向する中点ペアのうち長い方（=最長軸の中心線）
    if (mids[2] - mids[0]).norm >= (mids[3] - mids[1]).norm:
        start, end = mids[0], mids[2]
    else:
        start, end = mids[1], mids[3]

    direction = end - start
    length = direction.norm
    if length <= nozzle_diameter:
        return []

    inset = nozzle_diameter / 2
    unit = direction * (1.0 / length)
    return [start + unit * inset, end - unit * inset]


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

        path.extend(_truncate_ring(ring, line_spacing))

    return path


def _truncate_ring(ring: list[Point2d], gap: float) -> list[Point2d]:
    """閉じたリングを、gap分手前で切り詰めたオープンパスを返す."""
    points = ring[:-1]
    if not points:
        return []

    n = len(points)
    perimeter = sum((points[(i + 1) % n] - points[i]).norm for i in range(n))
    target = perimeter - gap
    if target <= 0:
        return [points[0]]

    path = [points[0]]
    accumulated = 0.0
    for i in range(n):
        next_p = points[(i + 1) % n]
        edge_len = (next_p - points[i]).norm
        if accumulated + edge_len >= target:
            t = (target - accumulated) / edge_len if edge_len > 0 else 0.0
            path.append(points[i] + (next_p - points[i]) * t)
            break
        accumulated += edge_len
        path.append(next_p)
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
