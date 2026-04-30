"""ポリゴン塗りつぶしパスの生成."""

from __future__ import annotations

from shapely import MultiPolygon, Polygon
from shapely.geometry import GeometryCollection
from shapely.geometry.base import BaseGeometry

from .transform import Point2d


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


def generate_spiral_path(
    polygon: Polygon,
    line_spacing: float,
    initial_inset: float,
) -> list[Point2d]:
    """中心起点でポリゴン形状追従の連続螺旋パスを生成する.

    パスの先頭点は ``polygon.representative_point()`` （凹形でも内部保証）に
    固定され、そこから最内リングへ最短ジャンプして以降は ``truncate + rotate``
    方式でポリゴン形状追従の同心オフセットリングを innermost → outermost に
    辿る。最外周のみ truncate せず完走する。``polygon`` が複数連結成分に
    またがる場合（凹形状の ``buffer(-d)`` が分裂する場合を含む）、各成分の
    螺旋を最近傍順で連結する（最初の成分は中心起点に最も近いものを選択）。

    Args:
        polygon: 対象ポリゴン（mm単位）
        line_spacing: リング間隔（mm、正の値）
        initial_inset: 最内のオフセット深さ（mm、0以上）

    Returns:
        中心点を先頭とする innermost-first の連続螺旋座標列。
        ``polygon.buffer(-initial_inset)`` が空、もしくは入力が空／不正の場合
        は ``[]``。

    Raises:
        ValueError: ``line_spacing`` が0以下、または ``initial_inset`` が負の場合
    """
    if line_spacing <= 0:
        raise ValueError(f"line_spacingは正の値である必要があります: {line_spacing}")
    if initial_inset < 0:
        raise ValueError(f"initial_insetは0以上である必要があります: {initial_inset}")

    if polygon.is_empty or not polygon.is_valid:
        return []

    components = _offset_components(polygon, 0.0)
    if not components:
        return []

    rep = polygon.representative_point()
    anchor = Point2d(rep.x, rep.y)

    spirals: list[list[Point2d]] = []
    for component in components:
        spiral = _spiral_one_component(
            component, line_spacing, initial_inset, anchor=anchor
        )
        if spiral:
            spirals.append(spiral)

    if not spirals:
        return []

    return [anchor] + _connect_nearest(spirals, anchor=anchor)


def generate_linear_path(
    polygon: Polygon,
    end_inset: float,
) -> list[Point2d]:
    """ポリゴンの最長軸に沿った2点パスを生成する.

    ``polygon.minimum_rotated_rectangle`` の長軸の中央線を求め、両端を
    ``end_inset`` だけ内側に補正した2点パスを返す。長軸長が
    ``2 * end_inset`` 以下の場合は ``[]`` を返す。

    Args:
        polygon: 対象ポリゴン（mm単位）
        end_inset: 両端の内側補正量（mm、0以上）

    Returns:
        ``[start, end]`` の2点パス。長軸が短すぎる、または入力が空／不正な
        場合は ``[]``。

    Raises:
        ValueError: ``end_inset`` が負の場合
    """
    if end_inset < 0:
        raise ValueError(f"end_insetは0以上である必要があります: {end_inset}")

    if polygon.is_empty or not polygon.is_valid:
        return []

    mrr = polygon.minimum_rotated_rectangle
    if not isinstance(mrr, Polygon):
        return []
    coords = list(mrr.exterior.coords)
    if len(coords) < 5:
        return []

    mids = [
        Point2d(
            (coords[i][0] + coords[i + 1][0]) / 2,
            (coords[i][1] + coords[i + 1][1]) / 2,
        )
        for i in range(4)
    ]
    if (mids[2] - mids[0]).norm >= (mids[3] - mids[1]).norm:
        start, end = mids[0], mids[2]
    else:
        start, end = mids[1], mids[3]

    direction = end - start
    length = direction.norm
    if length <= 2 * end_inset:
        return []

    unit = direction * (1.0 / length)
    return [start + unit * end_inset, end - unit * end_inset]


def generate_concentric_rings(
    polygon: Polygon,
    line_spacing: float,
    initial_inset: float,
) -> list[list[Point2d]]:
    """同心オフセットリング群を innermost first で返す.

    ``polygon.buffer(-(initial_inset + k * line_spacing))`` を ``k=0,1,...``
    と進めて空になるまでリングを集め、最後のリスト要素が最も外側になる順序
    （innermost first）で返す。リング間の連続接続は行わない（各リングは
    独立した閉ポリラインとなる）。MultiPolygon の場合は各連結成分について
    独立にリング群を計算し、すべてを連結したリストを返す。

    Args:
        polygon: 対象ポリゴン（mm単位）
        line_spacing: リング間隔（mm、正の値）
        initial_inset: 最外のオフセット深さ（mm、0以上）

    Returns:
        innermost-first のリング群（各リングは閉ポリラインの座標列）。
        空／不正ポリゴンの場合は ``[]``。

    Raises:
        ValueError: ``line_spacing`` が0以下、または ``initial_inset`` が負の場合
    """
    if line_spacing <= 0:
        raise ValueError(f"line_spacingは正の値である必要があります: {line_spacing}")
    if initial_inset < 0:
        raise ValueError(f"initial_insetは0以上である必要があります: {initial_inset}")

    if polygon.is_empty or not polygon.is_valid:
        return []

    components = _offset_components(polygon, 0.0)
    if not components:
        return []

    all_rings: list[list[Point2d]] = []
    for component in components:
        all_rings.extend(
            _concentric_rings_one_component(component, line_spacing, initial_inset)
        )
    return all_rings


