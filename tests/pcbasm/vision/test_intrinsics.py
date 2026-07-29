"""`pcbasm.vision.intrinsics` の仕様テスト.

計画書「カメラキャリブレーションのレンズ歪み補正対応」§1 が契約:

- `CameraIntrinsics` は ndarray を持たず、ネストした tuple で保持して JSON へ
  永続化できる（frozen attrs の eq / cattrs を壊さない）
- `Undistorter` は新カメラ行列に元のカメラ行列をそのまま使う
  （`getOptimalNewCameraMatrix` は使わない）。これにより画像サイズ・主点・
  中心付近のスケールが保存され、既存の px↔mm 変換点が無改造で正しくなる
- `apply` はサイズ不一致を ValueError で弾く
- 黒縁は crop 600 に侵入しない（Pi5 実測を回帰テスト化した設計根拠）

3rd-party 表面（OpenCV / cattrs）はモックせず実物を通す。
"""

import json

import numpy as np
import pytest
from cattrs.preconf.json import make_converter

from pcbasm.vision import CameraIntrinsics, Image, Undistorter

RESOLUTION = (1280, 720)
CROP_SIDE = 600


def _intrinsics(
    distortion: tuple[float, float, float, float, float],
    resolution: tuple[int, int] = RESOLUTION,
) -> CameraIntrinsics:
    """名目焦点距離 = フレーム幅の内部パラメータを組み立てる."""
    width, height = resolution
    return CameraIntrinsics(
        camera_matrix=(
            (float(width), 0.0, width / 2.0),
            (0.0, float(width), height / 2.0),
            (0.0, 0.0, 1.0),
        ),
        distortion=distortion,
        resolution=resolution,
    )


def _white(resolution: tuple[int, int] = RESOLUTION) -> Image:
    width, height = resolution
    return Image(np.full((height, width, 3), 255, dtype=np.uint8))


