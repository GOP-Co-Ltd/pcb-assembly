"""ペースト塗布用フィルパス生成.

公開 API は :func:`build_paste_fill_plan`（互換の :func:`build_paste_fill_path`）
と、解決済み塗布設定から引数を束ねる :func:`build_pad_fill_plan_for`。ノズル径と
塗布パラメータからマシン固有のヒューリスティクス（線間隔・インセット・
フォールバック判定）を決定し、同モジュール内の private ヘルパー（``_area_fill`` /
``_outline_and_zigzag`` / ``_line_fill`` / ``_dot_fill`` および幾何
ユーティリティ群）に委譲する。

アルゴリズムは「面塗布（外周トレース＋牛耕式ジグザグ）→ 線塗布（最長軸中心線）
→ 点塗布（代表点1点）」のフォールバック階層からなる。面塗布は
``polygon.buffer(-inset)`` の各連結成分ごとに独立したポリラインを生成し、
戻り値は成分別ポリラインのリスト ``list[list[Point2d]]`` となる。
"""

from __future__ import annotations

from math import isclose
from typing import Literal, assert_never

import attrs
from shapely import MultiPolygon, Polygon
from shapely.geometry import GeometryCollection, LineString, MultiLineString
from shapely.geometry.base import BaseGeometry

from pcbasm.config import (
    DISPENSE_MODES,
    LINE_DIRECTIONS,
    DispenseMode,
    LineDirection,
)
from pcbasm.geometry import Point2d
from pcbasm.pasting.settings import ResolvedPaste

AppliedDispenseMode = Literal["dot", "line", "area"]


@attrs.frozen
class PasteFillPlan:
    """塗布パスと、fallback / auto 解決後に実際に使う方式."""

    dispense_mode: AppliedDispenseMode
    paths: list[list[Point2d]]


def build_paste_fill_path(
    polygon: Polygon,
    nozzle_diameter: float,
    *,
    dispense_mode: DispenseMode,
    auto_line_aspect_ratio: float,
    auto_area_short_side_factor: float,
    bead_width_factor: float = 1.0,
    overlap: float = 0.0,
    boundary_margin: float = 0.0,
) -> list[list[Point2d]]:
    """ペーストフィルパスを成分別ポリラインのリストとして生成する.

    :func:`build_paste_fill_plan` の ``paths`` だけを返す薄い互換 API。
    実際に使われた方式も必要な場合は ``build_paste_fill_plan`` を使う。
    """
    return build_paste_fill_plan(
        polygon,
        nozzle_diameter,
        dispense_mode=dispense_mode,
        auto_line_aspect_ratio=auto_line_aspect_ratio,
        auto_area_short_side_factor=auto_area_short_side_factor,
        bead_width_factor=bead_width_factor,
        overlap=overlap,
        boundary_margin=boundary_margin,
    ).paths