def _offset_components(polygon: Polygon, depth: float) -> list[Polygon]:
    """``polygon.buffer(-depth)`` の結果から ``Polygon`` のみを抽出する.

    MultiPolygon は連結成分に分解する。Point/LineString/GeometryCollection
    内の非Polygon要素は除外する。空・不正な結果は空リストとなる。
    """
    if depth > 0:
        offset = polygon.buffer(-depth)
    else:
        offset = polygon

    if offset.is_empty:
        return []

    if isinstance(offset, Polygon):
        if offset.is_valid and not offset.is_empty:
            return [offset]
        return []

    if isinstance(offset, (MultiPolygon, GeometryCollection)):
        geoms: list[BaseGeometry] = list(offset.geoms)
        return [g for g in geoms if isinstance(g, Polygon) and not g.is_empty]

    return []


def _ring_coords(polygon: Polygon) -> list[Point2d]:
    """``polygon.exterior.coords`` を ``Point2d`` リストとして返す.

    TODO: 内側ホール（``polygon.interiors``）には未対応。PCBパッドにホールは
    ほぼ存在しないため当面は外環のみを扱う。
    """
    return [Point2d(x=x, y=y) for x, y in polygon.exterior.coords]


def _spiral_one_component(
    component: Polygon,
    line_spacing: float,
    initial_inset: float,
    *,
    anchor: Point2d | None = None,
) -> list[Point2d]:
    """単一連結成分の螺旋を生成する.

    深さ ``initial_inset`` から ``line_spacing`` ずつ内側にオフセットしながら
    リングを集める。途中で複数連結成分に分裂した場合、各サブ成分から再帰的
    に螺旋を構成し、それを innermost として既存リング群（外側）の前に置く。

    ``anchor`` 指定時は最内リングを anchor 最近傍頂点から開始するよう回転する
    （最終的に呼び出し側が anchor を path 先頭に前置することを想定）。サブ
    成分への再帰では ``anchor=None`` として伝播しない。
    """
    rings: list[list[Point2d]] = []  # outer -> inner の順
    depth = initial_inset

    while True:
        sub_components = _offset_components(component, depth)
        if not sub_components:
            break
        if len(sub_components) > 1:
            # 螺旋途中での分裂: 各サブ成分の螺旋を innermost として接続
            sub_components_sorted = _sort_components_by_seed(
                sub_components, component.centroid
            )
            sub_spirals: list[list[Point2d]] = []
            for sub in sub_components_sorted:
                sub_spiral = _spiral_one_component(
                    sub, line_spacing, depth + line_spacing
                )
                if sub_spiral:
                    sub_spirals.append(sub_spiral)
            inner_path = _connect_nearest(sub_spirals) if sub_spirals else []
            outer_path = _walk_rings_outward(rings, anchor=anchor)
            return inner_path + outer_path

        rings.append(_ring_coords(sub_components[0]))
        depth += line_spacing

    return _walk_rings_outward(rings, anchor=anchor)


def _sort_components_by_seed(
    components: list[Polygon], reference: BaseGeometry
) -> list[Polygon]:
    """連結成分を参照点からの距離順にソートする（決定論性確保）."""
    return sorted(components, key=lambda c: c.centroid.distance(reference))


