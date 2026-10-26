"""ペースト塗布用フィルパス生成.

公開 API は :meth:`FillPlan.build` と、解決済み塗布設定から引数を束ねてノズル高さを付ける
:meth:`FillPlan.for_pad`、点・線・面の塗布高さ :func:`paste_height_mm`、経路 1 本あたりの塗布量 :func:`amount_per_path_ul`。ノズル径と塗布パラメータからマシン固有の
ヒューリスティクス（線間隔・インセット・フォールバック判定）を決定し、同モジュール内の private ヘルパー（``_area_fill`` /
``_outline_and_zigzag`` / ``_generate_linear_path`` / ``_dot_fill``）に委譲する。汎用の
計算幾何（外接矩形・オフセット成分・線分クリップ等）は :mod:`pcbasm.geometry` にある。

:meth:`FillPlan.mode_for_pad` は経路を作らずに実際の方式だけを返す（pad 編集表の表示用）。

フォールバック判定（``_applied_mode``）は ``build`` と共有する。

アルゴリズムは「面塗布（外周トレース＋牛耕式ジグザグ）→ 線塗布（最長軸中心線）
→ 点塗布（代表点 1 点）」のフォールバック階層からなる。微小線は線塗布と同じ中心線を
ノズル外径基準の inset で引き、成立しなければ点塗布に落ちる。面塗布は
``polygon.buffer(-inset)`` の各連結成分ごとに独立したポリラインを生成し、
戻り値は成分別ポリラインの tuple となる。

塗布 Z（基板表面からのノズル高さ）はこのモジュールだけが決める。
機械座標の絶対 Z は、呼び出し側が渡す変換（高さ面や ``Shift(z=表面 Z)``）が足す。
"""

from __future__ import annotations

from collections.abc import Sequence
from math import isclose
from typing import Literal, assert_never

import attrs
from shapely import Polygon
from shapely.geometry import LineString

from pcbasm.config.solder import SolderConfig
from pcbasm.geometry import (
    Point2d,
    Point3d,
    clip_segment,
    exterior_points,
    offset_components,
    oriented_bbox,
)
from pcbasm.pasting.params import DispenseMode, PasteHeight, PasteParams

AppliedDispenseMode = Literal["dot", "line", "area", "micro_line"]

# 微小線判定（短辺 <= ノズル内径）の比較許容誤差 [mm]。回転した pad では外接矩形の短辺に
# 浮動小数誤差が乗り、同じ footprint が向きで micro_line と line に分かれるのを防ぐ
_MICRO_LINE_SHORT_SIDE_TOLERANCE = 1e-9


