"""テスト用の CameraModel を組む."""

from datetime import UTC, datetime

from pcbasm.geometry import Identity, Transform
from pcbasm.vision import CameraModel
from pcbasm.vision.image import ImageSize

CALIBRATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def make_camera_model(
    pixel_per_mm: float,
    image_size: ImageSize,
    *,
    focus_z: float = 10.0,
    rotation: Transform | None = None,
) -> CameraModel:
    """校正の値（と測ったカメラの回転）から CameraModel を組む."""
    return CameraModel(
        pixel_per_mm=pixel_per_mm,
        image_size=image_size,
        focus_z=focus_z,
        calibrated_at=CALIBRATED_AT,
        rotation=rotation or Identity(),
    )
