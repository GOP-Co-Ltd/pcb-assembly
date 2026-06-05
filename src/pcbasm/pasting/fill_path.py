"""ペースト塗布用フィルパス生成.

公開 API は :func:`build_paste_fill_path` のみ。ノズル径と塗布パラメータから
マシン固有のヒューリスティクス（線間隔・インセット・フォールバック判定）を
決定し、同モジュール内の private ヘルパー（``_area_fill`` /
``_outline_and_zigzag`` / ``_line_fill`` / ``_dot_fill`` および幾何
ユーティリティ群）に委譲する。

アルゴリズムは「面塗布（外周トレース＋牛耕式ジグザグ）→ 線塗布（最長軸中心線）
→ 点塗布（代表点1点）」のフォールバック階層からなる。面塗布は
``polygon.buffer(-inset)`` の各連結成分ごとに独立したポリラインを生成し、
戻り値は成分別ポリラインのリスト ``list[list[Point2d]]`` となる。
"""

from __future__ import annotations

from shapely import MultiPolygon, Polygon
from shapely.geometry import GeometryCollection, LineString, MultiLineString
from shapely.geometry.base import BaseGeometry

from pcbasm.geometry import Point2d


def build_paste_fill_path(
    polygon: Polygon,
    nozzle_diameter: float,
    *,
    bead_width_factor: float = 1.0,
    overlap: float = 0.0,
    boundary_margin: float = 0.0,
) -> list[list[Point2d]]:
    """ペーストフィルパスを成分別ポリラインのリストとして生成する.

    ノズル径と塗布パラメータからビード幅・線間隔・インセットを決定し、
    面 → 線 → 点 のフォールバック階層で塗布経路を構築する。

    数式::

        w (bead_width) = nozzle_diameter * bead_width_factor
        line_spacing   = w * (1 - overlap)
        inset          = boundary_margin + w / 2     # 面塗布領域 = buffer(-inset)
        end_inset      = boundary_margin + w / 2     # 線塗布の両端内側補正

    フォールバック::

        面塗布（_area_fill）が非空ならそれを返す
        → 線塗布（_line_fill）が非空なら [line] を返す
        → 点塗布（_dot_fill）を [[rep]] として返す（必ず非空）

    Args:
        polygon: 塗布対象のポリゴン（mm単位）
        nozzle_diameter: ノズル内径 [mm]
        bead_width_factor: ビード幅係数（w = nozzle_diameter * bead_width_factor）
        overlap: ジグザグ行間オーバーラップ [0, 1)
        boundary_margin: 外周マージン [mm]

    Returns:
        成分別ポリラインのリスト。各内側ポリラインは1点以上の ``Point2d``。
        入力が空／不正なポリゴンの場合のみ ``[]``。

    Raises:
        ValueError: ``nozzle_diameter`` が0以下、``overlap`` が [0,1) 外、
            ``boundary_margin`` が負、``bead_width_factor`` が0以下の場合
    """
    if nozzle_diameter <= 0:
        raise ValueError(
            f"nozzle_diameterは正の値である必要があります: {nozzle_diameter}"
        )
    if not (0.0 <= overlap < 1.0):
        raise ValueError(f"overlapは[0,1)である必要があります: {overlap}")
    if boundary_margin < 0:
        raise ValueError(
            f"boundary_marginは0以上である必要があります: {boundary_margin}"
        )
    if bead_width_factor <= 0:
        raise ValueError(
            f"bead_width_factorは正の値である必要があります: {bead_width_factor}"
        )

    if polygon.is_empty or not polygon.is_valid:
        return []

    w = nozzle_diameter * bead_width_factor
    line_spacing = w * (1.0 - overlap)
    inset = boundary_margin + w / 2.0
    end_inset = boundary_margin + w / 2.0

    if paths := _area_fill(polygon, line_spacing, inset):
        return paths
    if line := _line_fill(polygon, end_inset):
        return [line]
    return [_dot_fill(polygon)]