@attrs.frozen
class FillPlan[P: (Point2d, Point3d)]:
    """塗布パスと、fallback / auto 解決後に実際に使う方式.

    :meth:`build` は平面の経路（``Point2d``）、:meth:`for_pad` は各点の z に基板表面からのノズル高さ [mm] を付けた経路（``Point3d``）を返す。

    Attributes:
        dispense_mode: 実際に使う方式
        paths: 成分別ポリライン。各ポリラインは 1 点以上。入力が空／不正なポリゴンのときだけ空
    """

    dispense_mode: AppliedDispenseMode
    paths: tuple[tuple[P, ...], ...]

    @classmethod
    def build(
        cls,
        polygon: Polygon,
        nozzle_diameter: float,
        *,
        dispense_mode: DispenseMode,
        auto_line_aspect_ratio: float,
        auto_area_short_side_factor: float,
        bead_width_factor: float = 1.0,
        overlap: float = 0.0,
        boundary_margin: float = 0.0,
        nozzle_outer_diameter: float,
    ) -> FillPlan[Point2d]:
        """ペーストフィルパスを生成し、実際の塗布方式も返す.

        ノズル径と塗布パラメータからビード幅・線間隔・インセットを決定し、
        指定された方式に従って塗布経路を構築する。引数は検証済みの値を受け取る
        （``nozzle_diameter > 0``、``0 <= overlap < 1``、``boundary_margin`` は有限値、
        ``bead_width_factor > 0``、``auto_line_aspect_ratio > 1``、
        ``auto_area_short_side_factor > 0``、``nozzle_outer_diameter >= nozzle_diameter``。
        検証は ``SolderConfig`` と ``PasteParams`` の読込、
        :func:`pcbasm.pasting.params.validate_param_values` が担う）。

        数式::

            w (bead_width) = nozzle_diameter * bead_width_factor
            line_spacing   = w * (1 - overlap)
            inset          = boundary_margin + w / 2     # 面塗布領域 = buffer(-inset)、線塗布の両端内側補正
            micro_inset    = boundary_margin + nozzle_outer_diameter / 2  # 微小線の両端内側補正

        ``boundary_margin < 0`` はビード外縁を pad 外形から ``-boundary_margin`` だけはみ出させる。
        inset が負になると面塗布は pad を外側へ広げた領域を塗り、線塗布は両端を pad の外へ延ばす。
        微小線も ``micro_inset`` が負なら両端を外へ延ばす。

        方式::

            auto: 最小回転 bounding box の短辺が nozzle_diameter *
                  auto_area_short_side_factor を超えれば area、そうでなく
                  long / short が auto_line_aspect_ratio を超えれば line
                  （短辺が nozzle_diameter 以下なら micro_line）、
                  それ以外は dot
            dot : 点塗布（代表点 1 点）
            line: 線塗布。成立しなければ dot
            area: 面塗布。成立しなければ line、さらに無理なら dot
            micro_line: 微小線。成立しなければ dot
        """
        if polygon.is_empty or not polygon.is_valid:
            return FillPlan(dispense_mode="dot", paths=())

        w = nozzle_diameter * bead_width_factor
        line_spacing = w * (1.0 - overlap)
        inset = _inset(nozzle_diameter, bead_width_factor, boundary_margin)
        micro_inset = _micro_inset(nozzle_outer_diameter, boundary_margin)
        mode = _applied_mode(
            polygon,
            _resolve_auto_mode(
                polygon,
                dispense_mode,
                nozzle_diameter,
                auto_line_aspect_ratio=auto_line_aspect_ratio,
                auto_area_short_side_factor=auto_area_short_side_factor,
            ),
            inset,
            micro_inset,
        )

        match mode:
            case "dot":
                return FillPlan(dispense_mode="dot", paths=(tuple(_dot_fill(polygon)),))
            case "line":
                return FillPlan(
                    dispense_mode="line",
                    paths=(tuple(_generate_linear_path(polygon, inset)),),
                )
            case "micro_line":
                return FillPlan(
                    dispense_mode="micro_line",
                    paths=(tuple(_generate_linear_path(polygon, micro_inset)),),
                )
            case "area":
                paths = _area_fill(polygon, line_spacing, inset)
                return FillPlan(
                    dispense_mode="area", paths=tuple(tuple(path) for path in paths)
                )
            case _:
                assert_never(mode)

    @classmethod
    def for_pad(
        cls,
        polygon: Polygon,
        *,
        config: SolderConfig,
        params: PasteParams,
        line_reference: Point2d | None = None,
    ) -> FillPlan[Point3d]:
        """解決済み塗布設定から pad 1 枚分の塗布計画を組み立てる.

        :class:`PasteParams` の per-pad 項目（dispense_mode / line_direction /
        bead_width_factor / overlap / boundary_margin）とマシン設定由来のヒューリスティクス
        （nozzle_diameter / auto_*）を :meth:`build` の引数へ束ねる対応の単一ソース。
        プレビュー（webui router）と実行（``PasteApplicator``）が同一の対応で計画を生成し、
        両者の乖離を構造的に防ぐ。

        ``line_direction`` は実際の方式が line / micro_line になったときだけ使う（area / dot では無視する）。
        ``line_direction`` が outward / inward でも ``line_reference``（部品位置）が無い pad
        は向きを揃えられないので unconstrained として扱う。

        各点の z は基板表面からのノズル高さ [mm]。
        点・線・面は :func:`paste_height_mm`、微小線は向きを揃えた後の始点に ``micro_line_z_start``、終点に ``micro_line_z_end`` を付ける（``auto`` は塗布量から求める）。

        Args:
            polygon: pad のポリゴン（board 座標 [mm]）
            config: ``MachineConfig.solder``（ノズル内径・外径と auto 判定の閾値を使う）
            params: この pad の解決済み塗布パラメータ
            line_reference: 部品位置（board 座標）。outward は部品位置から遠ざかる向き、inward は近づく向きに線を引く
        """
        plan = cls.build(
            polygon,
            config.nozzle.diameter,
            dispense_mode=params.dispense_mode,
            auto_line_aspect_ratio=config.auto.line_aspect_ratio,
            auto_area_short_side_factor=config.auto.area_short_side_factor,
            bead_width_factor=params.bead_width_factor,
            overlap=params.overlap,
            boundary_margin=params.boundary_margin,
            nozzle_outer_diameter=config.nozzle.effective_outer_diameter,
        )
        if (
            plan.dispense_mode in ("line", "micro_line")
            and params.line_direction != "unconstrained"
            and line_reference is not None
        ):
            plan = attrs.evolve(
                plan,
                paths=tuple(
                    tuple(
                        _orient_line_path(
                            list(path), params.line_direction, line_reference
                        )
                    )
                    for path in plan.paths
                ),
            )
        if plan.dispense_mode != "micro_line":
            z = paste_height_mm(params)
            return FillPlan(
                plan.dispense_mode,
                tuple(tuple(point.to3d(z) for point in path) for path in plan.paths),
            )
        ((start, end),) = plan.paths
        auto = _micro_line_auto_height(
            polygon,
            (start, end),
            amount_ul=amount_per_path_ul(polygon, params, len(plan.paths)),
            nozzle_outer_diameter=config.nozzle.effective_outer_diameter,
        )
        return FillPlan(
            "micro_line",
            (
                (
                    start.to3d(_or_auto(params.micro_line_z_start, auto)),
                    end.to3d(_or_auto(params.micro_line_z_end, auto)),
                ),
            ),
        )

    @staticmethod
    def mode_for_pad(
        polygon: Polygon, *, config: SolderConfig, params: PasteParams
    ) -> AppliedDispenseMode:
        """:meth:`for_pad` と同じ判定で、経路を作らずに実際の方式だけを返す.

        面塗布のジグザグは pad の面積に比例して重い。

        pad 編集表は GET のたびに全 pad の方式を出すので、成立判定だけで済ませる。
        """
        if polygon.is_empty or not polygon.is_valid:
            return "dot"
        return _applied_mode(
            polygon,
            _resolve_auto_mode(
                polygon,
                params.dispense_mode,
                config.nozzle.diameter,
                auto_line_aspect_ratio=config.auto.line_aspect_ratio,
                auto_area_short_side_factor=config.auto.area_short_side_factor,
            ),
            _inset(
                config.nozzle.diameter, params.bead_width_factor, params.boundary_margin
            ),
            _micro_inset(
                config.nozzle.effective_outer_diameter, params.boundary_margin
            ),
        )


