"""カメラキャリブレーション: 多視点コーナーから内部パラメータと pixel/mm を推定する.

チェッカーボードを 1 枚固定し、XY ステージを格子状に自動移動して多視点を稼ぐ。
``cv2.calibrateCamera`` でレンズ歪みまで含めた内部パラメータを求め、歪み補正後の
フレームで測った単一スカラー ``pixel_per_mm`` を FOV 全域で成立させる。

実行時の歪み補正そのものは ``intrinsics.py``（``Undistorter``）にある。
"""

import json
import logging
import math
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Self

import attrs
import cv2
import numpy as np
from cattrs.preconf.json import make_converter

from pcbasm.geometry import Point2d
from pcbasm.vision.image import Image, ImageArray
from pcbasm.vision.intrinsics import CameraIntrinsics, Undistorter

logger = logging.getLogger(__name__)

# ステージスキャンの格子（列数・行数）。ともに奇数なので中心 (0,0) が訪問点に含まれる
SCAN_COLUMNS = 5
SCAN_ROWS = 3
# 校正を成立させる有効視点数の下限
MINIMUM_SCAN_VIEWS = 8
# 残差を集計する半径帯の上限 [px]（帯下限は 1 つ内側の上限、最内は 0）。
# 値は実機のフル解像度 1280x720（半対角 734px）を前提に選んである。最外帯は上限を
# 超えたサンプルもすべて回収し、上限には実際に観測された最大半径を報告する
RESIDUAL_BUCKET_EDGES_PX = (150.0, 300.0, 450.0, 640.0)

# Path の変換をサポートするコンバーター
_converter = make_converter()
_converter.register_unstructure_hook(Path, str)
_converter.register_structure_hook(Path, lambda v, _: Path(v))

# ステージ移動幅を導出するときの片側の安全余裕 [px]
_SAFETY_MARGIN_PX = 24.0

_SUBPIX_CRITERIA = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)

# 合成データ・真値既知で比較して確定したフラグ。主点を固定すると補正マップ精度が
# 4.5 倍悪化し、k3 を自由にすると周辺で過剰適合する
_CALIBRATION_FLAGS = (
    cv2.CALIB_USE_INTRINSIC_GUESS
    | cv2.CALIB_FIX_FOCAL_LENGTH
    | cv2.CALIB_ZERO_TANGENT_DIST
    | cv2.CALIB_FIX_K3
)


def _corner_points(view: "CheckerboardView") -> ImageArray:
    """視点のコーナーを (N,2) float64 で返す."""
    return np.asarray(view.corners, dtype=np.float64).reshape(-1, 2)


def _fit_linear_map(source: ImageArray, target: ImageArray) -> ImageArray:
    """``source`` (N,2) → ``target`` (N,2) の 2x2 線形写像を最小二乗フィットする.

    平行移動は含めない（差分量どうしのフィットに使う）。返り値 ``A`` は
    ``target.T ≈ A @ source.T`` を満たす。
    """
    solution, *_ = np.linalg.lstsq(source, target, rcond=None)
    return np.asarray(solution, dtype=np.float64).T


def _map_scale(matrix: ImageArray) -> float:
    """2x2 写像の等方スケール成分を返す（行列式の平方根、鏡映も許容）."""
    return math.sqrt(abs(float(np.linalg.det(matrix))))


def _map_rotation_deg(matrix: ImageArray) -> float:
    """2x2 写像が X 軸を送る向きを角度 [deg] で返す（カメラ取付角の指標）.

    符号規約: 機械 +X が画像上で向く方向を、画像 x 軸から反時計回り（画素座標系
    なので画面上では時計回りに見える）に測る。写像が鏡映を含む場合（行列式が負＝
    下向きカメラで画像 y が機械 Y と逆向き）その情報はこの角度に現れないので、
    ``OffsetTransformMeasurer`` の 2 点法と相互検証するときは同じ鏡映規約で測った
    値どうしを比べること（規約が違うと符号が反転して見える）。
    """
    return math.degrees(math.atan2(float(matrix[1, 0]), float(matrix[0, 0])))


