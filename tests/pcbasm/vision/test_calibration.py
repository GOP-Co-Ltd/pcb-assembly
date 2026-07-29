"""`pcbasm.vision.calibration` の仕様テスト.

計画書「カメラキャリブレーションのレンズ歪み補正対応」§2 §7 が契約:

- `CalibrationResult` は intrinsics 必須の破壊的変更。旧スキーマの JSON は読めない
  （再校正を強制する）。`resolution` は intrinsics から導出し二重管理しない
- `ResidualReport.measure` は基準視点との差分から `p_v - p_0 = A (s_0 - s_v)` を
  最小二乗フィットし、`pixel_per_mm` はステージ変位を定規にした値になる
  （印刷ボードの寸法精度に依存しない）。`A` は一般 2x2 でフィットするので、
  下向きカメラのように鏡映を含む配置（`det A < 0`）でも成立する
- `CalibrationResult.pixel_per_mm` は `quality.after.pixel_per_mm`＝**ステージ定規**の
  値を採用する。盤（`square_size_mm`）を定規にすると印刷誤差がそのまま倍率誤差に
  なり、しかも残差 0 のまま品質ゲートを通ってしまう。盤定規の値は視点間ばらつき
  `pixel_per_mm_std`（FOV 一様性の指標）としてのみ残る
- `ScanGrid.positions[0]` は中心視点 (0,0)。残差は差分なので順序は機能に影響しない
  が、基準視点が隅だと最内の半径帯にサンプルが入らず crop の根拠が測れない
- `ScanGrid.plan` は計画用ショットの外接矩形から、盤が視野内に収まったまま動ける
  格子を導出する。盤が視野に対して大きすぎれば None
- `CheckerboardDetector` はパターンサイズを初回視点で確定して以降固定する
  （総当たりは 1 視点 5.45 秒かかるので速度要件、視点間で size がぶれると
  objectPoints の対応が壊れるので精度要件）
- `load_undistorter` は唯一の degrade 点。不在・壊れた JSON・旧スキーマ・
  解像度不一致で warning + None
- `usable_crop_side_px` は解像度を知っている `CalibrationResult` が所有し、
  `min(side, width, height)` でクランプする（`CalibrationQuality` 側には持たない）
- `undistort_views` / `residual_field` は `solve` の内部処理を外に出したもので、
  レポート描画（`render_scan_residuals` / `draw_scan_coverage`）が使う

`TestDistortionRecovery` が本命。既知の (K, D) を持つ合成カメラで 15 視点を
レンダーして `solve` に通し、**画素空間の歪みマップ一致**で検証する。
平行同姿勢視点のみでは `(fx, t_z, k1, k2)` が `(s fx, s t_z, s^2 k1, s^4 k2)` の
1 次元族で縮退し `k1` 単体は真値からずれ得るが、画素空間の補正マップは厳密に
不変なので、`k1` の一致ではなくマップの一致をアサートするのが数学的に正しい。

3rd-party 表面（OpenCV / cattrs）はモックせず実物を通す。合成画像も
`cv2.projectPoints`（実 OpenCV）で生成し、歪みモデルを自前で書かない。
"""

import json
import logging
import math
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import attrs
import cv2
import numpy as np
import pytest

from pcbasm.geometry import Point2d
from pcbasm.vision import (
    RESIDUAL_BUCKET_EDGES_PX,
    SCAN_COLUMNS,
    SCAN_ROWS,
    CalibrationQuality,
    CalibrationResult,
    CameraIntrinsics,
    CheckerboardDetector,
    CheckerboardView,
    Image,
    IntrinsicsCalibrator,
    ResidualReport,
    ScanGrid,
    Undistorter,
    load_undistorter,
    measure_pixel_per_mm,
    residual_field,
    undistort_views,
)
from pcbasm.vision.image import ImageArray
from tests.helpers import TESTING_DATA_DIR, SyntheticCheckerboardCamera

RESOLUTION = (1280, 720)
PATTERN_SIZE = (11, 8)
SQUARE_SIZE_MM = 1.5
PIXEL_PER_MM = 30.31
CROP_SIDE = 600

# 計画 §7 が実機 1280x720 / 30.31px/mm / マス目 12x9・1.5mm から導出したステップ
STEP_X_MM = 5.66
STEP_Y_MM = 4.34

BARREL = (-0.12, 0.03, 0.0, 0.0, 0.0)
PINCUSHION = (0.15, -0.04, 0.0, 0.0, 0.0)


class _Stage:
    """合成カメラへ現在位置を渡すための可変ホルダ."""

    def __init__(self) -> None:
        self.position = Point2d(0.0, 0.0)


@pytest.fixture
def camera() -> SyntheticCheckerboardCamera:
    """実機想定の樽型歪みを持つ静止カメラ（ステージ位置は視点ごとに明示指定する）."""
    return SyntheticCheckerboardCamera(
        position=lambda: Point2d(0.0, 0.0), dist_coeffs=BARREL
    )


def _fixed_detector() -> CheckerboardDetector:
    """実機想定の 11x8 だけを試す検出器（総当たりの探索時間を掛けない）."""
    return CheckerboardDetector(pattern_rows_range=(8, 9), pattern_cols_range=(11, 12))


def _scan_offsets(
    step_x: float = STEP_X_MM, step_y: float = STEP_Y_MM
) -> tuple[Point2d, ...]:
    """5x3 の相対格子を `ScanGrid.plan` と同じ訪問順（中心 → 隅 → 蛇行）で返す.

    先頭が中心なのは残差の基準視点を画像中央の視点にするため（基準視点のコーナーが
    中央付近に集まっていないと最内の半径帯にサンプルが入らない）。
    """
    offsets: list[Point2d] = []
    for row in range(SCAN_ROWS):
        y = (row - (SCAN_ROWS - 1) / 2.0) * step_y
        columns = range(SCAN_COLUMNS)
        order = columns if row % 2 == 0 else reversed(columns)
        for column in order:
            offsets.append(Point2d((column - (SCAN_COLUMNS - 1) / 2.0) * step_x, y))
    center = Point2d(0.0, 0.0)
    offsets.remove(center)
    return (center, *offsets)


