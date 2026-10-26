"""基板の 4 隅にある基準点マーカーの検出器."""

from .camera_model import CameraModel
from .detection import CircleDetector


def reference_marker_detector(
    camera_model: CameraModel,
    crop_size: tuple[int, int],
    *,
    target_diameter_mm: float,
    diameter_tolerance_mm: float,
) -> CircleDetector:
    """基準点マーカーの検出器を作る.

    基板の位置計測、基準点の教示、プレビューが同じ検出器で同じ円を見るよう、組み立てをここに集める。

    Args:
        camera_model: カメラの写像（pixel/mm を使う）
        crop_size: 検出に使う関心領域 (width, height)
        target_diameter_mm: マーカーの直径 (mm)
        diameter_tolerance_mm: 直径の許容誤差 (mm)
    """
    return CircleDetector(
        pixel_per_mm=camera_model.pixel_per_mm,
        target_diameter_mm=target_diameter_mm,
        diameter_tolerance_mm=diameter_tolerance_mm,
        crop_size=crop_size,
    )