def _fit_view_scale(object_points: ImageArray, image_points: ImageArray) -> float:
    """盤座標 (N,2) [mm] → 画素座標 (N,2) の相似変換から pixel/mm を返す.

    平行移動は両者の重心を揃えることで吸収する。
    """
    source = object_points - object_points.mean(axis=0)
    target = image_points - image_points.mean(axis=0)
    return _map_scale(_fit_linear_map(source, target))


def _object_points_mm(
    pattern_size: tuple[int, int], square_size_mm: float
) -> ImageArray:
    """内部コーナーの盤座標 (N,2) float64 [mm]。中心原点、cv2 の並び順."""
    columns, rows = pattern_size
    grid = np.mgrid[0:columns, 0:rows].T.reshape(-1, 2).astype(np.float64)
    grid -= np.array([(columns - 1) / 2.0, (rows - 1) / 2.0])
    return grid * square_size_mm


@attrs.frozen(eq=False)
class CheckerboardView:
    """1 視点で検出したチェッカーボードのコーナー（ndarray を持つので永続化しない).

    Attributes:
        stage_position: コマンドしたステージ XY [mm]（絶対）
        corners: (N,1,2) float32、cv2 の並び。フル解像度の画素座標
        pattern_size: 内部コーナー数 (cols, rows)
        image_size: 検出元フレームのサイズ (width, height)。残差の半径帯に使う
    """

    stage_position: Point2d
    corners: ImageArray
    pattern_size: tuple[int, int]
    image_size: tuple[int, int]


def measure_pixel_per_mm(view: CheckerboardView, square_size_mm: float) -> float:
    """1 視点のコーナー配置から pixel/mm を測る.

    盤座標 [mm] → 画素座標の相似変換をフィットし、そのスケール成分を返す。歪みを
    補正していない生コーナーなので FOV 全域で厳密ではなく、``ScanGrid.plan`` へ
    渡す移動幅の導出や操作者へのログ表示に使う概算値。しかも盤の印刷寸法を定規に
    しているので盤の寸法誤差がそのまま乗る。校正後の正確な値は
    ``CalibrationResult.pixel_per_mm``（多視点・補正後・ステージ変位を定規）を使う。

    Args:
        view: 計測する視点
        square_size_mm: チェッカーボードの 1 マスのサイズ [mm]
    """
    return _fit_view_scale(
        _object_points_mm(view.pattern_size, square_size_mm), _corner_points(view)
    )


@attrs.frozen
class ScanGrid:
    """ステージを蛇行させて多視点を稼ぐための相対 XY 格子.

    Attributes:
        positions: 開始位置からの相対 XY [mm]。先頭は必ず中心視点 (0,0) で、
            以降は格子の隅から蛇行（serpentine）順に巡る。中心を先頭に置くのは
            残差の基準視点にするため（基準視点のコーナーが画像中央付近に集まって
            いないと、最内の半径帯にサンプルが入らず ``usable_crop_side_px`` が
            測れない）
        columns: 列数
        rows: 行数
        span_mm: 移動幅 (X, Y) [mm]
        max_corner_radius_px: 予測されるコーナー被覆半径 [px]（ログ用）
    """

    positions: tuple[Point2d, ...]
    columns: int
    rows: int
    span_mm: tuple[float, float]
    max_corner_radius_px: float

    @classmethod
    def plan(
        cls,
        *,
        image_size: tuple[int, int],
        corners: ImageArray,
        pattern_size: tuple[int, int],
        pixel_per_mm: float,
        columns: int = SCAN_COLUMNS,
        rows: int = SCAN_ROWS,
    ) -> Self | None:
        """計画用ショットの検出結果から格子を導出する.

        コーナー外接矩形に上下左右 1 マス分の静穏枠を足して盤全体の寸法を推定し、
        フレームに収まったまま動かせる幅を移動幅とする（``findChessboardCorners``
        は全パターンが視野内にあることを要求する）。

        Args:
            image_size: フレームサイズ (width, height)
            corners: 計画用ショットのコーナー (N,1,2) または (N,2)
            pattern_size: 内部コーナー数 (cols, rows)
            pixel_per_mm: 計画用ショットで測った pixel/mm
            columns: 格子の列数
            rows: 格子の行数

        Returns:
            格子。導出した移動幅がフレームの 1/4 未満なら None（盤が視野に対して大きい）
        """
        points = np.asarray(corners, dtype=np.float64).reshape(-1, 2)
        pattern_columns, pattern_rows = pattern_size
        extent_x = float(points[:, 0].max() - points[:, 0].min())
        extent_y = float(points[:, 1].max() - points[:, 1].min())
        board_x = extent_x * (1.0 + 2.0 / (pattern_columns - 1))
        board_y = extent_y * (1.0 + 2.0 / (pattern_rows - 1))

        image_width, image_height = image_size
        travel_x = image_width - board_x - 2.0 * _SAFETY_MARGIN_PX
        travel_y = image_height - board_y - 2.0 * _SAFETY_MARGIN_PX
        if travel_x < image_width / 4.0 or travel_y < image_height / 4.0:
            return None

        span_mm = (travel_x / pixel_per_mm, travel_y / pixel_per_mm)
        step_x = span_mm[0] / (columns - 1)
        step_y = span_mm[1] / (rows - 1)

        positions: list[Point2d] = []
        for row in range(rows):
            y = (row - (rows - 1) / 2.0) * step_y
            order = range(columns) if row % 2 == 0 else reversed(range(columns))
            for column in order:
                positions.append(Point2d((column - (columns - 1) / 2.0) * step_x, y))
        # 中心視点を残差の基準にするため先頭へ出す。以降は蛇行順のままなので、
        # 中心を抜いた 1 か所（中央行）だけが 2 ステップの飛びになる
        center = Point2d(0.0, 0.0)
        if center in positions:
            positions.remove(center)
            positions.insert(0, center)

        return cls(
            positions=tuple(positions),
            columns=columns,
            rows=rows,
            span_mm=span_mm,
            max_corner_radius_px=math.hypot(
                (extent_x + travel_x) / 2.0, (extent_y + travel_y) / 2.0
            ),
        )