def _area_fill(
    polygon: Polygon,
    line_spacing: float,
    inset: float,
) -> list[list[Point2d]]:
    """面塗布パスを成分別ポリラインのリストとして生成する.

    ``polygon.buffer(-inset)`` の各連結成分（``_offset_components``）に
    ``_outline_and_zigzag`` を適用し、空でないポリラインを集めて返す。
    成分が無い（buffer 後が空）場合は ``[]``。
    """
    components = _offset_components(polygon, inset)
    paths: list[list[Point2d]] = []
    for component in components:
        path = _outline_and_zigzag(component, line_spacing)
        if path:
            paths.append(path)
    return paths


def _outline_and_zigzag(
    component: Polygon,
    line_spacing: float,
) -> list[Point2d]:
    """1連結成分の外周トレース＋内部ジグザグを1ポリラインに結合して返す.

    最小実装の方針（牛耕式）:

    1. 外周: ``_ring_coords(component)`` で外環をトレースする。
    2. ジグザグ: ``component.minimum_rotated_rectangle`` から最長軸方向を取り、
       最長軸に垂直なスキャンラインを ``line_spacing`` 間隔で生成する。各
       スキャンライン∩``component`` の区間を最長軸座標でソートし、行ごとに
       方向を交互反転して（牛耕式に）繋ぐ。
    3. 外周 → 内部ジグザグの順で、各遷移を最近傍接続で1ポリラインに結合する。

    点が作れなければ ``[]`` を返す。
    """
    outline = _ring_coords(component)
    zigzag = _zigzag_rows(component, line_spacing)

    path: list[Point2d] = []
    for segment in [outline, *zigzag]:
        if not segment:
            continue
        if path:
            # 直前終端への近さで接続向きを揃える
            if (segment[-1] - path[-1]).norm < (segment[0] - path[-1]).norm:
                segment = list(reversed(segment))
            # 接続ジャンプが成分外を横切る場合は外周経由で繋ぐ（凹成分対策）
            path.extend(_connect_via_outline(path[-1], segment[0], component))
        path.extend(segment)
    return path


def _connect_via_outline(
    start: Point2d,
    end: Point2d,
    component: Polygon,
) -> list[Point2d]:
    """``start`` → ``end`` の接続が成分外を横切る場合の中継点列を返す.

    直線接続 ``[start, end]`` が ``component`` に収まるなら中継不要（``[]``）。
    収まらない場合は、成分外周（``component.exterior``）上で ``start`` / ``end``
    に最も近い頂点の間を外周に沿って辿る中継点列を返す。凹成分で牛耕式の行
    遷移が成分外を横切るケースの局所対処（先回りの一般化はしない）。
    """
    jump = LineString([(start.x, start.y), (end.x, end.y)])
    if component.buffer(1e-9).covers(jump):
        return []

    # _ring_coords は閉環（末尾が始点の重複）なので末尾を除いて巡回頂点とする
    vertices = _ring_coords(component)[:-1]
    n = len(vertices)
    i_start = min(range(n), key=lambda i: (vertices[i] - start).norm)
    i_end = min(range(n), key=lambda i: (vertices[i] - end).norm)

    # 外周を時計回り・反時計回りの両方向で辿り、短い方を採用
    forward = _ring_segment(vertices, i_start, i_end, step=1)
    backward = _ring_segment(vertices, i_start, i_end, step=-1)
    return (
        forward if _polyline_length(forward) <= _polyline_length(backward) else backward
    )


def _ring_segment(
    vertices: list[Point2d],
    i_start: int,
    i_end: int,
    *,
    step: int,
) -> list[Point2d]:
    """``vertices`` を ``i_start`` から ``i_end`` まで ``step`` 方向に巡回した点列."""
    n = len(vertices)
    path: list[Point2d] = []
    i = i_start
    while i != i_end:
        path.append(vertices[i])
        i = (i + step) % n
    path.append(vertices[i_end])
    return path


def _polyline_length(points: list[Point2d]) -> float:
    """ポリラインの総延長を返す."""
    return sum((points[i + 1] - points[i]).norm for i in range(len(points) - 1))


