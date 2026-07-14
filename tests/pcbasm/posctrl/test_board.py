"""Posctrl/board の仕様テスト.

計画書 memory/agents/implementation-planner/board-corner-calibration.md
「新計測器」節に基づく。

- fit_affine_transform(board_points, machine_points) -> Compose:
  並進含む 6DOF affine の最小二乗フィット。3点未満は ValueError。
- BoardTransformMeasurer.measure(marker_pos) -> Compose:
  T0 = Shift(marker_pos − offset − corner.board_position(w, h)) を初期推定に、
  基板4隅の外形輪郭を CopperPadObserver + XYPositionAdjustor でサーボ計測し、
  4対応点の最小二乗で board→machine 変換を返す。基準点マーカーの座標は
  最終変換に残らない（粗並進の初期推定のみ）。1隅でも照合不能なら
  コーナー名（corner.value）入りの RuntimeError で即中止（4隅必須）。

計測器の結合テストはエッジ検出・照合とも実装（実 OpenCV パイプライン）を
使い、カメラは現在のステージ位置から見えを描画する closed-loop の自前
Camera Impl、Klipper / XYZStage は自前 HAL のため mocker.Mock を使用する
（tests/pcbasm/posctrl/test_position.py の流儀 + 位置追従）。
"""

from collections.abc import Iterator
from pathlib import Path
from typing import override

import cv2
import numpy as np
import pytest
import shapely
from pytest_mock import MockerFixture

from pcbasm import gcode
from pcbasm.config import (
    BoardAlign,
    Corner,
    ReferencePoint,
    get_machine_config,
)
from pcbasm.geometry import (
    Compose,
    Identity,
    Point2d,
    Point3d,
    Rotation,
    Scale,
    Shift,
    Transform,
)
from pcbasm.hal import Camera, CameraInfo, Resolution
from pcbasm.pcb import Outline
from pcbasm.posctrl import OrthogonalityMetrics
from pcbasm.posctrl.board import BoardTransformMeasurer, fit_affine_transform
from pcbasm.posctrl.setup import (
    BoardCalibrationResult,
    machine_session,
    setup_board_calibration,
)
from pcbasm.vision import Image
from tests.helpers import mark_hardware

PPM = 10.0  # pixel/mm
IMAGE_SIZE = (200, 200)
BOARD_W = 30.0  # mm
BOARD_H = 20.0  # mm
BOARD_POLYGON = shapely.box(0.0, 0.0, BOARD_W, BOARD_H)
BOARD_CORNERS = (
    Point2d(0.0, 0.0),
    Point2d(BOARD_W, 0.0),
    Point2d(0.0, BOARD_H),
    Point2d(BOARD_W, BOARD_H),
)
OFFSET = (2.5, -2.5)  # 基板コーナー → マーカー [mm]
# 合成描画は 1px(=0.1mm) 量子化なので、既定 tolerance(0.05mm) では量子化残差で
# 収束しない。1px 強を許容する値にする。
TOLERANCE = 0.12


class TestFitAffineTransform:
    """fit_affine_transform の 6DOF 最小二乗フィットのテスト."""

    def test_recovers_rotation_scale_translation_exactly(self):
        """回転+スケール+並進の既知アフィンを4隅対応点から厳密復元する."""
        truth = Compose([Scale(1.02, 1.02), Rotation(7.0), Shift(3.0, -2.0)])
        machine = [truth.apply(b) for b in BOARD_CORNERS]

        fitted = fit_affine_transform(BOARD_CORNERS, machine)

        assert isinstance(fitted, Compose)
        for point in (*BOARD_CORNERS, Point2d(15.0, 10.0)):
            mapped = fitted.apply(point)
            expected = truth.apply(point)
            assert mapped.x == pytest.approx(expected.x, abs=1e-6)
            assert mapped.y == pytest.approx(expected.y, abs=1e-6)

    def test_recovers_mirror_transform(self):
        """鏡映（det<0）を含むアフィンも復元できる（剛体前提の実装はここで割れる）."""
        truth = Compose([Scale.flip(y=True), Rotation(3.0), Shift(1.0, 2.0)])
        machine = [truth.apply(b) for b in BOARD_CORNERS]

        fitted = fit_affine_transform(BOARD_CORNERS, machine)

        for point in (*BOARD_CORNERS, Point2d(15.0, 10.0)):
            mapped = fitted.apply(point)
            expected = truth.apply(point)
            assert mapped.x == pytest.approx(expected.x, abs=1e-6)
            assert mapped.y == pytest.approx(expected.y, abs=1e-6)

    def test_five_points_fit_by_least_squares(self):
        """和も一次モーメントも 0 の対称ノイズを載せた5点で真値に一致する.

        4隅に (+d, −d, −d, +d) のノイズを載せると最小二乗解は真の変換と厳密に 一致する。先頭3点の厳密解などの非
        LSQ 実装はノイズが解へ漏れて割れる。
        """
        truth = Compose([Rotation(2.0), Shift(1.0, 0.5)])
        d = Point2d(0.1, 0.0)
        noise = (d, Point2d(-d.x, 0.0), Point2d(-d.x, 0.0), d)
        board = [*BOARD_CORNERS, Point2d(15.0, 10.0)]
        machine = [
            truth.apply(b) + n for b, n in zip(BOARD_CORNERS, noise, strict=True)
        ]
        machine.append(truth.apply(Point2d(15.0, 10.0)))

        fitted = fit_affine_transform(board, machine)

        for point in board:
            mapped = fitted.apply(point)
            expected = truth.apply(point)
            assert mapped.x == pytest.approx(expected.x, abs=1e-6)
            assert mapped.y == pytest.approx(expected.y, abs=1e-6)

    def test_fewer_than_three_points_raise_value_error(self):
        """アフィン 6 未知数は 2 対応点では決まらないので ValueError."""
        board = [Point2d(0.0, 0.0), Point2d(30.0, 0.0)]
        machine = [Point2d(1.0, 1.0), Point2d(31.0, 1.0)]

        with pytest.raises(ValueError):
            fit_affine_transform(board, machine)


