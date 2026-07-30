"""銅箔照合の関心領域を幾何計算だけで選ぶ（HAL 非依存・撮像不要）.

領域は pixel 空間に張った一辺 ``region_size_px`` のタイル格子で、重なりが無い。
格子の位相は塗布対象 pad 中心の重心に合わせるので、タイル位置はタイル数に
依存せず、board 変換の回転で格子が跳ばない。

採否は 3 条件だけ:

1. ROI 全体が ``safe_area``（基板外形を外周マージンだけ内側へ縮めた領域）に収まる
2. 区内に塗布対象 pad の中心が 1 つ以上ある
3. 予測 sharpness が ``min_sharpness`` 以上

3 の予測 sharpness は、想定エッジを約 1px 間隔の点に落とし、ROI 内に入った点の
単位法線 ``n`` から積んだ拘束行列 ``A = Σ n nᵀ`` の ``λ_min``（chamfer コストの
2 次近似のヘッセそのもの）から出す。一方向のエッジしか無い領域は ``λ_min ≈ 0``
になり、移動する前に落ちる。
"""

import math
from collections.abc import Sequence
from typing import NamedTuple

import attrs
import numpy as np
from shapely import Polygon

from pcbasm.geometry import Point2d, Transform, sort_by_nearest
from pcbasm.posctrl.copper import CopperProjector, PixelRect, centered_roi
from pcbasm.vision import ImageArray

# 線分とみなす最小長 [px]。これ未満は重複頂点として捨てる
_MIN_SEGMENT_PX = 1e-9


def _sharpness(constraint: float, edge_point_count: int) -> float:
    """拘束行列のλ_minを、実測sharpnessと同じ尺度へ正規化する."""
    return math.sqrt(constraint / edge_point_count)


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
        return _sharpness(self.constraint, self.edge_point_count)


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


def plan_alignment_regions(
    projector: CopperProjector,
    board_transform: Transform,
    pad_centers: Sequence[Point2d],
    *,
    safe_area: Polygon,
    region_size_px: int,
    min_sharpness: float,
    image_size: tuple[int, int],
    tour_start: Point2d,
) -> list[AlignmentRegion]:
    """塗布対象padを含む区を重なりなしのタイルとして選び、巡回順に並べて返す.

    タイル格子は pixel 空間で一辺 ``region_size_px``、位相は pad 中心の重心に
    合わせる（タイル位置がタイル数に依存しないので、格子の丸めで配置が跳ばない）。

    ROI ごと ``safe_area`` の内側に入れるのは、やすり掛けで削れた外周の銅箔を
    避けるためだけでなく、基板外形そのものの強いエッジを視野に入れないため。
    外形線は ``CopperProjector`` が描く想定エッジに一切含まれないので、視野に
    入ると片方向 chamfer では一切ペナルティを受けない偽エッジとして働く。

    Args:
        projector: 設計銅箔の投影器（ポリゴンとアフィンの供給元）
        board_transform: board座標→機械座標の変換（anchorの算出に使う）
        pad_centers: 塗布対象padの中心（board座標、mm）。空なら領域は0個
        safe_area: 照合を許す領域（board座標、mm）。基板外形を外周マージンだけ
            内側へ縮めたもの。使うのはbboxと包含判定だけなので、縮めた結果が
            分裂して MultiPolygon になっていても同じに扱える。空なら領域は0個
        region_size_px: 領域の一辺 [px]
        min_sharpness: 予測sharpnessの下限（これ未満の区は移動前に落とす）
        image_size: カメラ画像サイズ (width, height)
        tour_start: 巡回の起点（機械座標、mm）

    Returns:
        0個以上のAlignmentRegion（巡回順、indexは0始まりで振り直し。上限なし）。
        不足しても例外は投げない（min_regionsの判定は呼び出し側の責務）

    Raises:
        ValueError: region_size_px < 1 / region_size_px > min(image_size)
    """
    if region_size_px < 1:
        raise ValueError(f"region_size_pxは1以上である必要があります: {region_size_px}")
    if region_size_px > min(image_size):
        raise ValueError(
            f"region_size_px {region_size_px} が画像サイズ {image_size} を超えています"
        )
    if not pad_centers or safe_area.is_empty:
        return []

    # 参照アンカーのフレームで採点する。pixel_of は board 点について線形で、
    # stage 位置は平行移動しか動かさないので、board 点 b の pixel 位置を
    # このフレームで出せば、実際に anchor = T_b(b) へ移動したとき b は画像中心へ来る。
    minx, miny, maxx, maxy = safe_area.bounds
    bbox_center = Point2d(x=(minx + maxx) / 2, y=(miny + maxy) / 2)
    matrix, shift = projector.board_to_pixel_affine(board_transform.apply(bbox_center))
    inverse = np.linalg.inv(matrix)

    points, normals = _projected_edge_points(projector, matrix, shift)
    if len(points) == 0:
        return []
    half = region_size_px / 2
    # ROIの4隅は区の中心からpixel空間で ±half。board座標へ戻した相対位置は
    # 区に依らないので1回だけ求める（回転があるので軸平行にはならない）
    corner_offsets = (
        np.array([[-half, -half], [half, -half], [half, half], [-half, half]])
        @ inverse.T
    )

    pad_px = np.array([[p.x, p.y] for p in pad_centers]) @ matrix.T + shift
    # 格子の位相はpad重心。中心がpadから half 以内にない区は
    # pad包含条件で必ず落ちるので、走査範囲はこれで十分
    base = pad_px.mean(axis=0)
    k_lo = np.ceil((pad_px.min(axis=0) - half - base) / region_size_px).astype(np.int64)
    k_hi = np.floor((pad_px.max(axis=0) + half - base) / region_size_px).astype(
        np.int64
    )

    candidates: list[_Candidate] = []
    for i in range(int(k_lo[0]), int(k_hi[0]) + 1):
        for j in range(int(k_lo[1]), int(k_hi[1]) + 1):
            center = base + np.array([i, j]) * region_size_px
            board_xy = (center - shift) @ inverse.T
            if not Polygon(corner_offsets + board_xy).within(safe_area):
                continue
            if not np.any(np.all(np.abs(pad_px - center) <= half, axis=1)):
                continue
            inside = np.all(np.abs(points - center) <= half, axis=1)
            edge_point_count = int(inside.sum())
            if edge_point_count == 0:
                continue
            roi_normals = normals[inside]
            # λ_min はGram行列の固有値なので数学的に非負。丸めで出る微小な
            # 負値はここで落として predicted_sharpness を常に定義域に保つ
            constraint = max(
                float(np.linalg.eigvalsh(roi_normals.T @ roi_normals)[0]), 0.0
            )
            if _sharpness(constraint, edge_point_count) < min_sharpness:
                continue
            candidates.append(
                _Candidate(
                    constraint,
                    edge_point_count,
                    Point2d(x=float(board_xy[0]), y=float(board_xy[1])),
                )
            )

    roi = centered_roi(image_size, region_size_px)
    ordered = sort_by_nearest(
        [(board_transform.apply(c.board_xy), c) for c in candidates],
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
