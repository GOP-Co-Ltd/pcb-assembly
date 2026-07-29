"""銅箔照合の関心領域を幾何計算だけで選ぶ（HAL 非依存・撮像不要）.

想定エッジの各線分（投影済み、pixel 空間）について ROI 内に入る長さ ``L`` と
単位法線 ``n`` から拘束行列 ``A = Σ L · n nᵀ`` を積む。これは chamfer コストの
2 次近似のヘッセそのもので、``λ_min(A)`` が「最も弱く拘束されている方向の拘束量」。
一方向のエッジしか無い領域は ``λ_min ≈ 0`` になって自動的に落ちる。
"""

import math
from collections.abc import Sequence
from typing import NamedTuple

import attrs
import numpy as np

from pcbasm.geometry import Point2d, Transform, sort_by_nearest
from pcbasm.posctrl.copper import CopperProjector, PixelRect, centered_roi
from pcbasm.vision import ImageArray

# 線分とみなす最小長 [px]。これ未満は重複頂点として捨てる
_MIN_SEGMENT_PX = 1e-9


@attrs.frozen
class AlignmentRegion:
    """銅箔照合の関心領域.

    Attributes:
        index: 巡回順の0始まり通し番号
        anchor: 機械座標 [mm]。ここへ移動して撮像すると領域が画像中心に来る
        roi: 照合ROI（全画面px）。全regionで同一の画像中心固定矩形
        constraint: λ_min(A) [px]。A = Σ L·n nᵀ（ROI内に入る線分長Lと単位法線n）
        edge_length_px: ROI内に入る想定エッジの総長 [px]
    """

    index: int
    anchor: Point2d
    roi: PixelRect
    constraint: float
    edge_length_px: float

    @property
    def predicted_sharpness(self) -> float:
        """撮像前に幾何だけで予測したsharpness.

        ``EdgeMatch.sharpness`` と同じ尺度（chamferコストのヘッセ ``H = 2A``、
        template画素数 ``n ≈ Σ L`` の近似の下で一致する）。照合後の実測値と
        比べれば、想定どおりの銅箔を見ているかを診断できる。
        """
        return math.sqrt(self.constraint / self.edge_length_px)


class _Candidate(NamedTuple):
    """採点済みの候補領域（board座標）."""

    constraint: float
    edge_length_px: float
    board_xy: Point2d