class _BoardSceneCamera(Camera):
    """現在のステージ位置に応じて基板の見えを描画する closed-loop テスト用 Camera Impl.

    真の board 変換を知っており、投影規約 pixel = center + ppm * (stage −
    T_true(b))（tests/pcbasm/posctrl/test_copper.py の符号ピンと同一）で明背景 (240)
    に暗基板矩形 (60) を描く。固定画像列ではなくステージ位置から描画
    するため、キャプチャ回数・収束反復回数という内部実装に依存しない振る舞い レベルの結合検証ができる。blank_near
    を与えると、その機械座標の近傍では エッジの無い無地画像を返す（コーナー照合失敗の再現用）。
    """

    def __init__(
        self,
        stage_position: list[Point2d],
        board_transform: Transform,
        polygon: shapely.Polygon,
        *,
        blank_near: Point2d | None = None,
    ) -> None:
        self._stage_position = stage_position
        self._board_transform = board_transform
        self._polygon = polygon
        self._blank_near = blank_near

    @property
    @override
    def resolution(self) -> Resolution:
        return Resolution(width=IMAGE_SIZE[0], height=IMAGE_SIZE[1], fps=30.0)

    @property
    @override
    def info(self) -> CameraInfo:
        return CameraInfo(name="BoardSceneCamera", formats={"BGR": [self.resolution]})

    @override
    def capture(self) -> Image:
        width, height = IMAGE_SIZE
        frame = np.full((height, width, 3), 240, dtype=np.uint8)
        stage = self._stage_position[0]
        if self._blank_near is not None and (stage - self._blank_near).norm < 3.0:
            return Image(frame)
        pixels = []
        for bx, by in self._polygon.exterior.coords:
            machine = self._board_transform.apply(Point2d(bx, by))
            pixels.append(
                [
                    width / 2.0 + PPM * (stage.x - machine.x),
                    height / 2.0 + PPM * (stage.y - machine.y),
                ]
            )
        points = np.round(np.array(pixels)).astype(np.int32).reshape(-1, 1, 2)
        cv2.fillPoly(frame, [points], (60, 60, 60))
        return Image(frame)


def _tracking_stage(
    mocker: MockerFixture, position: list[Point2d], moves: list[Point2d]
):
    """Move() 指令で現在位置が更新される XYZStage の Mock.

    test_position.py の Mock 流儀に、サーボの収束シミュレーションに必要な 位置追従（get_position
    が直近の指令位置を返す）を加えたもの。
    """
    stage = mocker.Mock()
    stage.max_velocity = 100.0
    stage.get_position.side_effect = lambda: Point3d(position[0].x, position[0].y, 5.0)

    def move(**kwargs: object) -> gcode.GCode:
        current = position[0]
        x = kwargs.get("x")
        y = kwargs.get("y")
        position[0] = Point2d(
            current.x if x is None else float(x),  # type: ignore[arg-type]
            current.y if y is None else float(y),  # type: ignore[arg-type]
        )
        moves.append(position[0])
        return gcode.GCode("G1")

    stage.move.side_effect = move
    return stage


def _reference_point(corner: Corner, marker_pos: Point2d) -> ReferencePoint:
    return ReferencePoint(
        x=marker_pos.x,
        y=marker_pos.y,
        target_diameter=3.0,
        offset=OFFSET,
        corner=corner,
    )


def _measurer(
    camera: Camera,
    klipper: object,
    stage: object,
    reference_point: ReferencePoint,
) -> BoardTransformMeasurer:
    return BoardTransformMeasurer(
        camera=camera,
        klipper=klipper,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        outline=Outline(BOARD_POLYGON),
        reference_point=reference_point,
        offset_transform=Identity(),
        pixel_per_mm=PPM,
        image_size=IMAGE_SIZE,
        board_align=BoardAlign(tolerance=TOLERANCE),
        settle_time=0.0,
    )