def _analytic_views(
    camera: SyntheticCheckerboardCamera,
    offsets: Sequence[Point2d] | None = None,
) -> tuple[CheckerboardView, ...]:
    """画像レンダリングを経由せず `cv2.projectPoints` から視点を組み立てる.

    残差の数学だけを検証したいケースは、検出誤差を混ぜずに済むこの経路を使う。
    """
    positions = _scan_offsets() if offsets is None else offsets
    return tuple(camera.project_view(position) for position in positions)


def _undistorted(
    views: Sequence[CheckerboardView], undistorter: Undistorter
) -> tuple[CheckerboardView, ...]:
    """視点のコーナーを歪み補正した写しを返す."""
    return tuple(
        attrs.evolve(
            view,
            corners=undistorter.apply_points(
                np.asarray(view.corners, dtype=np.float64).reshape(-1, 2)
            ).reshape(-1, 1, 2),
        )
        for view in views
    )


def _truth_intrinsics(
    camera: SyntheticCheckerboardCamera, resolution: tuple[int, int] = RESOLUTION
) -> CameraIntrinsics:
    """合成カメラの ground truth (K, D) を内部パラメータにする."""
    return CameraIntrinsics.of(camera.camera_matrix, camera.dist_coeffs, resolution)


def _truth_undistorter(camera: SyntheticCheckerboardCamera) -> Undistorter:
    """合成カメラの ground truth (K, D) から補正器を作る."""
    return Undistorter(_truth_intrinsics(camera))


def _sample_result(z_position: float | None = None) -> CalibrationResult:
    """永続化テスト用の CalibrationResult（残差レポートは実測値）."""
    camera = SyntheticCheckerboardCamera(
        position=lambda: Point2d(0.0, 0.0), dist_coeffs=BARREL
    )
    # 同一直線上にならない視点を選ぶ（一直線だと px↔mm 写像が決まらない）
    offsets = _scan_offsets()
    views = _analytic_views(camera, (offsets[0], offsets[1], offsets[7]))
    report = ResidualReport.measure(views)
    return CalibrationResult(
        intrinsics=_truth_intrinsics(camera),
        pixel_per_mm=PIXEL_PER_MM,
        square_size_mm=SQUARE_SIZE_MM,
        quality=CalibrationQuality(
            reprojection_rms_px=0.104,
            before=report,
            after=report,
            pixel_per_mm_std=0.012,
            view_count=len(views),
        ),
        calibrated_at=datetime(2026, 7, 28, 12, 0, 0),
        z_position=z_position,
    )


class TestCalibrationResult:
    """内部パラメータ必須化と永続化の契約."""

    def test_resolution_is_derived_from_intrinsics(self):
        result = _sample_result()

        assert result.resolution == result.intrinsics.resolution

    def test_save_and_load_roundtrip(self, tmp_path: Path):
        result = _sample_result(z_position=15.5)
        path = tmp_path / "calibration.json"

        result.save(path)

        assert CalibrationResult.load(path) == result

    def test_load_json_without_z_position_yields_none(self, tmp_path: Path):
        result = _sample_result()
        path = tmp_path / "calibration.json"
        result.save(path)
        data = json.loads(path.read_text())
        data.pop("z_position", None)
        path.write_text(json.dumps(data))

        assert CalibrationResult.load(path).z_position is None

    def test_load_legacy_json_without_intrinsics_raises(self, tmp_path: Path):
        # 破壊的変更のピン: 単視点方式で作った旧 JSON は読めず再校正を強制する
        path = tmp_path / "legacy.json"
        path.write_text(
            json.dumps(
                {
                    "pixel_per_mm": 100.0,
                    "square_size_mm": 1.5,
                    "mean_distance_px": 150.0,
                    "std_distance_px": 2.5,
                    "resolution": [640, 480],
                    "crop_size": [400, 400],
                    "calibrated_at": "2025-01-06T12:00:00",
                }
            )
        )

        # cattrs は構造化失敗を ExceptionGroup 派生の ClassValidationError で包み、
        # 包む型・入れ子の深さがバージョンで変わる。ここでピンしたい契約は
        # 「読めずに落ちる」ことなので、型ではなく送出そのものを見る
        with pytest.raises(Exception):
            CalibrationResult.load(path)