def _zigzag_rows(component: Polygon, line_spacing: float) -> list[list[Point2d]]:
    """成分内部を最長軸方向の牛耕式ジグザグ行として生成する.

    ``minimum_rotated_rectangle`` の最長辺方向を走査方向（最長軸）とし、それに
    垂直なスキャンラインを ``line_spacing`` 間隔で並べる。各スキャンラインと
    ``component`` の交線区間を最長軸座標でソートし、行ごとに方向を交互反転して
    牛耕式に並べた行のリストを返す。交差区間が無ければ空行は含めない。
    """
    if line_spacing <= 0:
        return []

    mrr = component.minimum_rotated_rectangle
    if not isinstance(mrr, Polygon):
        return []
    coords = list(mrr.exterior.coords)
    if len(coords) < 5:
        return []

    corner = Point2d(coords[0][0], coords[0][1])
    edge_a = Point2d(coords[1][0], coords[1][1]) - corner
    edge_b = Point2d(coords[3][0], coords[3][1]) - corner

    # 最長辺方向 = 走査方向（最長軸 u）、もう一方 = 行送り方向 v
    if edge_a.norm >= edge_b.norm:
        long_edge, short_edge = edge_a, edge_b
    else:
        long_edge, short_edge = edge_b, edge_a

    long_len = long_edge.norm
    short_len = short_edge.norm
    if long_len <= 0 or short_len <= 0:
        return []

    u = long_edge * (1.0 / long_len)  # 走査方向（最長軸）の単位ベクトル
    v = short_edge * (1.0 / short_len)  # 行送り方向の単位ベクトル

    # スキャンラインを行送り方向に line_spacing 間隔で配置（両端は半間隔内側）
    n_rows = max(1, int(short_len / line_spacing))
    rows: list[list[Point2d]] = []
    for i in range(n_rows):
        offset = (i + 0.5) * short_len / n_rows
        base = corner + v * offset
        # 最長軸方向に矩形を貫くスキャンライン
        scan = LineString(
            [
                (base.x, base.y),
                ((base + u * long_len).x, (base + u * long_len).y),
            ]
        )
        intervals = _scanline_intervals(scan, component, base, u)
        if not intervals:
            continue
        # 行ごとに走査向きを交互反転（牛耕式）
        if i % 2 == 1:
            intervals = list(reversed([(b, a) for a, b in intervals]))
        for start, end in intervals:
            rows.append([start, end])
    return rows


def _scanline_intervals(
    scan: LineString,
    component: Polygon,
    base: Point2d,
    u: Point2d,
) -> list[tuple[Point2d, Point2d]]:
    """スキャンラインと成分の交線区間を最長軸座標でソートして返す.

    ``scan ∩ component`` の各 ``LineString`` 区間の端点を、最長軸方向 ``u`` への
    射影座標で昇順に並べた ``(start, end)`` のリストを返す。交差が無ければ空。
    """
    inter = scan.intersection(component)
    if inter.is_empty:
        return []

    if isinstance(inter, LineString):
        lines: list[LineString] = [inter]
    elif isinstance(inter, MultiLineString):
        lines = [g for g in inter.geoms if isinstance(g, LineString)]
    else:
        return []

    intervals: list[tuple[Point2d, Point2d]] = []
    for line in lines:
        pts = [Point2d(x, y) for x, y in line.coords]
        if len(pts) < 2:
            continue
        # 端点を最長軸座標でソートして区間化
        pts.sort(key=lambda p: (p - base).x * u.x + (p - base).y * u.y)
        intervals.append((pts[0], pts[-1]))

    intervals.sort(key=lambda seg: (seg[0] - base).x * u.x + (seg[0] - base).y * u.y)
    return intervals


def _line_fill(polygon: Polygon, end_inset: float) -> list[Point2d]:
    """線塗布パスを生成する（``_generate_linear_path`` への委譲）."""
    return _generate_linear_path(polygon, end_inset)


def _dot_fill(polygon: Polygon) -> list[Point2d]:
    """点塗布パスを生成する（代表点1点・必ず非空）."""
    rep = polygon.representative_point()
    return [Point2d(rep.x, rep.y)]


def _generate_linear_path(
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
