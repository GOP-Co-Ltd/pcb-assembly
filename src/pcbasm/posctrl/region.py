"""銅箔照合の関心領域を幾何計算だけで選ぶ（HAL 非依存・撮像不要）.

想定エッジを約 1px 間隔の点に落とし、ROI 内に入った点の単位法線 ``n`` から
拘束行列 ``A = Σ n nᵀ`` を積む。これは chamfer コストの 2 次近似のヘッセそのもので、
``λ_min(A)`` が「最も弱く拘束されている方向の拘束量」。一方向のエッジしか無い
領域は ``λ_min ≈ 0`` になって自動的に落ちる。

候補にできるのは ROI 全体が ``safe_area``（基板外形を外周マージンだけ内側へ
縮めた領域）に収まる位置だけ。領域選定の定義域と外周除外はこの 1 つの図形で
決まる。
"""

import math
from typing import NamedTuple

import attrs
import numpy as np
from shapely import Polygon

from pcbasm.geometry import Point2d, Transform, sort_by_nearest
from pcbasm.posctrl.copper import CopperProjector, PixelRect, centered_roi
from pcbasm.vision import ImageArray

# 線分とみなす最小長 [px]。これ未満は重複頂点として捨てる
_MIN_SEGMENT_PX = 1e-9

# 候補格子の分割数を決める ceil の相対許容。step が回転由来の丸め誤差で
# 振れても分割数が変わらない大きさ（倍精度の相対誤差より十分大きく、
# 実寸の差より十分小さい）
_GRID_RATIO_TOLERANCE = 1e-9


@attrs.frozen
class AlignmentRegion:
    """銅箔照合の関心領域.

    Attributes:
        index: 巡回順の0始まり通し番号
        anchor: 機械座標 [mm]。ここへ移動して撮像すると領域が画像中心に来る
        roi: 照合ROI（全画面px）。全regionで同一の画像中心固定矩形
        constraint: λ_min(A)。A = Σ n nᵀ（ROI内に入ったエッジ点の単位法線n）
        edge_point_count: ROI内に入った想定エッジ点の数
    """

    index: int
    anchor: Point2d
    roi: PixelRect
    constraint: float
    edge_point_count: int

    @property
    def predicted_sharpness(self) -> float:
        """撮像前に幾何だけで予測したsharpness.

        ``EdgeMatch.sharpness`` と同じ尺度。あちらは chamfer コストのヘッセ
        ``H = 2A`` を template画素数 ``|T|`` で正規化するので、こちらも長さでは
        なく ``|T|`` と同種の量（約1px間隔に置いたエッジ点の数）で正規化する。

        実 PCB では実測より数 % 〜 十数 % 楽観側に出る。斜めエッジが支配的な
        形状ではラスタライズの階段が chamfer 距離を変えるため最大 +22% ずれる。
        """
        return math.sqrt(self.constraint / self.edge_point_count)


class _Candidate(NamedTuple):
    """採点済みの候補領域（board座標）."""

    constraint: float
    edge_point_count: int
    board_xy: Point2d


def _projected_edge_points(
    projector: CopperProjector, matrix: ImageArray, shift: ImageArray
) -> tuple[ImageArray, ImageArray]:
    """全ポリゴンのリングを、pixel空間の約1px間隔の点列へ一括変換する.

    点は候補位置に依存しない（投影フレームは参照アンカーで固定）ので、
    1 回作れば全候補で使い回せる。

    Returns:
        (P, N)。点 (n, 2) と、その点が乗る線分の単位法線 (n, 2)
    """
    starts: list[ImageArray] = []
    ends: list[ImageArray] = []
    for polygon in projector.polygons:
        for ring in (polygon.exterior, *polygon.interiors):
            pixels = np.asarray(ring.coords, dtype=np.float64) @ matrix.T + shift
            if len(pixels) < 2:
                continue
            starts.append(pixels[:-1])
            ends.append(pixels[1:])
    if not starts:
        empty = np.zeros((0, 2))
        return empty, empty

    p0 = np.concatenate(starts)
    delta = np.concatenate(ends) - p0
    lengths = np.hypot(delta[:, 0], delta[:, 1])
    keep = lengths > _MIN_SEGMENT_PX
    p0, delta, lengths = p0[keep], delta[keep], lengths[keep]
    units = delta / lengths[:, None]
    normals = np.stack([-units[:, 1], units[:, 0]], axis=1)

    # 各線分を約1px幅の区間に割り、その中点へ点を置く
    counts = np.maximum(np.round(lengths), 1.0).astype(np.int64)
    index = np.repeat(np.arange(len(counts)), counts)
    origins = np.repeat(np.cumsum(counts) - counts, counts)
    fractions = (np.arange(len(index)) - origins + 0.5) / counts[index]
    return p0[index] + fractions[:, None] * delta[index], normals[index]


def _candidate_grid(safe_area: Polygon, step: float) -> list[Point2d]:
    """safe_areaのbboxにstep間隔の格子を張り、board座標の候補点列を返す.

    分割数は ``ceil(span / step)``。ただし ``span / step`` がちょうど整数になる
    配置では、``step`` に乗った丸め誤差（回転を含む変換から出るので避けられない）
    だけで ceil が 1 段跳び、格子間隔が不連続に変わる。相対許容を引いてその跳びを
    吸収する。

    bboxが1軸で潰れている（hi == lo）場合、その軸の候補はその1点だけになる。
    """
    minx, miny, maxx, maxy = safe_area.bounds
    axes: list[ImageArray] = []
    for lo, hi in ((minx, maxx), (miny, maxy)):
        ratio = (hi - lo) / step
        divisions = math.ceil(ratio - _GRID_RATIO_TOLERANCE * max(ratio, 1.0))
        axes.append(np.linspace(lo, hi, divisions + 1))
    return [Point2d(x=float(x), y=float(y)) for x in axes[0] for y in axes[1]]