class TestResidualReport:
    """ステージ変位を真値とした残差の数学（画像レンダリングを経由しない）."""

    def test_requires_at_least_two_views(self, camera: SyntheticCheckerboardCamera):
        views = _analytic_views(camera, _scan_offsets()[:1])

        with pytest.raises(ValueError) as exc:
            ResidualReport.measure(views)

        assert "2視点" in str(exc.value)

    def test_rejects_collinear_stage_positions(
        self, camera: SyntheticCheckerboardCamera
    ):
        # 下向きカメラでは A の行列式が負になり回転だけでは表現できないので、
        # 変位が 2 方向に広がっている必要がある。offsets[1:4] は蛇行の 1 行目＝
        # Y が同じ 3 点なので変位が 1 方向しかない
        offsets = _scan_offsets()
        views = _analytic_views(camera, offsets[1:4])

        with pytest.raises(ValueError) as exc:
            ResidualReport.measure(views)

        assert "同一直線上" in str(exc.value)

    def test_rejects_views_with_different_corner_counts(
        self, camera: SyntheticCheckerboardCamera
    ):
        offsets = _scan_offsets()
        views = _analytic_views(camera, (offsets[0], offsets[1], offsets[7]))
        truncated = attrs.evolve(views[2], corners=views[2].corners[:-1])

        with pytest.raises(ValueError) as exc:
            ResidualReport.measure((views[0], views[1], truncated))

        assert "コーナー数" in str(exc.value)

    def test_raw_corners_show_large_residual_over_the_whole_frame(
        self, camera: SyntheticCheckerboardCamera
    ):
        # 歪みを補正しないと 15 視点のステージ変位に対して数十〜数百 um ずれる
        report = ResidualReport.measure(_analytic_views(camera))

        assert report.rms_um > 50.0
        assert report.max_um > 200.0

    def test_radial_buckets_tile_the_radius_and_keep_every_sample(
        self, camera: SyntheticCheckerboardCamera
    ):
        report = ResidualReport.measure(_analytic_views(camera))

        lower = 0.0
        for bucket in report.buckets:
            assert bucket.radius_px[0] == pytest.approx(lower)
            lower = bucket.radius_px[1]
        # どの帯にも入らずに消えるサンプルがない（最外帯が上限超過も回収する）
        assert sum(bucket.sample_count for bucket in report.buckets) == (
            report.corner_count
        )
        # 基準視点が中心なので最内帯にサンプルが入る（crop の根拠が測れる条件）
        assert report.buckets[0].sample_count > 0

    def test_outermost_bucket_collects_everything_beyond_the_nominal_edge(self):
        # 帯の px 値は 1280x720 前提の固定値で、フレーム隅の半径 723px は最外帯の
        # 上限 640px を超える。回収しないと rms_um / max_um には効くのに帯表からは
        # 消え、帯を根拠に crop を決める運用が壊れる。上限には観測最大半径を報告する
        frame_corners = np.array(
            [[8.0, 8.0], [1272.0, 8.0], [8.0, 712.0], [1272.0, 712.0]]
        )
        stages = (Point2d(0.0, 0.0), Point2d(0.1, 0.0), Point2d(0.0, 0.1))
        views = tuple(
            CheckerboardView(
                stage_position=stage,
                corners=(
                    frame_corners + PIXEL_PER_MM * np.array([stage.x, stage.y])
                ).reshape(-1, 1, 2),
                pattern_size=(2, 2),
                image_size=RESOLUTION,
            )
            for stage in stages
        )
        center = np.array([RESOLUTION[0] / 2.0, RESOLUTION[1] / 2.0])
        nearest = float(np.linalg.norm(frame_corners - center, axis=1).min())
        assert nearest > RESIDUAL_BUCKET_EDGES_PX[-1]

        report = ResidualReport.measure(views)

        assert report.buckets[-1].sample_count == report.corner_count
        assert report.buckets[-1].radius_px[1] >= nearest

    @pytest.mark.parametrize("distortion", [BARREL, PINCUSHION])
    def test_raw_residual_grows_monotonically_with_image_radius(
        self, distortion: tuple[float, float, float, float, float]
    ):
        # 半径バケットが単調増加することが「歪みが原因」のシグネチャ
        # （移動量に比例して増えるならボードの傾きを疑う、が summary の読み方）
        camera = SyntheticCheckerboardCamera(
            position=lambda: Point2d(0.0, 0.0), dist_coeffs=distortion
        )

        report = ResidualReport.measure(_analytic_views(camera))

        # サンプルが入らなかった帯は rms 0.0 を報告するので単調性の対象外
        rms = [bucket.rms_um for bucket in report.buckets if bucket.sample_count]
        assert len(rms) >= 3
        assert rms == sorted(rms)
        assert rms[-1] > rms[0] * 2.0

    def test_undistorted_corners_leave_sub_micrometre_residual(
        self, camera: SyntheticCheckerboardCamera
    ):
        # ground truth の (K, D) で補正すればステージ変位と厳密に一致する
        views = _undistorted(_analytic_views(camera), _truth_undistorter(camera))

        report = ResidualReport.measure(views)

        assert report.rms_um < 1.0
        for bucket in report.buckets:
            assert bucket.rms_um < 1.0

    @pytest.mark.parametrize(
        "mirror_y", [False, True], ids=["same-handed", "mirrored (det A < 0)"]
    )
    def test_recovers_pixel_per_mm_from_stage_displacement(self, mirror_y: bool):
        # mirror_y=True が実機のジオメトリ（下向きカメラで画像 y が機械 Y と逆向き
        # ＝ det A < 0）。A を一般 2x2 でフィットする設計はこの配置のためにあり、
        # 等方スケール × 回転に制限すると鏡映を表現できず ppm も残差も壊れる
        camera = SyntheticCheckerboardCamera(
            position=lambda: Point2d(0.0, 0.0),
            dist_coeffs=BARREL,
            mirror_y=mirror_y,
        )
        views = _undistorted(_analytic_views(camera), _truth_undistorter(camera))

        report = ResidualReport.measure(views)

        assert report.pixel_per_mm == pytest.approx(PIXEL_PER_MM, rel=1e-3)
        assert report.rms_um < 1.0

    def test_view_residuals_cover_every_view_except_the_reference(
        self, camera: SyntheticCheckerboardCamera
    ):
        views = _analytic_views(camera)

        report = ResidualReport.measure(views)

        assert len(report.view_residuals) == len(views) - 1
        assert [residual.index for residual in report.view_residuals] == list(
            range(1, len(views))
        )
        assert report.view_residuals[0].stage_position == views[1].stage_position

    def test_reports_no_rotation_for_an_aligned_camera(
        self, camera: SyntheticCheckerboardCamera
    ):
        views = _undistorted(_analytic_views(camera), _truth_undistorter(camera))

        report = ResidualReport.measure(views)

        assert report.rotation_deg == pytest.approx(0.0, abs=0.05)

    def test_recovers_camera_mount_rotation(self):
        # カメラ取付角は OffsetTransformMeasurer の 2 点法と相互検証できる指標。
        # 合成カメラは A = ppm R(theta) なので符号込みで +3.0 が期待値になる
        # （符号規約が壊れたら気付けるよう abs() は取らない）
        rotated = SyntheticCheckerboardCamera(
            position=lambda: Point2d(0.0, 0.0),
            dist_coeffs=BARREL,
            mount_rotation_deg=3.0,
        )
        views = _undistorted(_analytic_views(rotated), _truth_undistorter(rotated))

        report = ResidualReport.measure(views)

        assert report.rotation_deg == pytest.approx(3.0, abs=0.05)
        assert report.pixel_per_mm == pytest.approx(PIXEL_PER_MM, rel=1e-3)