@attrs.frozen
class RadialResidualBucket:
    """画像半径帯ごとの残差集計.

    Attributes:
        radius_px: 帯の (下限, 上限) [px]。最外帯の上限は観測された最大半径
            （その帯が上限を超えたサンプルまで回収するため）
        sample_count: 帯に入った残差サンプル数
        rms_um: 残差の RMS [um]
        max_um: 残差の最大値 [um]
    """

    radius_px: tuple[float, float]
    sample_count: int
    rms_um: float
    max_um: float


@attrs.frozen
class ViewResidual:
    """視点ごとの残差 RMS（ボード傾き交絡の切り分け用）.

    Attributes:
        index: ``views`` 内の位置。基準視点 0 は残差を持たないので含まれない
        stage_position: その視点のステージ XY [mm]
        rms_um: その視点の残差 RMS [um]
    """

    index: int
    stage_position: Point2d
    rms_um: float


@attrs.frozen(eq=False)
class _ResidualFit:
    """基準視点との差分フィットの中間結果（ndarray を持つので永続化しない）.

    ``ResidualReport.measure`` と ``residual_field`` が共有する内部表現。行は
    (非基準視点, コーナー) の順に並び、``view_count`` × ``corners_per_view`` 行ある。

    Attributes:
        corners: (N,2) 残差を測った側（非基準視点）のコーナー画素座標
        residuals: (N,2) 残差ベクトル [px]
        radii: (N,) コーナー対の平均画像半径 [px]
        matrix: フィットした 2x2 写像（mm → px）
        pixel_per_mm: ``matrix`` のスケール成分 [px/mm]
        view_count: 非基準視点の数
        corners_per_view: 1 視点あたりのコーナー数
    """

    corners: ImageArray
    residuals: ImageArray
    radii: ImageArray
    matrix: ImageArray
    pixel_per_mm: float
    view_count: int
    corners_per_view: int