def build_paste_fill_plan(
    polygon: Polygon,
    nozzle_diameter: float,
    *,
    dispense_mode: DispenseMode,
    auto_line_aspect_ratio: float,
    auto_area_short_side_factor: float,
    bead_width_factor: float = 1.0,
    overlap: float = 0.0,
    boundary_margin: float = 0.0,
) -> PasteFillPlan:
    """ペーストフィルパスを生成し、実際の塗布方式も返す.

    ノズル径と塗布パラメータからビード幅・線間隔・インセットを決定し、
    指定された方式に従って塗布経路を構築する。

    数式::

        w (bead_width) = nozzle_diameter * bead_width_factor
        line_spacing   = w * (1 - overlap)
        inset          = boundary_margin + w / 2     # 面塗布領域 = buffer(-inset)
        end_inset      = boundary_margin + w / 2     # 線塗布の両端内側補正

    方式::

        auto: 最小回転 bounding box の短辺が nozzle_diameter *
              auto_area_short_side_factor を超えれば area、そうでなく
              long / short が auto_line_aspect_ratio を超えれば line、
              それ以外は dot
        dot : 点塗布（代表点1点）
        line: 線塗布。成立しなければ dot
        area: 面塗布。成立しなければ line、さらに無理なら dot

    Args:
        polygon: 塗布対象のポリゴン（mm単位）
        nozzle_diameter: ノズル内径 [mm]
        dispense_mode: 塗布方式
        auto_line_aspect_ratio: Auto 時に線塗布へ切り替える縦横比（>1.0）
        auto_area_short_side_factor: Auto 時に面塗布へ切り替える短辺のノズル径倍率（>0）
        bead_width_factor: ビード幅係数（w = nozzle_diameter * bead_width_factor）
        overlap: ジグザグ行間オーバーラップ [0, 1)
        boundary_margin: 外周マージン [mm]

    Returns:
        実塗布方式と成分別ポリライン。各内側ポリラインは1点以上の ``Point2d``。
        入力が空／不正なポリゴンの場合のみ ``paths=[]``。

    Raises:
        ValueError: ``nozzle_diameter`` が0以下、``overlap`` が [0,1) 外、
            ``boundary_margin`` が負、``bead_width_factor`` が0以下、
            ``auto_line_aspect_ratio`` が1.0以下、``auto_area_short_side_factor``
            が0以下、未知の ``dispense_mode`` の場合
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
    if auto_line_aspect_ratio <= 1.0:
        raise ValueError(
            "auto_line_aspect_ratioは1.0より大きい必要があります: "
            f"{auto_line_aspect_ratio}"
        )
    if auto_area_short_side_factor <= 0:
        raise ValueError(
            "auto_area_short_side_factorは正の値である必要があります: "
            f"{auto_area_short_side_factor}"
        )
    if dispense_mode not in DISPENSE_MODES:
        raise ValueError(f"未知の塗布方式です: {dispense_mode}")

    if polygon.is_empty or not polygon.is_valid:
        return PasteFillPlan(dispense_mode="dot", paths=[])

    w = nozzle_diameter * bead_width_factor
    line_spacing = w * (1.0 - overlap)
    inset = boundary_margin + w / 2.0
    end_inset = boundary_margin + w / 2.0
    mode = _resolve_auto_mode(
        polygon,
        dispense_mode,
        nozzle_diameter,
        auto_line_aspect_ratio=auto_line_aspect_ratio,
        auto_area_short_side_factor=auto_area_short_side_factor,
    )

    match mode:
        case "dot":
            return PasteFillPlan(dispense_mode="dot", paths=[_dot_fill(polygon)])
        case "line":
            if line := _line_fill(polygon, end_inset):
                return PasteFillPlan(dispense_mode="line", paths=[line])
            return PasteFillPlan(dispense_mode="dot", paths=[_dot_fill(polygon)])
        case "area":
            if paths := _area_fill(polygon, line_spacing, inset):
                return PasteFillPlan(dispense_mode="area", paths=paths)
            if line := _line_fill(polygon, end_inset):
                return PasteFillPlan(dispense_mode="line", paths=[line])
            return PasteFillPlan(dispense_mode="dot", paths=[_dot_fill(polygon)])
        case _:
            assert_never(mode)


def build_pad_fill_plan_for(
    polygon: Polygon,
    *,
    nozzle_diameter: float,
    auto_line_aspect_ratio: float,
    auto_area_short_side_factor: float,
    paste: ResolvedPaste,
    line_reference: Point2d | None = None,
) -> PasteFillPlan:
    """解決済み塗布設定から pad 1 枚分の塗布計画を組み立てる.

    :class:`ResolvedPaste` の per-pad 項目（dispense_mode / line_direction /
    bead_width_factor / overlap / boundary_margin）とマシン設定由来の
    ヒューリスティクス 3 値を :func:`build_paste_fill_plan` の引数へ束ねる対応の
    単一ソース。プレビュー（webui router）と実行（``PasteApplicator._fill``）が
    同一の対応で計画を生成し、両者の乖離を構造的に防ぐ。

    Args:
        polygon: 塗布対象のポリゴン（mm単位）
        nozzle_diameter: ノズル内径 [mm]（``PasteDispenser.nozzle_diameter``）
        auto_line_aspect_ratio: Auto 時に線塗布へ切り替える縦横比
        auto_area_short_side_factor: Auto 時に面塗布へ切り替える短辺のノズル径倍率
        paste: この pad の解決済み塗布設定
        line_reference: outward / inward の基準にする部品位置（board 座標）

    Returns:
        実塗布方式と成分別ポリライン（:class:`PasteFillPlan`）

    Raises:
        ValueError: :func:`build_paste_fill_plan` の検証に通らない場合
    """
    if paste.line_direction not in LINE_DIRECTIONS:
        raise ValueError(f"未知の線走行方向です: {paste.line_direction}")
    if paste.line_direction != "unconstrained" and line_reference is None:
        raise ValueError("線走行方向の指定には部品位置が必要です")

    plan = build_paste_fill_plan(
        polygon,
        nozzle_diameter,
        dispense_mode=paste.dispense_mode,
        auto_line_aspect_ratio=auto_line_aspect_ratio,
        auto_area_short_side_factor=auto_area_short_side_factor,
        bead_width_factor=paste.bead_width_factor,
        overlap=paste.overlap,
        boundary_margin=paste.boundary_margin,
    )
    if plan.dispense_mode != "line" or paste.line_direction == "unconstrained":
        return plan

    assert line_reference is not None
    return attrs.evolve(
        plan,
        paths=[
            _orient_line_path(path, paste.line_direction, line_reference)
            for path in plan.paths
        ],
    )


def _orient_line_path(
    path: list[Point2d], direction: LineDirection, reference: Point2d
) -> list[Point2d]:
    """線パスを基準点から外向き、または基準点へ内向きになるよう整列する."""
    if len(path) < 2 or direction == "unconstrained":
        return path

    start_distance = (path[0] - reference).norm
    end_distance = (path[-1] - reference).norm
    if isclose(start_distance, end_distance, rel_tol=0.0, abs_tol=1e-9):
        return path

    starts_near_reference = start_distance < end_distance
    match direction:
        case "outward":
            reverse = not starts_near_reference
        case "inward":
            reverse = starts_near_reference
        case _:
            assert_never(direction)
    return list(reversed(path)) if reverse else path


def _resolve_auto_mode(
    polygon: Polygon,
    dispense_mode: DispenseMode,
    nozzle_diameter: float,
    *,
    auto_line_aspect_ratio: float,
    auto_area_short_side_factor: float,
) -> AppliedDispenseMode:
    match dispense_mode:
        case "auto":
            long, short = _minimum_rotated_dimensions(polygon)
            if short <= 0:
                return "dot"
            if short > nozzle_diameter * auto_area_short_side_factor:
                return "area"
            return "line" if long / short > auto_line_aspect_ratio else "dot"
        case "dot" | "line" | "area":
            return dispense_mode
        case _:
            assert_never(dispense_mode)


def _minimum_rotated_dimensions(polygon: Polygon) -> tuple[float, float]:
    """最小回転外接矩形の (長辺, 短辺) [mm] を返す。退化時は ``(0.0, 0.0)``."""
    mrr = polygon.minimum_rotated_rectangle
    if not isinstance(mrr, Polygon):
        return (0.0, 0.0)
    coords = list(mrr.exterior.coords)
    if len(coords) < 5:
        return (0.0, 0.0)
    edge_a = Point2d(coords[1][0], coords[1][1]) - Point2d(coords[0][0], coords[0][1])
    edge_b = Point2d(coords[2][0], coords[2][1]) - Point2d(coords[1][0], coords[1][1])
    long = max(edge_a.norm, edge_b.norm)
    short = min(edge_a.norm, edge_b.norm)
    return (long, short)


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