class TestScanGrid:
    """計画用ショットからの格子導出."""

    @pytest.fixture
    def grid(self, camera: SyntheticCheckerboardCamera) -> ScanGrid:
        planned = ScanGrid.plan(
            image_size=RESOLUTION,
            corners=camera.project_corners(Point2d(0.0, 0.0)),
            pattern_size=PATTERN_SIZE,
            pixel_per_mm=PIXEL_PER_MM,
        )
        assert planned is not None
        return planned

    def test_visits_columns_times_rows_points_including_the_start(self, grid: ScanGrid):
        assert grid.columns == SCAN_COLUMNS
        assert grid.rows == SCAN_ROWS
        assert len(grid.positions) == SCAN_COLUMNS * SCAN_ROWS
        assert len(set(grid.positions)) == len(grid.positions)
        # 列・行がともに奇数なので開始位置そのもの（相対 (0,0)）が訪問点に含まれる
        assert Point2d(0.0, 0.0) in grid.positions

    def test_starts_at_the_centre_view(self, grid: ScanGrid):
        # 計画 §2 の `positions[0] == (0,0)`。残差は基準視点との差分なので順序は
        # 機能に影響しないが、基準視点が隅だと最内の半径帯にサンプルが入らず
        # `usable_crop_side_px` が測れなくなる（中心が基準なら n>0 になる）
        assert grid.positions[0] == Point2d(0.0, 0.0)

    def test_positions_form_a_full_rectangular_lattice(self, grid: ScanGrid):
        xs = sorted({position.x for position in grid.positions})
        ys = sorted({position.y for position in grid.positions})

        assert len(xs) == SCAN_COLUMNS
        assert len(ys) == SCAN_ROWS
        assert set(grid.positions) == {Point2d(x, y) for x in xs for y in ys}

    def test_moves_after_the_centre_are_single_axis_lattice_steps(self, grid: ScanGrid):
        # 中心視点の次に隅へ跳んだあとは蛇行順なので、各移動は単軸の小ステップ。
        # 中央行から中心を抜いた 1 か所だけ X が 2 ステップぶんの飛びになる
        step_x = grid.span_mm[0] / (grid.columns - 1)
        step_y = grid.span_mm[1] / (grid.rows - 1)
        serpentine = grid.positions[1:]

        for previous, current in zip(serpentine, serpentine[1:], strict=False):
            moved_x = abs(current.x - previous.x) > 1e-6
            moved_y = abs(current.y - previous.y) > 1e-6
            assert moved_x != moved_y
            assert abs(current.x - previous.x) <= 2.0 * step_x + 1e-6
            assert abs(current.y - previous.y) <= step_y + 1e-6

    def test_board_stays_inside_the_frame_at_every_position(
        self, camera: SyntheticCheckerboardCamera, grid: ScanGrid
    ):
        # findChessboardCorners は全パターンが視野内にあることを要求する
        width, height = RESOLUTION
        for position in grid.positions:
            corners = camera.project_corners(position).reshape(-1, 2)
            assert corners[:, 0].min() > 0.0
            assert corners[:, 0].max() < width - 1
            assert corners[:, 1].min() > 0.0
            assert corners[:, 1].max() < height - 1

    def test_span_is_wider_in_x_than_in_y_for_a_landscape_frame(self, grid: ScanGrid):
        assert grid.span_mm[0] > grid.span_mm[1] > 0.0

    def test_corner_coverage_reaches_beyond_the_crop_600_corner(self, grid: ScanGrid):
        # crop 600 の隅は r = 424px。ここを覆えることが crop を戻せる根拠
        crop_corner_radius = math.hypot(CROP_SIDE / 2.0, CROP_SIDE / 2.0)

        assert grid.max_corner_radius_px > crop_corner_radius
        assert grid.max_corner_radius_px < math.hypot(*RESOLUTION) / 2.0

    def test_returns_none_when_the_board_fills_the_frame(self):
        # 盤が視野に対して大きすぎると動かす余地がない（None 返却バリデーション）
        oversized = SyntheticCheckerboardCamera(
            position=lambda: Point2d(0.0, 0.0),
            dist_coeffs=BARREL,
            pixel_per_mm=90.0,
        )

        planned = ScanGrid.plan(
            image_size=RESOLUTION,
            corners=oversized.project_corners(Point2d(0.0, 0.0)),
            pattern_size=PATTERN_SIZE,
            pixel_per_mm=90.0,
        )

        assert planned is None


def _quality(
    camera: SyntheticCheckerboardCamera,
    *,
    corrected: bool,
    offsets: Sequence[Point2d] | None = None,
) -> CalibrationQuality:
    """合成カメラの 15 視点から品質指標を組み立てる."""
    raw = _analytic_views(camera, offsets)
    after = _undistorted(raw, _truth_undistorter(camera)) if corrected else raw
    return CalibrationQuality(
        reprojection_rms_px=0.1,
        before=ResidualReport.measure(raw),
        after=ResidualReport.measure(after),
        pixel_per_mm_std=0.01,
        view_count=len(raw),
    )


