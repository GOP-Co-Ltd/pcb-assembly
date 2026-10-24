"""CameraModel（カメラの写像）の仕様テスト.

カメラ mm 空間は原点が画像中心で、+x 右、+y 下。

- pixel_offset_to_camera(d) = d / ppm（画像中心からの pixel のずれ）
- observed_to_machine_offset(a)(o) = a − R(o)（位置補正の (A4) アンカー）
- machine_to_camera(s)(p) = R(s − p)（銅箔の投影）

2 つは互いの逆ではない。往復すると o が R² で回る（統一は付録 X1）。
"""

from datetime import datetime
from pathlib import Path

import pytest

from pcbasm.geometry import Point2d, Rotation, Shift
from pcbasm.vision import CameraCalibration, CameraModel
from tests.support.camera_model import make_camera_model

CALIBRATED_AT = datetime(2026, 8, 20, 12, 0, 0)


def _camera_model(rotation: Rotation | None = None) -> CameraModel:
    return make_camera_model(10.0, (320, 200), rotation=rotation)


def _assert_close(actual: Point2d, expected: Point2d) -> None:
    assert actual.x == pytest.approx(expected.x, abs=1e-9)
    assert actual.y == pytest.approx(expected.y, abs=1e-9)


class TestCameraModel:
    def test_load_reads_the_calibration_with_no_rotation(self, tmp_path: Path):
        path = tmp_path / "calibration.json"
        CameraCalibration(
            pixel_per_mm=28.5,
            square_size_mm=1.5,
            mean_distance_px=42.75,
            std_distance_px=0.1,
            resolution=(1280, 800),
            crop_size=(600, 600),
            calibrated_at=CALIBRATED_AT,
            z_position=12.5,
        ).save(path)

        camera_model = CameraModel.load(path)

        assert (
            camera_model.pixel_per_mm,
            camera_model.image_size,
            camera_model.focus_z,
        ) == (
            28.5,
            (1280, 800),
            12.5,
        )
        assert camera_model.calibrated_at == CALIBRATED_AT
        _assert_close(camera_model.rotation.apply(Point2d(1.0, 2.0)), Point2d(1.0, 2.0))

    def test_pixel_offset_maps_to_camera_mm_with_the_same_axes(self):
        camera_model = _camera_model(Rotation(90.0))

        mm = camera_model.pixel_offset_to_camera(Point2d(12.0, -8.0))

        _assert_close(mm, Point2d(1.2, -0.8))

    def test_observed_offset_maps_to_anchor_minus_rotated_offset(self):
        """符号ピン: R=Rotation(90)、o=(1,0) → a − (0,1)."""
        camera_model = _camera_model(Rotation(90.0))

        machine = camera_model.observed_to_machine_offset(Point2d(10.0, 20.0))

        _assert_close(machine.apply(Point2d(1.0, 0.0)), Point2d(10.0, 19.0))

    def test_machine_point_maps_to_rotated_stage_minus_point(self):
        """符号ピン: R=Rotation(90)、s − p=(−2, 0.5) → (−0.5, −2)."""
        camera_model = _camera_model(Rotation(90.0))

        mm = camera_model.machine_to_camera(Point2d(1.0, 2.0)).apply(Point2d(3.0, 1.5))

        _assert_close(mm, Point2d(-0.5, -2.0))

    @pytest.mark.parametrize("degrees", [0.0, 5.0])
    def test_round_trip_turns_the_offset_by_r_squared(self, degrees: float):
        """2 つの向きは互いの逆ではない。回転 0 のときだけ往復で元に戻る."""
        camera_model = _camera_model(Rotation(degrees))
        stage = Point2d(30.0, 40.0)
        observed = Point2d(0.8, -0.3)

        machine = camera_model.observed_to_machine_offset(stage).apply(observed)
        back = camera_model.machine_to_camera(stage).apply(machine)

        _assert_close(back, Rotation(2 * degrees).apply(observed))

    def test_machine_point_maps_to_pixel_around_the_image_center(self):
        """Pixel = 画像中心 (160, 100) + ppm * R(s − p)."""
        camera_model = _camera_model(Rotation(90.0))

        pixel = camera_model.machine_to_pixel(Point2d(1.0, 2.0)).apply(
            Point2d(3.0, 1.5)
        )

        _assert_close(pixel, Point2d(155.0, 80.0))

    def test_board_to_pixel_composes_the_board_transform(self):
        camera_model = _camera_model(Rotation(5.0))
        board_transform = Shift(2.0, 1.0)
        stage = Point2d(4.0, 3.0)

        affine = camera_model.board_to_pixel(board_transform, stage)

        for point in (Point2d(0.0, 0.0), Point2d(1.0, 0.5), Point2d(-2.0, 3.0)):
            _assert_close(
                affine.apply(point),
                camera_model.machine_to_pixel(stage).apply(
                    board_transform.apply(point)
                ),
            )

    def test_image_size_error_is_none_when_the_size_matches(self):
        assert _camera_model().image_size_error((320, 200)) is None

    def test_image_size_error_names_both_sizes(self):
        assert (
            _camera_model().image_size_error((640, 480))
            == "カメラ 640x480 が校正 320x200 と違います"
        )

    def test_centered_roi_is_a_square_at_the_image_center(self):
        assert _camera_model().centered_roi(100) == (110, 50, 210, 150)

    @pytest.mark.parametrize("size_px", [0, 201])
    def test_centered_roi_rejects_sizes_outside_the_image(self, size_px: int):
        with pytest.raises(ValueError, match="size_px"):
            _camera_model().centered_roi(size_px)