def _inset(
    nozzle_diameter: float, bead_width_factor: float, boundary_margin: float
) -> float:
    """面塗布領域の内側オフセットと線塗布の両端補正（``boundary_margin + w / 2``）."""
    return boundary_margin + nozzle_diameter * bead_width_factor / 2.0


def _micro_inset(nozzle_outer_diameter: float, boundary_margin: float) -> float:
    """微小線の両端補正（``boundary_margin + 外径 / 2``）."""
    return boundary_margin + nozzle_outer_diameter / 2.0


def _applied_mode(
    polygon: Polygon, mode: AppliedDispenseMode, inset: float, micro_inset: float
) -> AppliedDispenseMode:
    """成立しない方式を area → line → dot の順に落とし、実際の方式を返す.

    微小線は ``micro_inset`` で線が引けなければ dot に落とす。

    面塗布は ``buffer(-inset)`` の成分が 1 つでもあれば外周トレースが必ず作れるので、
    ジグザグを生成せずに成分の有無だけで判定する。
    """
    match mode:
        case "dot":
            return "dot"
        case "line":
            return "line" if _generate_linear_path(polygon, inset) else "dot"
        case "micro_line":
            return (
                "micro_line" if _generate_linear_path(polygon, micro_inset) else "dot"
            )
        case "area":
            if offset_components(polygon, inset):
                return "area"
            return _applied_mode(polygon, "line", inset, micro_inset)
        case _:
            assert_never(mode)