def _corner_first_quality(camera: SyntheticCheckerboardCamera) -> CalibrationQuality:
    """中心視点を最後に回した順（＝基準視点が格子の隅）で品質指標を組み立てる.

    残差は基準視点との差分なので、基準が隅だとコーナー対の平均半径が押し上げられ、 最内の半径帯にサンプルが 1
    つも入らない一方で最外帯は観測最大半径まで埋まる。
    """
    grid = ScanGrid.plan(
        image_size=RESOLUTION,
        corners=camera.project_corners(Point2d(0.0, 0.0)),
        pattern_size=PATTERN_SIZE,
        pixel_per_mm=camera.pixel_per_mm,
    )
    assert grid is not None
    return _quality(
        camera, corrected=True, offsets=(*grid.positions[1:], grid.positions[0])
    )


def _result_with(
    camera: SyntheticCheckerboardCamera,
    quality: CalibrationQuality,
    resolution: tuple[int, int] = RESOLUTION,
) -> CalibrationResult:
    """指定した品質指標と解像度を持つ CalibrationResult を組み立てる."""
    return CalibrationResult(
        intrinsics=_truth_intrinsics(camera, resolution),
        pixel_per_mm=PIXEL_PER_MM,
        square_size_mm=SQUARE_SIZE_MM,
        quality=quality,
        calibrated_at=datetime(2026, 7, 28, 12, 0, 0),
    )


class TestUsableCropSide:
    """Crop 拡大の可否を数値で示す指標（解像度を知る CalibrationResult が所有）.

    所有者が `CalibrationResult` 側であることは、同じ品質指標でも解像度を変えると
    値が変わる 2 テスト（クランプの有無）でピンしている。解像度を持たない
    `CalibrationQuality` にはこの計算を置けない。
    """

    @pytest.fixture
    def small_board(self) -> SyntheticCheckerboardCamera:
        """実機想定より少し小さく印刷された盤（コーナーがフレーム隅まで届く）."""
        return SyntheticCheckerboardCamera(
            position=lambda: Point2d(0.0, 0.0), dist_coeffs=BARREL, pixel_per_mm=20.0
        )

    def test_usable_crop_side_is_monotonic_in_the_limit(
        self, camera: SyntheticCheckerboardCamera
    ):
        result = _result_with(camera, _quality(camera, corrected=False))

        sides = [
            result.usable_crop_side_px(limit)
            for limit in (0.5, 30.0, 100.0, 200.0, 1.0e6)
        ]

        assert sides == sorted(sides)
        assert sides[0] == 0
        assert sides[-1] > CROP_SIDE

    def test_corrected_residuals_declare_crop_600_usable(
        self, camera: SyntheticCheckerboardCamera
    ):
        # 補正後の残差が既定上限 30um を全帯で下回るので crop 600 が使える
        result = _result_with(camera, _quality(camera, corrected=True))

        assert result.usable_crop_side_px(30.0) > CROP_SIDE

    def test_empty_inner_buckets_are_read_through_instead_of_failing(
        self, small_board: SyntheticCheckerboardCamera
    ):
        # 基準視点が隅だと最内帯にサンプルが 1 つも入らない。これを「上限超過」と
        # 同一視すると、残差が全帯で良好（補正後 0um）でも 0px を返し、crop 拡大の
        # 根拠が消える（不具合の回帰テスト）。証拠が無い先頭側の帯は読み飛ばす
        quality = _corner_first_quality(small_board)

        assert quality.after.buckets[0].sample_count == 0
        assert _result_with(small_board, quality).usable_crop_side_px(30.0) > CROP_SIDE

    def test_side_never_exceeds_the_shorter_frame_edge(
        self, small_board: SyntheticCheckerboardCamera
    ):
        # 最外帯（観測最大半径 628px）まで合格すると素の式は 888px になり、720px 高の
        # フレームに収まらない値を推奨してしまう（不具合の回帰テスト）
        result = _result_with(small_board, _corner_first_quality(small_board))

        assert result.usable_crop_side_px(1.0e6) == min(RESOLUTION)

    def test_side_is_not_clamped_when_the_frame_is_large_enough(
        self, small_board: SyntheticCheckerboardCamera
    ):
        # クランプが常に効いているわけではないことのピン（解像度だけを変えた対照）。
        # 解像度で値が変わる＝この計算は解像度を持つ CalibrationResult にしか置けない
        result = _result_with(
            small_board, _corner_first_quality(small_board), (2000, 2000)
        )

        assert result.usable_crop_side_px(1.0e6) > min(RESOLUTION)


class TestCalibrationQuality:
    """操作者向けレポート行の組み立て（WebUI 側で文言を作らない）."""

    def test_summary_lines_report_before_and_after_and_every_bucket(
        self, camera: SyntheticCheckerboardCamera
    ):
        quality = _quality(camera, corrected=True)

        lines = quality.summary_lines()

        assert all(isinstance(line, str) for line in lines)
        assert any("補正前" in line and "補正後" in line for line in lines)
        assert sum(1 for line in lines if line.startswith("r ")) == len(
            quality.after.buckets
        )