def _fit_residuals(views: Sequence[CheckerboardView]) -> _ResidualFit:
    """基準視点との差分から px↔mm 写像をフィットし、残差ベクトルを返す.

    ボードは動かずカメラがトーヘッドで動くので、基準視点（``views[0]``）との差分を
    とるとボード座標が消え ``p_v - p_0 = A (s_0 - s_v)`` になる。この ``A`` を全
    (視点, コーナー) で最小二乗フィットし、そこからのずれを残差とする。

    ``A`` は 2x2 の一般線形写像としてフィットする（等方スケール × 回転に制限しない）。
    下向きカメラでは画像 y 軸が機械 Y 軸と逆向きになり ``A`` の行列式が負になるため、
    回転のみのモデルでは表現できない。したがってステージ変位が 2 方向に広がって
    いること＝同一直線上でない 3 視点以上が必要。

    残差は 2 点の差分量なので、半径にはコーナー対の**平均画像半径**（基準視点側と
    当該視点側の半径の平均）を使う。対について対称なので基準視点の選び方に依存せず、
    歪みの勾配が半径とともに増えることが半径帯ごとの単調増加として現れる。

    Raises:
        ValueError: 視点が 2 未満、視点間でコーナー数が一致しない、または
            ステージ位置が同一直線上で写像が決まらない場合
    """
    if len(views) < 2:
        raise ValueError(f"残差の計測には2視点以上必要です: {len(views)}視点")

    all_points = [_corner_points(view) for view in views]
    reference_points = all_points[0]
    if any(points.shape != reference_points.shape for points in all_points[1:]):
        raise ValueError("視点間でコーナー数が一致しません")

    reference = views[0]
    image_width, image_height = reference.image_size
    center = np.array([image_width / 2.0, image_height / 2.0])
    reference_radii = np.linalg.norm(reference_points - center, axis=1)

    view_deltas = np.array(
        [
            [
                reference.stage_position.x - view.stage_position.x,
                reference.stage_position.y - view.stage_position.y,
            ]
            for view in views[1:]
        ]
    )
    if np.linalg.matrix_rank(view_deltas) < 2:
        raise ValueError(
            "ステージ位置が同一直線上です"
            "（変位が2方向に広がっていないと px↔mm 写像が決まりません）"
        )

    corners_per_view = int(reference_points.shape[0])
    source = np.repeat(view_deltas, corners_per_view, axis=0)
    target = np.vstack([points - reference_points for points in all_points[1:]])
    matrix = _fit_linear_map(source, target)

    return _ResidualFit(
        corners=np.vstack(all_points[1:]),
        residuals=target - source @ matrix.T,
        radii=np.concatenate(
            [
                (np.linalg.norm(points - center, axis=1) + reference_radii) / 2.0
                for points in all_points[1:]
            ]
        ),
        matrix=matrix,
        pixel_per_mm=_map_scale(matrix),
        view_count=len(views) - 1,
        corners_per_view=corners_per_view,
    )


@attrs.frozen
class ResidualReport:
    """ステージ変位を真値としたコーナー変位の残差（歪みの直接指標）.

    Attributes:
        pixel_per_mm: フィットした写像のスケール成分 [px/mm]
        rotation_deg: フィットした写像の回転成分 [deg]（カメラ取付角の指標）
        rms_um: 残差の RMS [um]
        max_um: 残差の最大値 [um]
        buckets: 画像半径帯ごとの集計
        view_residuals: 視点ごとの残差 RMS
        corner_count: 残差サンプル総数
    """

    pixel_per_mm: float
    rotation_deg: float
    rms_um: float
    max_um: float
    buckets: tuple[RadialResidualBucket, ...]
    view_residuals: tuple[ViewResidual, ...]
    corner_count: int

    @classmethod
    def measure(
        cls, views: Sequence[CheckerboardView], *, bucket_count: int = 4
    ) -> Self:
        """基準視点との差分から残差を計測する.

        フィットの数学は ``_fit_residuals`` にある。``pixel_per_mm`` はフィットした
        写像のスケール成分＝ステージ変位を定規にした値なので、印刷ボードの寸法
        精度に依存しない。

        Args:
            views: 2 視点以上、かつステージ位置が同一直線上でないこと
            bucket_count: 使う半径帯の数（``RESIDUAL_BUCKET_EDGES_PX`` の先頭から）

        Raises:
            ValueError: 視点が 2 未満、視点間でコーナー数が一致しない、または
                ステージ位置が同一直線上で写像が決まらない場合
        """
        fit = _fit_residuals(views)
        errors_um = np.linalg.norm(fit.residuals, axis=1) / fit.pixel_per_mm * 1000.0
        # 各視点は errors_um 上で連続した corners_per_view 行を占める
        per_view = errors_um.reshape(fit.view_count, fit.corners_per_view)

        return cls(
            pixel_per_mm=fit.pixel_per_mm,
            rotation_deg=_map_rotation_deg(fit.matrix),
            rms_um=float(np.sqrt(np.mean(np.square(errors_um)))),
            max_um=float(errors_um.max()),
            buckets=_radial_buckets(fit.radii, errors_um, bucket_count),
            view_residuals=tuple(
                ViewResidual(
                    index=index,
                    stage_position=view.stage_position,
                    rms_um=float(np.sqrt(np.mean(np.square(per_view[index - 1])))),
                )
                for index, view in enumerate(views[1:], start=1)
            ),
            corner_count=int(errors_um.size),
        )