def _walk_rings_outward(
    rings_outer_to_inner: list[list[Point2d]],
    *,
    anchor: Point2d | None = None,
) -> list[Point2d]:
    """``outer -> inner`` 順に蓄積されたリング群から innermost-first 螺旋を生成する.

    最内リングから走査を開始し、各リング遷移時に直前の終端に最も近い頂点へ
    rotate して接続する。最外周以外は ``line_spacing`` 分 truncate して隣接
    リングへの遷移代を確保し、最外周は rotate のみで完走する。

    ``anchor`` 指定時は最内リング（リング1本のみのケースではそのリング）を
    anchor 最近傍頂点から開始するよう回転する。
    """
    if not rings_outer_to_inner:
        return []

    # 最後の要素（最も外側）以外を innermost first で走査
    rings_inner_to_outer = list(reversed(rings_outer_to_inner))

    if len(rings_inner_to_outer) == 1:
        # リングが1本のみの場合は最外周なので完走
        ring = rings_inner_to_outer[0]
        if not ring:
            return []
        if anchor is not None:
            return _rotate_ring_to_nearest(ring, anchor)
        return ring[:]

    # 隣接リング遷移代を計算するため、各リング間の距離（line_spacing相当）が必要だが
    # ここでは呼び出し側 (_spiral_one_component) で常に等間隔オフセットされた
    # リング群が来る前提で、ring間の最短距離を line_spacing として採用する。
    line_spacing = _estimate_ring_spacing(rings_inner_to_outer)

    path: list[Point2d] = []
    # 最内リング: anchor 指定時は anchor 最近傍頂点から、未指定なら先頭から開始
    innermost = rings_inner_to_outer[0]
    if not innermost:
        return []
    if anchor is not None:
        innermost = _rotate_ring_to_nearest(innermost, anchor)
    path.extend(_truncate_ring(innermost, line_spacing))

    # 中間リング: 直前点に最も近い頂点まで rotate、truncate
    for ring in rings_inner_to_outer[1:-1]:
        if not ring:
            continue
        rotated = _rotate_ring_to_nearest(ring, path[-1]) if path else ring
        path.extend(_truncate_ring(rotated, line_spacing))

    # 最外リング: rotate のみ、truncate なし
    outermost = rings_inner_to_outer[-1]
    if outermost:
        rotated_outer = (
            _rotate_ring_to_nearest(outermost, path[-1]) if path else outermost
        )
        path.extend(rotated_outer)

    return path


def _estimate_ring_spacing(rings_inner_to_outer: list[list[Point2d]]) -> float:
    """隣接リング間の代表的な間隔を推定する.

    最内2リング間の最短頂点距離を返す。1本のみなら ``0.0``（呼び出し側で
    最外周の完走パスに使われ、``_truncate_ring`` には渡されない）。
    """
    if len(rings_inner_to_outer) < 2:
        return 0.0
    inner = rings_inner_to_outer[0][:-1]
    outer = rings_inner_to_outer[1][:-1]
    if not inner or not outer:
        return 0.0
    min_dist = float("inf")
    for p in inner:
        for q in outer:
            d = (p - q).norm
            if d < min_dist:
                min_dist = d
    return min_dist if min_dist != float("inf") else 0.0


def _connect_nearest(
    spirals: list[list[Point2d]],
    *,
    anchor: Point2d | None = None,
) -> list[Point2d]:
    """複数の螺旋を最近傍順で連結する.

    ``anchor`` 指定時は最初の螺旋を「先頭点が anchor 最近傍」のものから
    始める。未指定時は決定論的シードとして「先頭点が最も左、同一xなら最も
    下」のものを選ぶ。以降は直前の終端に最も近い先頭点を持つ螺旋を選んで
    連結する。``spirals`` が1要素の場合はそのまま返す。
    """
    if not spirals:
        return []
    if len(spirals) == 1:
        return spirals[0][:]

    remaining = [s for s in spirals if s]
    if not remaining:
        return []

    if anchor is not None:
        seed_idx = min(
            range(len(remaining)),
            key=lambda i: (remaining[i][0] - anchor).norm,
        )
    else:
        # シード: 先頭点が最も左→最も下
        seed_idx = min(
            range(len(remaining)),
            key=lambda i: (remaining[i][0].x, remaining[i][0].y),
        )
    result = remaining.pop(seed_idx)[:]

    while remaining:
        end = result[-1]
        next_idx = min(
            range(len(remaining)),
            key=lambda i: (remaining[i][0] - end).norm,
        )
        result.extend(remaining.pop(next_idx))

    return result


def _concentric_rings_one_component(
    component: Polygon,
    line_spacing: float,
    initial_inset: float,
) -> list[list[Point2d]]:
    """単一成分の同心リング群を innermost first で返す.

    途中で分裂した場合は各サブ成分について再帰的に集める（深さは継続）。
    """
    rings_outer_to_inner: list[list[Point2d]] = []
    depth = initial_inset

    while True:
        sub_components = _offset_components(component, depth)
        if not sub_components:
            break
        if len(sub_components) > 1:
            inner_rings: list[list[Point2d]] = []
            for sub in sub_components:
                inner_rings.extend(
                    _concentric_rings_one_component(sub, line_spacing, depth)
                )
            # innermost first: サブ成分のリング群を先頭に、外側リングを後ろに
            outer_rings_inner_first = list(reversed(rings_outer_to_inner))
            return inner_rings + outer_rings_inner_first

        rings_outer_to_inner.append(_ring_coords(sub_components[0]))
        depth += line_spacing

    return list(reversed(rings_outer_to_inner))