class TestUndistortViews:
    """視点列の歪み補正（`solve` の内部処理をレポート側から使えるようにしたもの）."""

    def test_keeps_view_metadata_and_corner_shape(
        self, camera: SyntheticCheckerboardCamera
    ):
        views = _analytic_views(camera)

        corrected = undistort_views(
            views,
            _truth_intrinsics(camera),
        )

        assert len(corrected) == len(views)
        for before, after in zip(views, corrected, strict=True):
            assert after.stage_position == before.stage_position
            assert after.pattern_size == before.pattern_size
            assert after.image_size == before.image_size
            assert after.corners.shape == before.corners.shape

    def test_ground_truth_intrinsics_remove_the_residual(
        self, camera: SyntheticCheckerboardCamera
    ):
        # 補正後座標は元のカメラ行列を通るので、ステージ変位と厳密に一致する
        corrected = undistort_views(
            _analytic_views(camera),
            _truth_intrinsics(camera),
        )

        report = ResidualReport.measure(corrected)

        assert report.rms_um < 1.0
        assert report.pixel_per_mm == pytest.approx(PIXEL_PER_MM, rel=1e-3)

    def test_source_views_are_left_untouched(self, camera: SyntheticCheckerboardCamera):
        views = _analytic_views(camera)
        original = views[3].corners.copy()

        undistort_views(
            views,
            _truth_intrinsics(camera),
        )

        assert np.array_equal(views[3].corners, original)


class TestResidualField:
    """Quiver 描画用の生の残差（集計前）."""

    def test_one_row_per_corner_of_every_non_reference_view(
        self, camera: SyntheticCheckerboardCamera
    ):
        views = _analytic_views(camera)
        corners_per_view = PATTERN_SIZE[0] * PATTERN_SIZE[1]

        positions, residuals = residual_field(views)

        expected_rows = (len(views) - 1) * corners_per_view
        assert positions.shape == (expected_rows, 2)
        assert residuals.shape == (expected_rows, 2)

    def test_positions_are_image_coordinates_inside_the_frame(
        self, camera: SyntheticCheckerboardCamera
    ):
        width, height = RESOLUTION

        positions, _ = residual_field(_analytic_views(camera))

        assert positions[:, 0].min() >= 0.0
        assert positions[:, 0].max() <= width
        assert positions[:, 1].min() >= 0.0
        assert positions[:, 1].max() <= height

    def test_residuals_are_pixels_consistent_with_the_report(
        self, camera: SyntheticCheckerboardCamera
    ):
        # 描画側が um 換算するときの規約（residuals / pixel_per_mm * 1000）をピンする
        views = _analytic_views(camera)
        report = ResidualReport.measure(views)

        _, residuals = residual_field(views)

        errors_um = np.linalg.norm(residuals, axis=1) / report.pixel_per_mm * 1000.0
        assert math.sqrt(float((errors_um**2).mean())) == pytest.approx(
            report.rms_um, rel=1e-6
        )
        assert errors_um.max() == pytest.approx(report.max_um, rel=1e-6)

    def test_correcting_the_views_shrinks_the_field(
        self, camera: SyntheticCheckerboardCamera
    ):
        # 2 パネル（補正前/後）を同一スケールで描くための素材になっていること
        views = _analytic_views(camera)
        corrected = undistort_views(
            views,
            _truth_intrinsics(camera),
        )

        _, before = residual_field(views)
        _, after = residual_field(corrected)

        assert np.linalg.norm(before, axis=1).max() > 1.0
        assert np.linalg.norm(after, axis=1).max() < 0.01


class TestCheckerboardDetector:
    """パターンサイズの確定とキャッシュ."""

    @pytest.fixture
    def image(self) -> Image:
        camera = SyntheticCheckerboardCamera(
            position=lambda: Point2d(0.0, 0.0), dist_coeffs=BARREL
        )
        return camera.render(Point2d(0.0, 0.0))

    def test_detects_pattern_and_records_frame_context(self, image: Image):
        detector = _fixed_detector()

        view = detector.detect(image, Point2d(1.0, 2.0))

        assert view is not None
        assert view.pattern_size == PATTERN_SIZE
        assert view.image_size == RESOLUTION
        assert view.stage_position == Point2d(1.0, 2.0)
        assert view.corners.reshape(-1, 2).shape == (
            PATTERN_SIZE[0] * PATTERN_SIZE[1],
            2,
        )

    def test_pattern_size_is_none_until_a_detection_succeeds(self):
        detector = _fixed_detector()
        blank = Image(np.full((720, 1280, 3), 128, dtype=np.uint8))

        assert detector.pattern_size is None
        assert detector.detect(blank, Point2d(0.0, 0.0)) is None
        assert detector.pattern_size is None

    def test_pattern_size_is_locked_after_the_first_view(self, image: Image):
        # 5x5 の実画像で確定させたあと、別サイズの盤は検出しない
        # （視点間で pattern_size がぶれると objectPoints の対応が壊れる）
        detector = CheckerboardDetector(
            pattern_rows_range=(5, 9), pattern_cols_range=(5, 12)
        )
        real_board = Image.load(TESTING_DATA_DIR / "checkerboard.png")

        first = detector.detect(real_board, Point2d(0.0, 0.0))

        assert first is not None
        assert detector.pattern_size == (5, 5)
        assert detector.detect(image, Point2d(0.0, 0.0)) is None
        assert detector.pattern_size == (5, 5)

    def test_draw_returns_an_annotated_frame_of_the_same_size(self, image: Image):
        detector = _fixed_detector()
        view = detector.detect(image, Point2d(0.0, 0.0))
        assert view is not None

        annotated = detector.draw(image, view)

        assert annotated.size == image.size
        assert not np.array_equal(annotated.numpy(), image.numpy())