def _radial_buckets(
    radii: ImageArray, errors_um: ImageArray, bucket_count: int
) -> tuple[RadialResidualBucket, ...]:
    """半径帯ごとに残差を集計する.

    最外帯は帯上限を超えたサンプルもすべて回収し、上限として実際に観測された最大
    半径を報告する（帯の合計サンプル数が全残差数と一致する＝表から黙って消えない）。
    """
    edges = RESIDUAL_BUCKET_EDGES_PX[:bucket_count]
    observed_max = float(radii.max())
    buckets: list[RadialResidualBucket] = []
    lower = 0.0
    for index, edge in enumerate(edges):
        outermost = index == len(edges) - 1
        upper = max(lower, observed_max) if outermost else edge
        inside = radii >= lower
        if not outermost:
            inside &= radii < edge
        selected = errors_um[inside]
        buckets.append(
            RadialResidualBucket(
                radius_px=(lower, upper),
                sample_count=int(selected.size),
                rms_um=float(np.sqrt(np.mean(np.square(selected))))
                if selected.size
                else 0.0,
                max_um=float(selected.max()) if selected.size else 0.0,
            )
        )
        lower = upper
    return tuple(buckets)


def undistort_views(
    views: Sequence[CheckerboardView], intrinsics: CameraIntrinsics
) -> tuple[CheckerboardView, ...]:
    """各視点のコーナーを歪み補正した視点列を返す.

    ``IntrinsicsCalibrator.solve`` が補正後の品質指標を出すために使うのと同じ処理。
    補正後の座標は元のカメラ行列を通るので（``Undistorter.apply_points``）、
    ``image_size`` と画素座標系は補正前と同じまま扱える。

    Args:
        views: 補正する視点列
        intrinsics: 内部パラメータ
    """
    undistorter = Undistorter(intrinsics)
    return tuple(
        attrs.evolve(
            view,
            corners=undistorter.apply_points(_corner_points(view)).reshape(-1, 1, 2),
        )
        for view in views
    )


def residual_field(
    views: Sequence[CheckerboardView],
) -> tuple[ImageArray, ImageArray]:
    """コーナー位置と残差ベクトルを返す（quiver 描画用）.

    ``ResidualReport.measure`` と同じフィット（``_fit_residuals``）を使い、集計する
    前の生の残差を取り出す。行は (非基準視点, コーナー) の順で
    ``N = (視点数 - 1) × 1視点のコーナー数``。基準視点 ``views[0]`` は差分の基準
    なので行を持たない。

    Args:
        views: 2 視点以上、かつステージ位置が同一直線上でないこと

    Returns:
        ``(corners, residuals)``。ともに (N,2) float64 で単位は px
        （``corners`` は画像座標、``residuals`` は残差ベクトル）

    Raises:
        ValueError: ``ResidualReport.measure`` と同じ条件
    """
    fit = _fit_residuals(views)
    return fit.corners, fit.residuals