def plan_alignment_regions(
    projector: CopperProjector,
    board_transform: Transform,
    *,
    safe_area: Polygon,
    region_size_px: int,
    count: int,
    image_size: tuple[int, int],
    tour_start: Point2d,
) -> list[AlignmentRegion]:
    """拘束の強い関心領域をcount個まで選び、巡回順に並べて返す.

    候補は ``safe_area`` のbboxに張った格子のうち、**ROI 全体が ``safe_area`` に
    収まる**ものだけ。ROI ごと内側に入れるのは、やすり掛けで削れた外周の銅箔を
    避けるためだけでなく、基板外形そのものの強いエッジを視野に入れないため。
    外形線は ``CopperProjector`` が描く想定エッジに一切含まれないので、視野に
    入ると片方向 chamfer では一切ペナルティを受けない偽エッジとして働く。

    Args:
        projector: 設計銅箔の投影器（ポリゴンとアフィンの供給元）
        board_transform: board座標→機械座標の変換（anchorの算出に使う）
        safe_area: 照合を許す領域（board座標、mm）。基板外形を外周マージンだけ
            内側へ縮めたもの。使うのはbboxと包含判定だけなので、縮めた結果が
            分裂して MultiPolygon になっていても同じに扱える。空なら領域は0個
        region_size_px: 領域の一辺 [px]
        count: 選ぶ領域数の上限
        image_size: カメラ画像サイズ (width, height)
        tour_start: 巡回の起点（機械座標、mm）

    Returns:
        0個以上count個以下のAlignmentRegion（巡回順、indexは0始まりで振り直し）。
        safe_areaが空 / ROIが収まる候補が無い / constraint <= 0 の候補しか無い
        場合は空リスト。不足しても例外は投げない（min_regionsの判定は
        呼び出し側の責務）

    Raises:
        ValueError: region_size_px < 1 / count < 1 / region_size_px > min(image_size)
    """
    if region_size_px < 1:
        raise ValueError(f"region_size_pxは1以上である必要があります: {region_size_px}")
    if count < 1:
        raise ValueError(f"countは1以上である必要があります: {count}")
    if region_size_px > min(image_size):
        raise ValueError(
            f"region_size_px {region_size_px} が画像サイズ {image_size} を超えています"
        )
    if safe_area.is_empty:
        return []

    # 参照アンカーのフレームで採点する。pixel_of は board 点について線形で、
    # stage 位置は平行移動しか動かさないので、board 点 b の pixel 位置を
    # このフレームで出せば、実際に anchor = T_b(b) へ移動したとき b は画像中心へ来る。
    minx, miny, maxx, maxy = safe_area.bounds
    bbox_center = Point2d(x=(minx + maxx) / 2, y=(miny + maxy) / 2)
    matrix, shift = projector.board_to_pixel_affine(board_transform.apply(bbox_center))
    pixel_per_board_mm = math.hypot(matrix[0, 0], matrix[1, 0])
    region_mm = region_size_px / pixel_per_board_mm

    points, normals = _projected_edge_points(projector, matrix, shift)
    if len(points) == 0:
        return []
    half = region_size_px / 2
    # ROIの4隅は候補中心からpixel空間で ±half。board座標へ戻した相対位置は
    # 候補に依らないので1回だけ求める（回転があるので軸平行にはならない）
    corner_offsets = (
        np.array([[-half, -half], [half, -half], [half, half], [-half, half]])
        @ np.linalg.inv(matrix).T
    )

    scored: list[_Candidate] = []
    for board_xy in _candidate_grid(safe_area, region_mm / 2):
        origin = np.array([board_xy.x, board_xy.y])
        if not Polygon(corner_offsets + origin).within(safe_area):
            continue
        center = origin @ matrix.T + shift
        inside = (np.abs(points[:, 0] - center[0]) <= half) & (
            np.abs(points[:, 1] - center[1]) <= half
        )
        roi_normals = normals[inside]
        constraint = float(np.linalg.eigvalsh(roi_normals.T @ roi_normals)[0])
        if constraint <= 0.0:
            continue
        scored.append(_Candidate(constraint, int(inside.sum()), board_xy))

    # 拘束の強い順に、既選択から領域サイズ以上離れているものを貪欲に採る
    selected: list[_Candidate] = []
    for candidate in sorted(scored, key=lambda c: c.constraint, reverse=True):
        if len(selected) >= count:
            break
        if all(
            (candidate.board_xy - other.board_xy).norm >= region_mm
            for other in selected
        ):
            selected.append(candidate)

    roi = centered_roi(image_size, region_size_px)
    ordered = sort_by_nearest(
        [(board_transform.apply(c.board_xy), c) for c in selected],
        tour_start.to3d(),
        key=lambda entry: entry[0].to3d(),
    )
    return [
        AlignmentRegion(
            index=index,
            anchor=anchor,
            roi=roi,
            constraint=candidate.constraint,
            edge_point_count=candidate.edge_point_count,
        )
        for index, (anchor, candidate) in enumerate(ordered)
    ]
