"""カメラキャリブレーション: チェッカーボードからpixel/mm比率を計算."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Self, cast

import attrs
import cv2
import numpy as np
from cattrs.preconf.json import make_converter

from pcb_assembly.types import Image

# Path の変換をサポートするコンバーター
_converter = make_converter()
_converter.register_unstructure_hook(Path, str)
_converter.register_structure_hook(Path, lambda v, _: Path(v))


@attrs.frozen
class CalibrationResult:
    """キャリブレーション結果."""

    pixel_per_mm: float  # pixel/mm比率
    square_size_mm: float  # チェッカーボードの1マスのサイズ (mm)
    mean_distance_px: float  # 1マスの平均距離 (pixel)
    std_distance_px: float  # 1マスの距離の標準偏差 (pixel)
    resolution: tuple[int, int]  # カメラ解像度 (width, height)
    crop_size: tuple[int, int]  # 関心領域サイズ (width, height)
    calibrated_at: datetime  # キャリブレーション日時

    @property
    def mm_per_pixel(self) -> float:
        """mm/pixel比率."""
        return 1.0 / self.pixel_per_mm

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


class CheckerboardCalibrator:
    """チェッカーボードを使ったpixel/mm比率キャリブレーション."""

    def __init__(
        self,
        square_size_mm: float,
        crop_size: tuple[int, int],
        pattern_rows_range: tuple[int, int] = (4, 12),
        pattern_cols_range: tuple[int, int] = (4, 12),
    ) -> None:
        self._square_size_mm = square_size_mm
        self._crop_size = crop_size
        self._pattern_rows_range = pattern_rows_range
        self._pattern_cols_range = pattern_cols_range

    def calibrate(self, image: Image) -> tuple[CalibrationResult, Image] | None:
        """画像からキャリブレーションを実行.

        Returns:
            (キャリブレーション結果, コーナー描画済み画像) または検出失敗時はNone
        """
        resolution = (image.shape[1], image.shape[0])
        cropped = self._crop_center(image)

        detection = self._detect_checkerboard(cropped)
        if detection is None:
            return None

        corners, pattern_size = detection
        pixel_per_mm, mean_dist, std_dist = self._calculate_pixel_per_mm(
            corners, pattern_size
        )

        result = CalibrationResult(
            pixel_per_mm=pixel_per_mm,
            square_size_mm=self._square_size_mm,
            mean_distance_px=mean_dist,
            std_distance_px=std_dist,
            resolution=resolution,
            crop_size=self._crop_size,
            calibrated_at=datetime.now(),
        )

        vis = cropped.copy()
        cv2.drawChessboardCorners(vis, pattern_size, corners, True)

        return result, vis

    def _crop_center(self, image: Image) -> Image:
        """画像の中心をクロップ."""
        h, w = image.shape[:2]
        cx, cy = w // 2, h // 2
        half_w, half_h = self._crop_size[0] // 2, self._crop_size[1] // 2
        return cast(
            Image, image[cy - half_h : cy + half_h, cx - half_w : cx + half_w].copy()
        )

    def _detect_checkerboard(
        self, image: Image
    ) -> tuple[np.ndarray, tuple[int, int]] | None:
        """チェッカーボードのコーナーを検出."""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE

        rows_min, rows_max = self._pattern_rows_range
        cols_min, cols_max = self._pattern_cols_range

        for rows in range(rows_min, rows_max):
            for cols in range(cols_min, cols_max):
                ret, corners = cv2.findChessboardCorners(
                    gray, (cols, rows), flags=flags
                )
                if ret and corners is not None:
                    # サブピクセル精度で補正
                    criteria = (
                        cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER,
                        30,
                        0.001,
                    )
                    corners = cv2.cornerSubPix(
                        gray, corners, (11, 11), (-1, -1), criteria
                    )
                    return corners, (cols, rows)

        return None

    def _calculate_pixel_per_mm(
        self, corners: np.ndarray, pattern_size: tuple[int, int]
    ) -> tuple[float, float, float]:
        """コーナー間距離からpixel/mm比率を計算."""
        corners_2d = corners.reshape(-1, 2)
        cols, rows = pattern_size

        distances: list[float] = []

        # 水平方向の距離
        for r in range(rows):
            for c in range(cols - 1):
                idx1 = r * cols + c
                idx2 = r * cols + c + 1
                d = float(np.linalg.norm(corners_2d[idx1] - corners_2d[idx2]))
                distances.append(d)

        # 垂直方向の距離
        for r in range(rows - 1):
            for c in range(cols):
                idx1 = r * cols + c
                idx2 = (r + 1) * cols + c
                d = float(np.linalg.norm(corners_2d[idx1] - corners_2d[idx2]))
                distances.append(d)

        mean_distance = float(np.mean(distances))
        std_distance = float(np.std(distances))
        pixel_per_mm = mean_distance / self._square_size_mm

        return pixel_per_mm, mean_distance, std_distance
