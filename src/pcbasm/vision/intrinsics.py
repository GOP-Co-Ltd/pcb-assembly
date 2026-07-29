"""カメラ内部パラメータとレンズ歪み補正の実行時経路.

校正ソルバ（``cv2.calibrateCamera`` / ``findChessboardCorners``）は
``calibration.py`` 側に置く。``hal/framehub.py`` はこのモジュールを直接
（``from pcbasm.vision.intrinsics import Undistorter``）import し、実行時経路が
オフライン校正の API に依存しないことをコードで示す。パッケージの ``__init__`` が
両方を re-export するのでモジュールのロード自体は避けられない。分離の目的は依存の
向きを明示することであって遅延ロードではない。
"""

from __future__ import annotations

import math
from typing import Self

import attrs
import cv2
import numpy as np

from pcbasm.vision.image import Image, ImageArray


@attrs.frozen
class CameraIntrinsics:
    """OpenCV ピンホールモデルの内部パラメータ（歪みは補正前フレームの座標系）.

    ndarray は attrs frozen に持たせない（eq が壊れ cattrs で永続化もできない）。
    ネストした tuple で保持し、必要時に ``matrix()`` / ``coefficients()`` で変換する。

    Attributes:
        camera_matrix: 3x3 カメラ行列（行優先）
        distortion: 歪み係数 (k1, k2, p1, p2, k3)
        resolution: 推定時のフレームサイズ (width, height)
    """

    camera_matrix: tuple[tuple[float, float, float], ...]
    distortion: tuple[float, float, float, float, float]
    resolution: tuple[int, int]

    def __attrs_post_init__(self) -> None:
        if len(self.camera_matrix) != 3 or any(
            len(row) != 3 for row in self.camera_matrix
        ):
            raise ValueError(
                f"camera_matrixは3x3である必要があります: {self.camera_matrix}"
            )
        # 外部 JSON 境界の検証。json.loads は NaN / Infinity リテラルを既定で受理し、
        # calibrateCamera も発散すると NaN を返す。非有限値を通すと補正マップが
        # 全画素で壊れるのに残差比較（NaN > limit）が False になって黙って通る
        if any(
            not math.isfinite(value)
            for value in (
                *(v for row in self.camera_matrix for v in row),
                *self.distortion,
            )
        ):
            raise ValueError(
                "camera_matrix / distortion に非有限値を含められません: "
                f"{self.camera_matrix} {self.distortion}"
            )
        width, height = self.resolution
        if width <= 0 or height <= 0:
            raise ValueError(
                f"resolutionは正の値である必要があります: {self.resolution}"
            )

    def matrix(self) -> ImageArray:
        """カメラ行列を 3x3 float64 配列で返す."""
        return np.array(self.camera_matrix, dtype=np.float64)

    def coefficients(self) -> ImageArray:
        """歪み係数を (5,) float64 配列で返す."""
        return np.array(self.distortion, dtype=np.float64)

    @classmethod
    def of(
        cls,
        camera_matrix: ImageArray,
        distortion: ImageArray,
        resolution: tuple[int, int],
    ) -> Self:
        """``cv2.calibrateCamera`` の出力から生成する."""
        matrix = np.asarray(camera_matrix, dtype=np.float64).reshape(3, 3)
        k1, k2, p1, p2, k3 = (
            float(value)
            for value in np.asarray(distortion, dtype=np.float64).ravel()[:5]
        )
        return cls(
            camera_matrix=tuple(
                (float(row[0]), float(row[1]), float(row[2])) for row in matrix
            ),
            distortion=(k1, k2, p1, p2, k3),
            resolution=resolution,
        )


class Undistorter:
    """歪みマップを事前計算してフレームを補正する.

    ``cv2.undistort()`` は呼ぶたびにマップを作り直すため使わない
    （Pi5 実測 37ms/frame に対し、事前計算マップ + ``remap`` は 8.7ms/frame）。

    新カメラ行列には元のカメラ行列をそのまま使う（``getOptimalNewCameraMatrix``
    は使わない）。これにより画像サイズ・主点・中心付近のスケールが保存され、
    既存の px↔mm 変換点が無改造で正しくなる。
    """

    def __init__(self, intrinsics: CameraIntrinsics) -> None:
        """歪みマップを構築する.

        Args:
            intrinsics: 内部パラメータ。``resolution`` が補正対象のフレームサイズ
        """
        self._intrinsics = intrinsics
        matrix = intrinsics.matrix()
        self._map_x, self._map_y = cv2.initUndistortRectifyMap(
            matrix,
            intrinsics.coefficients(),
            np.eye(3, dtype=np.float64),  # R = 単位行列（回転なし）
            matrix,
            intrinsics.resolution,
            cv2.CV_16SC2,
        )

    @property
    def resolution(self) -> tuple[int, int]:
        """補正対象のフレームサイズ (width, height)."""
        return self._intrinsics.resolution

    def apply(self, image: Image) -> Image:
        """フレームを歪み補正して返す.

        Raises:
            ValueError: 画像サイズが ``resolution`` と一致しない場合
        """
        if image.size != self.resolution:
            raise ValueError(
                f"画像サイズ{image.size}が歪みマップ{self.resolution}と一致しません"
            )
        return Image(
            cv2.remap(image.numpy(), self._map_x, self._map_y, cv2.INTER_LINEAR)
        )

    def apply_points(self, points: ImageArray) -> ImageArray:
        """(N,2) の画素座標を歪み補正して (N,2) で返す.

        補正後の座標は元のカメラ行列を通す（``apply`` の画素座標系と一致させる）。
        """
        matrix = self._intrinsics.matrix()
        source = np.asarray(points, dtype=np.float64).reshape(-1, 1, 2)
        undistorted = cv2.undistortPoints(
            source, matrix, self._intrinsics.coefficients(), P=matrix
        )
        return np.asarray(undistorted, dtype=np.float64).reshape(-1, 2)