@attrs.frozen
class CalibrationQuality:
    """Crop 拡大の可否を数値で示す品質指標.

    Attributes:
        reprojection_rms_px: ``cv2.calibrateCamera`` の再投影誤差 RMS [px]
        before: 生コーナーの残差レポート
        after: 歪み補正後コーナーの残差レポート
        pixel_per_mm_std: 視点別 pixel/mm の標準偏差（FOV 一様性の指標）。各視点で
            盤の ``square_size_mm`` を定規にして測った値のばらつきなので、視野内の
            どこで測っても同じスケールになっているかを見る。採用値そのものではない
            （採用値は ``after.pixel_per_mm`` ＝ステージ変位を定規にした値）
        view_count: 校正に使った視点数
    """

    reprojection_rms_px: float
    before: ResidualReport
    after: ResidualReport
    pixel_per_mm_std: float
    view_count: int

    def summary_lines(self) -> tuple[str, ...]:
        """操作者向けのレポート行を組み立てる（WebUI 側で文言を作らない）.

        出す pixel/mm は ``after.pixel_per_mm``＝**ステージ変位を定規にした採用値**
        （``CalibrationResult.pixel_per_mm`` と同じ値）。盤の印刷寸法を定規にした
        値ではないことを文言でも示すため「ステージ定規」と添える。
        """
        lines = [
            f"採用 pixel/mm {self.after.pixel_per_mm:.2f}（ステージ定規）"
            f" / 視点別 pixel/mm の σ={self.pixel_per_mm_std:.4f}（盤定規・FOV 一様性）",
            f"残差 RMS: 補正前 {self.before.rms_um:.1f}um"
            f" → 補正後 {self.after.rms_um:.1f}um"
            f"（最大 {self.before.max_um:.1f}um → {self.after.max_um:.1f}um）",
            f"再投影 RMS {self.reprojection_rms_px:.3f} px"
            f" / 有効視点 {self.view_count}",
        ]
        for before, after in zip(self.before.buckets, self.after.buckets, strict=True):
            lower, upper = after.radius_px
            lines.append(
                f"r {lower:.0f}-{upper:.0f}px: 前 {before.rms_um:.1f}um"
                f" → 後 {after.rms_um:.1f}um (n={after.sample_count})"
            )
        if self.after.view_residuals:
            worst = max(self.after.view_residuals, key=lambda view: view.rms_um)
            lines.append(
                f"視点別 RMS の最大: 視点 {worst.index}"
                f" (X={worst.stage_position.x:.3f}, Y={worst.stage_position.y:.3f})"
                f" {worst.rms_um:.1f}um"
            )
        lines.append(
            "残差が画像半径ではなく移動量に比例して増える場合は、"
            "レンズ歪みではなくボードの傾きを疑う"
        )
        return tuple(lines)


@attrs.frozen
class CalibrationResult:
    """キャリブレーション結果（内部パラメータ必須。旧スキーマの JSON は読めない）.

    Attributes:
        intrinsics: 内部パラメータ（カメラ行列 + 歪み係数）
        pixel_per_mm: 歪み補正後フレームの pixel/mm。**コマンドしたステージ変位を
            定規にした値**（``quality.after.pixel_per_mm``）で、印刷ボードの寸法
            精度に依存しない。消費側（``Offset.mm`` のステージ移動量、
            ``CopperProjector.pixel_of``、``safe_move_distance``）が欲しいのは
            「ステージ 1mm あたりの画素数」なのでこの定規が整合する。盤を定規に
            した値は盤の印刷誤差がそのまま倍率誤差になり、しかも残差 0 のまま
            通ってしまうため採らない
        square_size_mm: チェッカーボードの 1 マスのサイズ [mm]
            （``cv2.calibrateCamera`` の objectPoints 生成に使う）
        quality: 品質指標（残差レポートを含む）
        calibrated_at: キャリブレーション日時
        z_position: キャリブレーション時の Z 座標 [mm]
    """

    intrinsics: CameraIntrinsics
    pixel_per_mm: float
    square_size_mm: float
    quality: CalibrationQuality
    calibrated_at: datetime
    z_position: float | None = None

    @property
    def resolution(self) -> tuple[int, int]:
        """キャリブレーション時のカメラ解像度 (width, height)."""
        return self.intrinsics.resolution

    def usable_crop_side_px(self, limit_um: float) -> int:
        """残差 RMS が ``limit_um`` 以内に収まる最大の正方 crop 辺長 [px] を返す.

        補正後の半径帯を内側から見て、条件を満たす最大の帯上限 r を採り
        ``floor(2 r / sqrt(2))`` を返す（帯の境界で線形補間はしない）。サンプルが
        入った帯で最初に上限を超えた時点、またはサンプルが出たあとに空帯が現れた
        時点で打ち切る。最内の帯ですでに超過していれば 0。

        サンプルが 1 つも入っていない**先頭側の**帯は「証拠なし」として読み飛ばす
        （内側の帯は外側の帯に包含され歪みは半径に対して単調なので、外側が合格
        しているなら内側も合格している）。基準視点のコーナーが画像中央に無いと
        最内帯が空になり得るため、これを超過と同一視すると健全な校正でも 0 を返す。

        最外帯まで合格するとフレームに収まらない辺長になるため、解像度でクランプ
        する（1280x720 で半対角 734px まで合格すると 1038px になる）。
        """
        radius = 0.0
        sampled = False
        for bucket in self.quality.after.buckets:
            if bucket.sample_count == 0:
                if sampled:
                    break
                continue
            sampled = True
            if bucket.rms_um > limit_um:
                break
            radius = bucket.radius_px[1]
        width, height = self.resolution
        return min(int(radius * 2.0 / math.sqrt(2.0)), width, height)

    def to_dict(self) -> dict[str, Any]:
        """辞書に変換."""
        return _converter.unstructure(self)

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        """辞書から生成."""
        return _converter.structure(data, cls)

    def save(self, path: Path) -> None:
        """JSONファイルに保存."""
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        """JSONファイルから読み込み."""
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