def paste_height_mm(params: PasteParams) -> float:
    """点・線・面塗布で基板表面から取るノズル高さ [mm]（``auto`` は ``ul_per_mm2`` を膜厚として解決）.

    ``ul_per_mm2`` は μL/mm² = mm³/mm² = mm なので、目標膜厚をそのまま基板表面からのクリアランスに使う。
    """
    return _or_auto(params.paste_height, params.ul_per_mm2)


def amount_per_path_ul(polygon: Polygon, params: PasteParams, path_count: int) -> float:
    """経路 1 本あたりの塗布量 [μL]（pad の全量 ``面積 × ul_per_mm2`` を経路数で均等に分ける）.

    実行が送る量と、微小線の ``auto`` 高さが使い切る量の単一ソース。
    """
    return polygon.area * params.ul_per_mm2 / path_count


def _or_auto(height: PasteHeight, auto: float) -> float:
    return auto if height == "auto" else height


def _micro_line_auto_height(
    polygon: Polygon,
    line: Sequence[Point2d],
    *,
    amount_ul: float,
    nozzle_outer_diameter: float,
) -> float:
    """微小線の高さ（基板表面からの隙間 [mm]）を塗布量から求める.

    ノズル端面がスキージとして厚さ g・幅 w の膜を残すとみなし、端面が掃く範囲で塗布量 V を
    使い切る隙間 g = V / (w × (L + D)) を返す。L は線長、D はノズル外径で、始点と終点で
    端面の下にある分（D）も膜になる。w は pad の短辺（最小回転外接矩形）とノズル外径の
    小さい方で、膜は端面より広がらず pad からもはみ出さないとみなす。

    ``line`` は :class:`FillPlan` が作った微小線（長さ > 0）であること（呼び出し側が保証する）。
    """
    box = oriented_bbox(polygon)
    assert box is not None  # 微小線を引けた pad は外接矩形を持つ
    width = min(box.short_length, nozzle_outer_diameter)
    swept = _polyline_length(line) + nozzle_outer_diameter
    return amount_ul / (width * swept)


def _orient_line_path(
    path: list[Point2d], direction: Literal["outward", "inward"], reference: Point2d
) -> list[Point2d]:
    """2 点の線パスを基準点から外向き、または基準点へ内向きになるよう整列する."""
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
            box = oriented_bbox(polygon)
            if box is None or box.short_length <= 0:
                return "dot"
            long, short = box.long_length, box.short_length
            if short > nozzle_diameter * auto_area_short_side_factor:
                return "area"
            if long / short <= auto_line_aspect_ratio:
                return "dot"
            if short <= nozzle_diameter + _MICRO_LINE_SHORT_SIDE_TOLERANCE:
                return "micro_line"
            return "line"
        case "dot" | "line" | "area" | "micro_line":
            return dispense_mode
        case _:
            assert_never(dispense_mode)