class TestIntrinsicsCalibrator:
    """視点集合の前提条件と、採用 pixel/mm がどちらの定規で測られるか."""

    def test_rejects_too_few_views(self, camera: SyntheticCheckerboardCamera):
        views = _analytic_views(camera, _scan_offsets()[:3])
        calibrator = IntrinsicsCalibrator(SQUARE_SIZE_MM, RESOLUTION, min_views=4)

        with pytest.raises(ValueError) as exc:
            calibrator.solve(views)

        assert "4視点" in str(exc.value)

    def test_rejects_views_with_inconsistent_pattern_size(
        self, camera: SyntheticCheckerboardCamera
    ):
        views = list(_analytic_views(camera, _scan_offsets()[:4]))
        views[2] = attrs.evolve(views[2], pattern_size=(8, 11))
        calibrator = IntrinsicsCalibrator(SQUARE_SIZE_MM, RESOLUTION, min_views=4)

        with pytest.raises(ValueError) as exc:
            calibrator.solve(views)

        assert "pattern_size" in str(exc.value)

    def test_adopted_pixel_per_mm_uses_the_stage_as_the_ruler(self):
        """盤が 1% 大きく印刷されていても採用 ppm はステージ定規の真値に一致する.

        消費側（`Offset.mm` のステージ移動量・`CopperProjector.pixel_of`・
        `safe_move_distance`）が欲しいのは「ステージ 1mm あたりの画素数」。盤を定規に
        すると印刷誤差がそのまま倍率誤差になり、しかも**残差は 0 のまま**なので品質
        ゲートに掛からない（画像中心から 10mm の点で 1% = 100um ＝ 0.1mm 予算を単独で
        使い切る）。ここでは操作者が公称 1.5mm と入力し、実際の盤が 1.515mm という
        取り違えを再現している。
        """
        oversized = SyntheticCheckerboardCamera(
            position=lambda: Point2d(0.0, 0.0),
            dist_coeffs=BARREL,
            square_size_mm=SQUARE_SIZE_MM * 1.01,
        )
        views = _analytic_views(oversized)
        calibrator = IntrinsicsCalibrator(SQUARE_SIZE_MM, RESOLUTION)

        result = calibrator.solve(views)

        assert result.pixel_per_mm == pytest.approx(PIXEL_PER_MM, rel=1e-3)
        # 対照: 同じ視点を盤（公称 square_size）で測ると +1% ずれる
        corrected = _undistorted(views, _truth_undistorter(oversized))
        board_ruler = measure_pixel_per_mm(corrected[0], SQUARE_SIZE_MM)
        assert board_ruler == pytest.approx(PIXEL_PER_MM * 1.01, rel=1e-3)


class TestLoadUndistorter:
    """唯一の degrade 点: 作れなければ warning 1 行 + None."""

    def test_builds_an_undistorter_for_a_matching_resolution(self, tmp_path: Path):
        path = tmp_path / "calibration.json"
        _sample_result().save(path)

        undistorter = load_undistorter(path, RESOLUTION)

        assert undistorter is not None
        assert undistorter.resolution == RESOLUTION

    def test_missing_file_degrades_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING):
            undistorter = load_undistorter(tmp_path / "absent.json", RESOLUTION)

        assert undistorter is None
        assert len(caplog.records) == 1

    def test_broken_json_degrades_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        path = tmp_path / "broken.json"
        path.write_text("{ not json")

        with caplog.at_level(logging.WARNING):
            undistorter = load_undistorter(path, RESOLUTION)

        assert undistorter is None
        assert len(caplog.records) == 1

    def test_legacy_schema_degrades_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        path = tmp_path / "legacy.json"
        path.write_text(
            json.dumps(
                {
                    "pixel_per_mm": 30.31,
                    "square_size_mm": 1.5,
                    "mean_distance_px": 45.5,
                    "std_distance_px": 0.5,
                    "resolution": [1280, 720],
                    "crop_size": [600, 600],
                    "calibrated_at": "2026-07-01T00:00:00",
                }
            )
        )

        with caplog.at_level(logging.WARNING):
            undistorter = load_undistorter(path, RESOLUTION)

        assert undistorter is None
        assert len(caplog.records) == 1

    def test_resolution_mismatch_degrades_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        # FixedImageCamera が 400x400 を返すテスト経路は自動的に補正なしへ落ちる
        path = tmp_path / "calibration.json"
        _sample_result().save(path)

        with caplog.at_level(logging.WARNING):
            undistorter = load_undistorter(path, (400, 400))

        assert undistorter is None
        assert len(caplog.records) == 1


def _pixel_grid(steps: int = 20) -> ImageArray:
    """画像全域を覆う steps x steps の格子点 (N,2) を返す."""
    width, height = RESOLUTION
    xs, ys = np.meshgrid(
        np.linspace(0.0, width - 1.0, steps), np.linspace(0.0, height - 1.0, steps)
    )
    return np.column_stack([xs.ravel(), ys.ravel()])


def _similarity_residual_px(fitted: ImageArray, truth: ImageArray) -> ImageArray:
    """一様スケール + 平行移動を最小二乗で吸収した後の残差 [px] を返す.

    スケール差の吸収が正当なのは、`pixel_per_mm` を補正後画像で測り直すため
    （縮退族 `(s fx, s t_z, s^2 k1, s^4 k2)` は画素空間マップを相似変換の範囲で
    しか動かさない）。
    """
    fitted_centred = fitted - fitted.mean(axis=0)
    truth_centred = truth - truth.mean(axis=0)
    scale = float((fitted_centred * truth_centred).sum() / (fitted_centred**2).sum())
    return np.linalg.norm(truth_centred - scale * fitted_centred, axis=1)