class CheckerboardDetector:
    """1 フレームからコーナーを検出する。パターンサイズは初回視点で確定し以降固定.

    総当たりは 1 視点あたり 5.45 秒かかる（実測。正解サイズ 1 回なら 17.8ms）ため
    キャッシュは速度要件であり、視点間で ``pattern_size`` がぶれると objectPoints
    の対応が壊れるため精度要件でもある。検出はフル解像度で行い crop しない。
    """

    def __init__(
        self,
        pattern_rows_range: tuple[int, int] = (4, 12),
        pattern_cols_range: tuple[int, int] = (4, 12),
    ) -> None:
        self._pattern_rows_range = pattern_rows_range
        self._pattern_cols_range = pattern_cols_range
        self._pattern_size: tuple[int, int] | None = None

    @property
    def pattern_size(self) -> tuple[int, int] | None:
        """確定したパターンサイズ (cols, rows)。まだ検出できていなければ None."""
        return self._pattern_size

    def detect(self, image: Image, stage_position: Point2d) -> CheckerboardView | None:
        """フレームからコーナーを検出する（検出できなければ None）."""
        gray = cv2.cvtColor(image.numpy(), cv2.COLOR_BGR2GRAY)
        flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE

        for pattern_size in self._candidates():
            found, corners = cv2.findChessboardCorners(gray, pattern_size, flags=flags)
            if not found or corners is None:
                continue
            refined = cv2.cornerSubPix(
                gray, corners, (11, 11), (-1, -1), _SUBPIX_CRITERIA
            )
            self._pattern_size = pattern_size
            return CheckerboardView(
                stage_position=stage_position,
                corners=refined,
                pattern_size=pattern_size,
                image_size=image.size,
            )
        return None

    def draw(self, image: Image, view: CheckerboardView) -> Image:
        """検出コーナーを描画した画像を返す."""
        vis = image.numpy().copy()
        cv2.drawChessboardCorners(vis, view.pattern_size, view.corners, True)
        return Image(vis)

    def _candidates(self) -> tuple[tuple[int, int], ...]:
        """試すパターンサイズ。確定後は 1 個だけ.

        大きいパターンから試す。``findChessboardCorners`` が盤の一部だけを部分
        パターンとして返すと、その誤ったサイズが確定・キャッシュされて以降の視点も
        同じ誤りに固定されるため、包含関係で大きい側を先に確定させる方が安全。
        """
        if self._pattern_size is not None:
            return (self._pattern_size,)
        rows_min, rows_max = self._pattern_rows_range
        cols_min, cols_max = self._pattern_cols_range
        return tuple(
            (cols, rows)
            for rows in reversed(range(rows_min, rows_max))
            for cols in reversed(range(cols_min, cols_max))
        )