class TestCameraIntrinsics:
    """内部パラメータの検証・配列変換・永続化."""

    def test_matrix_and_coefficients_convert_to_float64_arrays(self):
        intrinsics = _intrinsics((-0.12, 0.03, 0.0, 0.0, 0.0))

        matrix = intrinsics.matrix()
        coefficients = intrinsics.coefficients()

        assert matrix.shape == (3, 3)
        assert matrix.dtype == np.float64
        assert coefficients.shape == (5,)
        assert coefficients.dtype == np.float64
        assert matrix[0][0] == pytest.approx(1280.0)
        assert coefficients[0] == pytest.approx(-0.12)

    def test_of_accepts_calibrate_camera_output_shapes(self):
        # cv2.calibrateCamera は distortion を (1,5) で返す
        matrix = np.array(
            [[1280.0, 0.0, 640.0], [0.0, 1280.0, 360.0], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )
        distortion = np.array([[-0.12, 0.03, 0.0, 0.0, 0.0]], dtype=np.float64)

        intrinsics = CameraIntrinsics.of(matrix, distortion, RESOLUTION)

        assert intrinsics.camera_matrix == (
            (1280.0, 0.0, 640.0),
            (0.0, 1280.0, 360.0),
            (0.0, 0.0, 1.0),
        )
        assert intrinsics.distortion == (-0.12, 0.03, 0.0, 0.0, 0.0)
        assert intrinsics.resolution == RESOLUTION

    def test_rejects_non_3x3_camera_matrix(self):
        with pytest.raises(ValueError) as exc:
            CameraIntrinsics(
                camera_matrix=((1280.0, 0.0, 640.0), (0.0, 1280.0, 360.0)),
                distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
                resolution=RESOLUTION,
            )

        assert "camera_matrix" in str(exc.value)

    def test_rejects_non_positive_resolution(self):
        with pytest.raises(ValueError) as exc:
            CameraIntrinsics(
                camera_matrix=(
                    (1280.0, 0.0, 640.0),
                    (0.0, 1280.0, 360.0),
                    (0.0, 0.0, 1.0),
                ),
                distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
                resolution=(1280, 0),
            )

        assert "resolution" in str(exc.value)

    @pytest.mark.parametrize("value", [float("nan"), float("inf")])
    def test_rejects_non_finite_camera_matrix(self, value: float):
        # calibrateCamera が発散すると NaN の K を返す。通すと全フレームが壊れるのに
        # 残差比較（NaN > limit）が False になり品質ゲートを素通りする
        with pytest.raises(ValueError) as exc:
            CameraIntrinsics(
                camera_matrix=(
                    (1280.0, 0.0, value),
                    (0.0, 1280.0, 360.0),
                    (0.0, 0.0, 1.0),
                ),
                distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
                resolution=RESOLUTION,
            )

        assert "非有限" in str(exc.value)

    @pytest.mark.parametrize("literal", ["NaN", "Infinity"])
    def test_rejects_non_finite_distortion_from_json(self, literal: str):
        # json.loads は NaN / Infinity リテラルを既定で受理するので、外部 JSON 境界
        # であるここで弾かないと歪みマップが警告なしに壊れる
        text = (
            '{"camera_matrix": [[1280.0, 0.0, 640.0], [0.0, 1280.0, 360.0],'
            " [0.0, 0.0, 1.0]],"
            f' "distortion": [{literal}, 0.0, 0.0, 0.0, 0.0],'
            ' "resolution": [1280, 720]}'
        )

        # cattrs は ValueError を ExceptionGroup 派生の型で包む（型名はピンしない）
        with pytest.raises(Exception):
            make_converter().structure(json.loads(text), CameraIntrinsics)

    def test_survives_json_roundtrip_through_cattrs(self):
        # ndarray を持たないので JSON 化でき、frozen attrs の eq も成立する
        intrinsics = _intrinsics((-0.12, 0.03, 0.0, 0.0, 0.0))
        converter = make_converter()

        data = converter.unstructure(intrinsics)
        restored = converter.structure(json.loads(json.dumps(data)), CameraIntrinsics)

        assert restored == intrinsics


class TestUndistorter:
    """歪みマップの構築と適用."""

    def test_resolution_comes_from_intrinsics(self):
        undistorter = Undistorter(_intrinsics((-0.12, 0.03, 0.0, 0.0, 0.0)))

        assert undistorter.resolution == RESOLUTION

    def test_apply_preserves_image_size(self):
        undistorter = Undistorter(_intrinsics((-0.12, 0.03, 0.0, 0.0, 0.0)))

        corrected = undistorter.apply(_white())

        assert corrected.size == RESOLUTION

    def test_apply_rejects_mismatched_image_size(self):
        undistorter = Undistorter(_intrinsics((-0.12, 0.03, 0.0, 0.0, 0.0)))

        with pytest.raises(ValueError) as exc:
            undistorter.apply(_white((640, 480)))

        assert "一致しません" in str(exc.value)

    @pytest.mark.parametrize("k1", [-0.25, -0.12, 0.0, 0.15, 0.3])
    def test_principal_point_does_not_move(self, k1: float):
        # 新カメラ行列 = 元のカメラ行列にしているので主点は不動点
        undistorter = Undistorter(_intrinsics((k1, 0.0, 0.0, 0.0, 0.0)))
        principal = np.array([[640.0, 360.0]])

        moved = undistorter.apply_points(principal)

        assert moved.shape == (1, 2)
        assert moved[0][0] == pytest.approx(640.0, abs=1e-6)
        assert moved[0][1] == pytest.approx(360.0, abs=1e-6)

    def test_zero_distortion_is_identity_on_points(self):
        undistorter = Undistorter(_intrinsics((0.0, 0.0, 0.0, 0.0, 0.0)))
        points = np.array([[0.0, 0.0], [1279.0, 719.0], [400.0, 300.0]])

        corrected = undistorter.apply_points(points)

        assert corrected == pytest.approx(points, abs=1e-6)

    @pytest.mark.parametrize("k1", [-0.25, -0.12])
    def test_barrel_distortion_leaves_no_black_border_anywhere(self, k1: float):
        # Pi5 実測の回帰: バレル（k1<0）では全画面で黒画素が出ない
        undistorter = Undistorter(_intrinsics((k1, 0.0, 0.0, 0.0, 0.0)))

        corrected = undistorter.apply(_white())

        assert int((corrected.numpy() == 0).sum()) == 0

    @pytest.mark.parametrize("k1", [0.15, 0.3])
    def test_pincushion_black_border_does_not_reach_crop_600(self, k1: float):
        # Pi5 実測の回帰: ピンクッション k1=+0.3 でも中央 600x600 への侵入はゼロ。
        # crop 600 に戻せるという設計判断の根拠
        undistorter = Undistorter(_intrinsics((k1, 0.0, 0.0, 0.0, 0.0)))

        corrected = undistorter.apply(_white())

        center = corrected.crop_center((CROP_SIDE, CROP_SIDE))
        assert int((center.numpy() == 0).sum()) == 0