def _projected_segments(
    projector: CopperProjector, matrix: ImageArray, shift: ImageArray
) -> tuple[ImageArray, ImageArray, ImageArray, ImageArray]:
    """全ポリゴンのリングを一括でpixel空間の線分列へ変換する.

    Returns:
        (P0, D, L, N)。始点 (n, 2)、始点→終点ベクトル (n, 2)、長さ (n,)、単位法線 (n, 2)
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
        empty2 = np.zeros((0, 2))
        return empty2, empty2, np.zeros(0), empty2

    p0 = np.concatenate(starts)
    delta = np.concatenate(ends) - p0
    lengths = np.hypot(delta[:, 0], delta[:, 1])
    keep = lengths > _MIN_SEGMENT_PX
    p0, delta, lengths = p0[keep], delta[keep], lengths[keep]
    units = delta / lengths[:, None]
    normals = np.stack([-units[:, 1], units[:, 0]], axis=1)
    return p0, delta, lengths, normals


def _clipped_lengths(
    p0: ImageArray,
    delta: ImageArray,
    lengths: ImageArray,
    center: ImageArray,
    half: float,
) -> ImageArray:
    """軸平行矩形へLiang-Barskyでクリップした各線分の長さを返す（ベクトル化）."""
    t0 = np.zeros(len(lengths))
    t1 = np.ones(len(lengths))
    for axis in (0, 1):
        lo, hi = center[axis] - half, center[axis] + half
        d = delta[:, axis]
        for pcoef, qcoef in ((-d, p0[:, axis] - lo), (d, hi - p0[:, axis])):
            zero = pcoef == 0
            ratio = np.zeros(len(lengths))
            np.divide(qcoef, pcoef, out=ratio, where=~zero)
            t0 = np.where((~zero) & (pcoef < 0), np.maximum(t0, ratio), t0)
            t1 = np.where((~zero) & (pcoef > 0), np.minimum(t1, ratio), t1)
            # 矩形の外で境界に平行な線分は丸ごと棄却する
            t1 = np.where(zero & (qcoef < 0), -1.0, t1)
    return np.maximum(t1 - t0, 0.0) * lengths


def _candidate_grid(pad_centers: Sequence[Point2d], step: float) -> list[Point2d]:
    """pad中心のbboxにstep間隔の格子を張り、board座標の候補点列を返す.

    bboxが1軸で潰れている（hi == lo）場合、その軸の候補はその1点だけになる。
    """
    xs = [p.x for p in pad_centers]
    ys = [p.y for p in pad_centers]
    axes: list[ImageArray] = []
    for lo, hi in ((min(xs), max(xs)), (min(ys), max(ys))):
        axes.append(np.linspace(lo, hi, math.ceil((hi - lo) / step) + 1))
    return [Point2d(x=float(x), y=float(y)) for x in axes[0] for y in axes[1]]


def plan_alignment_regions(
    projector: CopperProjector,
    pad_centers: Sequence[Point2d],
    board_transform: Transform,
    *,
    region_size_px: int,
    count: int,
    image_size: tuple[int, int],
    tour_start: Point2d,
) -> list[AlignmentRegion]:
    """拘束の強い関心領域をcount個まで選び、巡回順に並べて返す.

    Args:
        projector: 設計銅箔の投影器（ポリゴンとアフィンの供給元）
        pad_centers: 候補格子を張る範囲を決めるTOP pad中心（board座標、mm）
        board_transform: board座標→機械座標の変換（anchorの算出に使う）
        region_size_px: 領域の一辺 [px]
        count: 選ぶ領域数の上限
        image_size: カメラ画像サイズ (width, height)
        tour_start: 巡回の起点（機械座標、mm）

    Returns:
        0個以上count個以下のAlignmentRegion（巡回順、indexは0始まりで振り直し）。
        constraint <= 0 の候補は除外するので、銅箔が無ければ空リストを返す。
        不足しても例外は投げない（min_regionsの判定は呼び出し側の責務）

    Raises:
        ValueError: region_size_px < 1 / count < 1 / pad_centersが空 /
            region_size_px > min(image_size)
    """
    if region_size_px < 1:
        raise ValueError(f"region_size_pxは1以上である必要があります: {region_size_px}")
    if count < 1:
        raise ValueError(f"countは1以上である必要があります: {count}")
    if not pad_centers:
        raise ValueError("pad_centersが空です")
    if region_size_px > min(image_size):
        raise ValueError(
            f"region_size_px {region_size_px} が画像サイズ {image_size} を超えています"
        )

    # 参照アンカーのフレームで採点する。pixel_of は board 点について線形で、
    # stage 位置は平行移動しか動かさないので、board 点 b の pixel 位置を
    # このフレームで出せば、実際に anchor = T_b(b) へ移動したとき b は画像中心へ来る。
    bbox_center = Point2d(
        x=(min(p.x for p in pad_centers) + max(p.x for p in pad_centers)) / 2,
        y=(min(p.y for p in pad_centers) + max(p.y for p in pad_centers)) / 2,
    )
    matrix, shift = projector.board_to_pixel_affine(board_transform.apply(bbox_center))
    pixel_per_board_mm = math.hypot(matrix[0, 0], matrix[1, 0])
    region_mm = region_size_px / pixel_per_board_mm

    p0, delta, lengths, normals = _projected_segments(projector, matrix, shift)
    if len(lengths) == 0:
        return []
    half = region_size_px / 2

    scored: list[_Candidate] = []
    for board_xy in _candidate_grid(pad_centers, region_mm / 2):
        center = np.array([board_xy.x, board_xy.y]) @ matrix.T + shift
        clipped = _clipped_lengths(p0, delta, lengths, center, half)
        constraint = float(
            np.linalg.eigvalsh(np.einsum("i,ij,ik->jk", clipped, normals, normals))[0]
        )
        if constraint <= 0.0:
            continue
        scored.append(_Candidate(constraint, float(clipped.sum()), board_xy))

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
            edge_length_px=candidate.edge_length_px,
        )
        for index, (anchor, candidate) in enumerate(ordered)
    ]