def _area_fill(
    polygon: Polygon,
    line_spacing: float,
    inset: float,
) -> list[list[Point2d]]:
    """面塗布パスを成分別ポリラインのリストとして生成する.

    ``polygon.buffer(-inset)``（inset が負なら外側へ広げる）の各連結成分（``offset_components``）に
    ``_outline_and_zigzag`` を適用し、空でないポリラインを集めて返す。
    成分が無い（buffer 後が空）場合は ``[]``。
    """
    components = offset_components(polygon, inset)
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
    """1 連結成分の外周トレース＋内部ジグザグを 1 ポリラインに結合して返す.

    最小実装の方針（牛耕式）:

    1. 外周: ``exterior_points(component)`` で外環をトレースする。
    2. ジグザグ: ``component.minimum_rotated_rectangle`` から最長軸方向を取り、
       最長軸に垂直なスキャンラインを ``line_spacing`` 間隔で生成する。各
       スキャンライン∩``component`` の区間を最長軸座標でソートし、行ごとに
       方向を交互反転して（牛耕式に）繋ぐ。
    3. 外周 → 内部ジグザグの順で、各遷移を最近傍接続で 1 ポリラインに結合する。

    点が作れなければ ``[]`` を返す。
    """
    outline = exterior_points(component)
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

    # exterior_points は閉環（末尾が始点の重複）なので末尾を除いて巡回頂点とする
    vertices = exterior_points(component)[:-1]
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
    vertices: Sequence[Point2d], i_start: int, i_end: int, *, step: int
) -> list[Point2d]:
    """巡回頂点列を ``i_start`` から ``i_end`` まで ``step``（+1 か -1）方向に辿った点列（両端を含む）."""
    n = len(vertices)
    path: list[Point2d] = []
    i = i_start
    while i != i_end:
        path.append(vertices[i])
        i = (i + step) % n
    path.append(vertices[i_end])
    return path


def _polyline_length(points: Sequence[Point2d]) -> float:
    """ポリラインの総延長を返す（点が 2 未満なら 0）."""
    return sum((points[i + 1] - points[i]).norm for i in range(len(points) - 1))


def _zigzag_rows(component: Polygon, line_spacing: float) -> list[list[Point2d]]:
    """成分内部を最長軸方向の牛耕式ジグザグ行として生成する.

    ``minimum_rotated_rectangle`` の最長辺方向を走査方向（最長軸）とし、それに
    垂直なスキャンラインを ``line_spacing`` 間隔で並べる。各スキャンラインと
    ``component`` の交線区間を最長軸座標でソートし、行ごとに方向を交互反転して
    牛耕式に並べた行のリストを返す。交差区間が無ければ空行は含めない。
    """
    box = oriented_bbox(component)
    if box is None:
        return []

    # 最長辺方向 = 走査方向（最長軸 u）、もう一方 = 行送り方向 v
    corner = box.corner
    long_edge, short_edge = box.long_edge, box.short_edge
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
        intervals = clip_segment(component, base, base + u * long_len)
        if not intervals:
            continue
        # 行ごとに走査向きを交互反転（牛耕式）
        if i % 2 == 1:
            intervals = list(reversed([(b, a) for a, b in intervals]))
        for start, end in intervals:
            rows.append([start, end])
    return rows


def _dot_fill(polygon: Polygon) -> list[Point2d]:
    """点塗布パスを生成する（代表点 1 点・必ず非空）."""
    rep = polygon.representative_point()
    return [Point2d(rep.x, rep.y)]


def _generate_linear_path(
    polygon: Polygon,
    end_inset: float,
) -> list[Point2d]:
    """ポリゴンの最長軸に沿った 2 点パスを生成する.

    ``polygon.minimum_rotated_rectangle`` の長軸の中央線を求め、両端を
    ``end_inset`` だけ内側に補正した 2 点パスを返す（負なら外側へ延ばす）。長軸長が
    ``2 * end_inset`` 以下の場合は ``[]`` を返す。

    Args:
        polygon: 対象ポリゴン（mm 単位）
        end_inset: 両端の内側補正量（mm、負なら外側へ延ばす）

    Returns:
        ``[start, end]`` の 2 点パス。長軸が短すぎる場合は ``[]``。
    """
    box = oriented_bbox(polygon)
    if box is None:
        return []
    start, end = box.center_line()

    direction = end - start
    length = direction.norm
    if length <= 2 * end_inset:
        return []

    unit = direction * (1.0 / length)
    return [start + unit * end_inset, end - unit * end_inset]