class TestBoardTransformMeasurer:
    """BoardTransformMeasurer.measure() の4隅輪郭サーボ計測のテスト."""

    @pytest.mark.parametrize("anchor", [Corner.TOP_LEFT, Corner.BOTTOM_RIGHT])
    def test_measure_recovers_known_transform_by_four_corner_servo(
        self, mocker: MockerFixture, anchor: Corner
    ):
        """既知の 6DOF 変換（回転+スケール+並進）を4隅サーボ計測から復元する.

        marker_pos にはアンカー基準の粗い初期推定誤差 (0.4, −0.3) mm を仕込む。
        最終変換は基準点によらず基板4隅の輪郭計測だけで決まるため、この誤差は 復元結果に残ってはならない（並進 DOF
        のピン）。アンカーコーナーを 変えても T0 = Shift(marker − offset −
        corner.board_position(w, h)) が 正しい初期推定を与える（4隅とも探索窓内に収まり収束する）。
        """
        truth = Compose([Scale(1.015, 1.015), Rotation(0.8), Shift(50.0, 40.0)])
        anchor_machine = truth.apply(anchor.board_position(BOARD_W, BOARD_H))
        marker_pos = anchor_machine + Point2d(*OFFSET) + Point2d(0.4, -0.3)
        position = [marker_pos]  # マーカーへのサーボ収束直後のステージ位置
        moves: list[Point2d] = []
        camera = _BoardSceneCamera(position, truth, BOARD_POLYGON)
        stage = _tracking_stage(mocker, position, moves)
        measurer = _measurer(
            camera, mocker.Mock(), stage, _reference_point(anchor, marker_pos)
        )

        transform = measurer.measure(marker_pos)

        assert isinstance(transform, Compose)
        for board_corner in BOARD_CORNERS:
            mapped = transform.apply(board_corner)
            expected = truth.apply(board_corner)
            assert mapped.x == pytest.approx(expected.x, abs=0.3)
            assert mapped.y == pytest.approx(expected.y, abs=0.3)
        # T0 を初期推定として4隅の近傍だけへ移動する（見当違いの場所へは
        # 行かない）。呼び出し順・回数はピンしない
        machine_corners = [truth.apply(b) for b in BOARD_CORNERS]
        for commanded in moves:
            assert min((commanded - m).norm for m in machine_corners) < 3.0

    def test_measure_aborts_with_corner_name_when_a_corner_has_no_edges(
        self, mocker: MockerFixture
    ):
        """1隅でも照合不能なら該当コーナー名入り RuntimeError で即中止（4隅必須）."""
        truth = Shift(50.0, 40.0)
        marker_pos = truth.apply(Point2d(0.0, 0.0)) + Point2d(*OFFSET)
        position = [marker_pos]
        camera = _BoardSceneCamera(
            position,
            truth,
            BOARD_POLYGON,
            # top_right コーナーの近傍だけエッジの無い無地画像になる
            blank_near=truth.apply(Point2d(BOARD_W, 0.0)),
        )
        stage = _tracking_stage(mocker, position, [])
        measurer = _measurer(
            camera,
            mocker.Mock(),
            stage,
            _reference_point(Corner.TOP_LEFT, marker_pos),
        )

        with pytest.raises(RuntimeError) as exc:
            measurer.measure(marker_pos)

        assert "top_right" in str(exc.value)


@mark_hardware
class TestBoardCornerHardware:
    """実機通し（実カメラ + 実 Moonraker、configs/kurousagi）。ユーザー実行.

    前提（計画書「検証」節のユーザー実機確認 ①②）:

    - Moonraker が localhost:7125 で稼働し、各軸がホーミング可能であること
    - 実カメラが接続済みでキャリブレーション済みであること
    - data/testing/fill_coverage の基板がステージにセットされ、アンカー
      コーナーの基準点マーカー1個と基板4コーナーがすべてカメラ視野/可動域内
      にあること
    """

    @pytest.fixture(scope="class")
    def calibration(self) -> Iterator[BoardCalibrationResult]:
        """アンカー1点サーボ → 基板4隅輪郭計測の通し（クラス内で1回だけ実行）."""
        machine = get_machine_config("kurousagi")
        result = setup_board_calibration(
            machine,
            Path("data/testing/fill_coverage/fill_coverage.kicad_pcb"),
        )
        with machine_session(result.klipper, machine):
            yield result

    def test_setup_board_calibration_completes(
        self, calibration: BoardCalibrationResult
    ):
        """セットアップが完走し board→machine の Compose 契約を返す."""
        assert isinstance(calibration.board_transform, Compose)

    def test_orthogonality_metrics_within_thresholds(
        self, calibration: BoardCalibrationResult
    ):
        """計測変換の scale / axis が妥当な範囲（ベベル等の誤エッジロック検出線）."""
        metrics = OrthogonalityMetrics.from_transform(calibration.board_transform)

        assert metrics.scale_x == pytest.approx(1.0, abs=0.02)
        assert metrics.scale_y == pytest.approx(1.0, abs=0.02)
        assert abs(metrics.axis_angle_error_deg) < 0.5