class TestDistortionRecovery:
    """本命: 既知の歪みを画素空間の補正マップとして復元できるか.

    `SyntheticCheckerboardCamera` が既知の (K, D) で 15 視点をレンダーし、
    `CheckerboardDetector` → `IntrinsicsCalibrator.solve` を通す。合成カメラは
    実 `cv2.projectPoints` で投影し、被テスト実装のミラーになる自前歪みモデルは
    書かない。

    閾値は本ブランチでの実測値に対する余裕を持たせてある（樽型 / 糸巻き型の
    どちらでも 全画面 max 約 0.19px、中央 600 max 約 0.05px、
    補正後残差 RMS 約 4.2um、補正前残差 RMS 119〜144um）。
    """

    @pytest.fixture(params=[BARREL, PINCUSHION], ids=["barrel", "pincushion"])
    def recovered(
        self, request: pytest.FixtureRequest
    ) -> tuple[SyntheticCheckerboardCamera, CalibrationResult]:
        distortion = request.param
        stage = _Stage()
        camera = SyntheticCheckerboardCamera(
            position=lambda: stage.position, dist_coeffs=distortion
        )
        detector = _fixed_detector()

        views: list[CheckerboardView] = []
        for offset in _scan_offsets():
            stage.position = offset
            view = detector.detect(camera.capture(), offset)
            assert view is not None, f"{offset} で検出できませんでした"
            views.append(view)

        calibrator = IntrinsicsCalibrator(SQUARE_SIZE_MM, RESOLUTION)
        return camera, calibrator.solve(views)

    def test_pixel_space_map_matches_ground_truth_over_the_whole_frame(
        self, recovered: tuple[SyntheticCheckerboardCamera, CalibrationResult]
    ):
        camera, result = recovered
        grid = _pixel_grid()

        fitted = Undistorter(result.intrinsics).apply_points(grid)
        truth = _truth_undistorter(camera).apply_points(grid)

        residual = _similarity_residual_px(fitted, truth)
        assert residual.max() < 0.5

    def test_pixel_space_map_matches_ground_truth_inside_crop_600(
        self, recovered: tuple[SyntheticCheckerboardCamera, CalibrationResult]
    ):
        # 実際に使う範囲。0.2px = 6.6um で 0.1mm 予算の 1/15
        camera, result = recovered
        grid = _pixel_grid()
        width, height = RESOLUTION
        inside = (np.abs(grid[:, 0] - width / 2.0) <= CROP_SIDE / 2.0) & (
            np.abs(grid[:, 1] - height / 2.0) <= CROP_SIDE / 2.0
        )

        fitted = Undistorter(result.intrinsics).apply_points(grid)
        truth = _truth_undistorter(camera).apply_points(grid)

        residual = _similarity_residual_px(fitted, truth)[inside]
        assert residual.max() < 0.2

    def test_residual_report_improves_by_an_order_of_magnitude(
        self, recovered: tuple[SyntheticCheckerboardCamera, CalibrationResult]
    ):
        _, result = recovered
        quality = result.quality

        assert quality.before.rms_um > 50.0
        assert quality.after.rms_um < 8.0
        assert quality.after.rms_um < quality.before.rms_um / 10.0

    def test_reports_pixel_per_mm_and_view_count(
        self, recovered: tuple[SyntheticCheckerboardCamera, CalibrationResult]
    ):
        _, result = recovered

        assert result.pixel_per_mm == pytest.approx(PIXEL_PER_MM, rel=2e-3)
        assert result.quality.view_count == SCAN_COLUMNS * SCAN_ROWS
        assert result.quality.pixel_per_mm_std < 0.02
        assert result.quality.reprojection_rms_px < 0.5

    def test_distortion_sign_and_magnitude_are_plausible(
        self, recovered: tuple[SyntheticCheckerboardCamera, CalibrationResult]
    ):
        # k1 単体は縮退により真値からずれ得るので符号と桁だけを見る
        # （厳密な検証は上の画素空間マップ一致テスト）
        camera, result = recovered
        expected = float(camera.dist_coeffs[0])
        actual = result.intrinsics.distortion[0]

        assert math.copysign(1.0, actual) == math.copysign(1.0, expected)
        assert 0.1 < abs(actual) / abs(expected) < 10.0

    def test_corrected_frames_keep_the_checkerboard_detectable(
        self, recovered: tuple[SyntheticCheckerboardCamera, CalibrationResult]
    ):
        # 補正が画像経路として成立していること（黒縁で盤が欠けない）
        camera, result = recovered
        undistorter = Undistorter(result.intrinsics)
        detector = _fixed_detector()

        corrected = undistorter.apply(camera.render(Point2d(0.0, 0.0)))

        assert detector.detect(corrected, Point2d(0.0, 0.0)) is not None


def test_synthetic_camera_matches_opencv_projection():
    """合成カメラの描画が `cv2.projectPoints` の真値コーナーと整合する（素材の健全性）.

    このテストが落ちたら被テスト実装ではなく合成素材を疑う。
    """
    camera = SyntheticCheckerboardCamera(
        position=lambda: Point2d(0.0, 0.0), dist_coeffs=BARREL
    )
    detector = _fixed_detector()

    view = detector.detect(camera.render(Point2d(0.0, 0.0)), Point2d(0.0, 0.0))

    assert view is not None
    detected = np.asarray(view.corners, dtype=np.float64).reshape(-1, 2)
    truth = camera.project_corners(Point2d(0.0, 0.0)).reshape(-1, 2)
    assert np.linalg.norm(detected - truth, axis=1).max() < 0.3


def test_undistorter_map_agrees_with_opencv_undistort_points():
    """`Undistorter.apply_points` が `cv2.undistortPoints(P=K)` と一致する（規約のピン）."""
    intrinsics = CameraIntrinsics.of(
        np.array([[1280.0, 0.0, 640.0], [0.0, 1280.0, 360.0], [0.0, 0.0, 1.0]]),
        np.array(BARREL),
        RESOLUTION,
    )
    points = _pixel_grid(steps=5)

    applied = Undistorter(intrinsics).apply_points(points)

    expected = cv2.undistortPoints(
        points.reshape(-1, 1, 2),
        intrinsics.matrix(),
        intrinsics.coefficients(),
        P=intrinsics.matrix(),
    ).reshape(-1, 2)
    assert applied == pytest.approx(expected, abs=1e-6)