class IntrinsicsCalibrator:
    """多視点のコーナーから内部パラメータと補正後 pixel/mm を推定する."""

    def __init__(
        self,
        square_size_mm: float,
        resolution: tuple[int, int],
        *,
        min_views: int = 4,
    ) -> None:
        """キャリブレータを初期化する.

        Args:
            square_size_mm: チェッカーボードの 1 マスのサイズ [mm]
            resolution: 校正対象のフレームサイズ (width, height)
            min_views: ``solve`` が要求する最小視点数
        """
        self._square_size_mm = square_size_mm
        self._resolution = resolution
        self._min_views = min_views

    def solve(self, views: Sequence[CheckerboardView]) -> CalibrationResult:
        """多視点から内部パラメータを推定し、補正後の品質指標まで含めて返す.

        Raises:
            ValueError: 視点数が ``min_views`` 未満、または視点間で
                ``pattern_size`` が一致しない場合
        """
        if len(views) < self._min_views:
            raise ValueError(
                f"校正には{self._min_views}視点以上必要です: {len(views)}視点"
            )
        pattern_size = views[0].pattern_size
        if any(view.pattern_size != pattern_size for view in views):
            raise ValueError("視点間でpattern_sizeが一致しません")

        object_points = self._object_points(pattern_size)
        image_points = [
            np.asarray(view.corners, dtype=np.float32).reshape(-1, 1, 2)
            for view in views
        ]
        width, height = self._resolution
        # 名目焦点距離 = フレーム幅。値は自由（誤差は k1,k2 と tz が吸収し
        # 補正マップは不変であることを実測で確認済み）
        initial_matrix = np.array(
            [[width, 0.0, width / 2.0], [0.0, width, height / 2.0], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )
        reprojection_rms, matrix, distortion, _, _ = cv2.calibrateCamera(
            [object_points] * len(views),
            image_points,
            self._resolution,
            initial_matrix,
            np.zeros(5, dtype=np.float64),
            flags=_CALIBRATION_FLAGS,
        )

        intrinsics = CameraIntrinsics.of(matrix, distortion, self._resolution)
        corrected = undistort_views(views, intrinsics)

        # 盤を定規にした視点別スケール。FOV 一様性の指標（σ）にだけ使い、採用値には
        # しない（盤の印刷誤差がそのまま倍率誤差になり、しかも残差 0 のまま通る）
        object_2d = _object_points_mm(pattern_size, self._square_size_mm)
        scales = np.array(
            [_fit_view_scale(object_2d, _corner_points(view)) for view in corrected]
        )
        after = ResidualReport.measure(corrected)

        return CalibrationResult(
            intrinsics=intrinsics,
            # 採用値はステージ変位を定規にした pixel/mm（消費側が欲しいのはこれ）
            pixel_per_mm=after.pixel_per_mm,
            square_size_mm=self._square_size_mm,
            quality=CalibrationQuality(
                reprojection_rms_px=float(reprojection_rms),
                before=ResidualReport.measure(views),
                after=after,
                pixel_per_mm_std=float(scales.std()),
                view_count=len(views),
            ),
            calibrated_at=datetime.now(),
        )

    def _object_points(self, pattern_size: tuple[int, int]) -> ImageArray:
        """内部コーナーの盤座標 (N,1,3) float32 [mm]（``cv2.calibrateCamera`` 用）."""
        columns, rows = pattern_size
        points = np.zeros((rows * columns, 1, 3), dtype=np.float32)
        points[:, 0, :2] = _object_points_mm(pattern_size, self._square_size_mm)
        return points


def load_undistorter(
    calibration_file: Path, resolution: tuple[int, int]
) -> Undistorter | None:
    """キャリブレーション JSON から歪み補正器を作る（作れなければ None）.

    唯一の degrade 点。校正前・旧スキーマ・解像度不一致では warning ログ 1 行を
    出して None を返し、フレーム経路は素通しになる（校正ジョブ自体が動けることの担保）。

    Args:
        calibration_file: キャリブレーション JSON のパス
        resolution: 実カメラのフレームサイズ (width, height)
    """
    try:
        result = CalibrationResult.load(calibration_file)
    except Exception as exc:  # noqa: BLE001 — 外部ファイル境界の degrade 点
        logger.warning(
            "キャリブレーションを読み込めません（歪み補正なしで続行）: %s: %s",
            calibration_file,
            exc,
        )
        return None
    if result.resolution != resolution:
        logger.warning(
            "キャリブレーション解像度 %s がカメラ解像度 %s と一致しません"
            "（歪み補正なしで続行）",
            result.resolution,
            resolution,
        )
        return None
    return Undistorter(result.intrinsics)
