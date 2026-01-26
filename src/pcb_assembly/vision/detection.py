"""画像検出: 円などの図形を検出し、位置ズレを計算."""

import statistics
from collections.abc import Iterable

import attrs
import cv2

from pcb_assembly.geometry import Point2d

from .image import Image


@attrs.frozen
class Offset:
    """画像中心からのズレ."""

    px: Point2d  # pixel単位
    pixel_per_mm: float

    @property
    def mm(self) -> Point2d:
        """mm単位のオフセット."""
        return Point2d(
            x=self.px.x / self.pixel_per_mm,
            y=self.px.y / self.pixel_per_mm,
        )


@attrs.frozen
class DetectedCircle:
    """検出された円."""

    center: Point2d  # 円の中心座標 (pixel)
    radius: float  # 円の半径 (pixel)
    offset: Offset  # 画像中心からのズレ


@attrs.frozen
class OffsetStatistics:
    """複数検出結果の統計情報."""

    mean: Point2d  # 平均オフセット (pixel)
    std: Point2d  # 標準偏差 (pixel)
    pixel_per_mm: float
    sample_count: int  # 有効サンプル数

    @property
    def mean_mm(self) -> Point2d:
        """平均オフセット (mm単位)."""
        return Point2d(
            x=self.mean.x / self.pixel_per_mm,
            y=self.mean.y / self.pixel_per_mm,
        )

    @property
    def std_mm(self) -> Point2d:
        """標準偏差 (mm単位)."""
        return Point2d(
            x=self.std.x / self.pixel_per_mm,
            y=self.std.y / self.pixel_per_mm,
        )


class CircleDetector:
    """画像から円を検出し、最も中心に近い円の位置ズレを計算."""

    def __init__(
        self,
        pixel_per_mm: float,
        target_diameter_mm: float = 3.0,
        diameter_tolerance_mm: float = 1.0,
        crop_size: tuple[int, int] | None = None,
    ) -> None:
        """CircleDetectorを初期化.

        Args:
            pixel_per_mm: pixel/mm比率
            target_diameter_mm: ターゲットの円の直径 (mm)
            diameter_tolerance_mm: 直径の許容誤差 (mm)
            crop_size: 関心領域サイズ (width, height)、Noneの場合は画像全体
        """
        self._pixel_per_mm = pixel_per_mm
        self._target_diameter_mm = target_diameter_mm
        self._diameter_tolerance_mm = diameter_tolerance_mm
        self._crop_size = crop_size

    def detect_nearest_center(self, image: Image) -> DetectedCircle | None:
        """画像から円を検出し、最も中心に近い円を返す.

        Args:
            image: 入力画像

        Returns:
            DetectedCircle または検出失敗時はNone
        """
        circles = self.detect_circles(image)
        if len(circles) == 0:
            return None

        # ターゲットサイズに近い円をフィルタリング
        target_circles = self._filter_by_size(circles)
        if len(target_circles) == 0:
            return None

        # 最も中心に近い円を選択
        return min(target_circles, key=lambda c: c.offset.px.norm)

    def detect_circles(self, image: Image) -> list[DetectedCircle]:
        """Hough変換で円を検出.

        Args:
            image: 入力画像

        Returns:
            検出された円のリスト
        """
        # 関心領域を切り出し
        if self._crop_size is not None:
            cropped = image.crop_center(self._crop_size)
        else:
            cropped = image

        image_center = (cropped.width / 2, cropped.height / 2)

        gray = cv2.cvtColor(cropped.numpy(), cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (9, 9), 2)

        # ターゲットサイズに基づいて検出パラメータを設定
        min_radius_mm = (self._target_diameter_mm - self._diameter_tolerance_mm) / 2
        max_radius_mm = (self._target_diameter_mm + self._diameter_tolerance_mm) / 2
        min_radius_px = max(1, int(min_radius_mm * self._pixel_per_mm))
        max_radius_px = int(max_radius_mm * self._pixel_per_mm)

        circles = cv2.HoughCircles(
            gray,
            cv2.HOUGH_GRADIENT,
            dp=1,
            minDist=min_radius_px * 2,
            param1=50,
            param2=30,
            minRadius=min_radius_px,
            maxRadius=max_radius_px,
        )

        if circles is None:
            return []

        result = []
        for c in circles[0]:
            center = Point2d(x=float(c[0]), y=float(c[1]))
            radius = float(c[2])
            offset_px = Point2d(
                x=center.x - image_center[0],
                y=center.y - image_center[1],
            )
            offset = Offset(px=offset_px, pixel_per_mm=self._pixel_per_mm)
            result.append(
                DetectedCircle(
                    center=center,
                    radius=radius,
                    offset=offset,
                )
            )

        return result

    def _filter_by_size(self, circles: list[DetectedCircle]) -> list[DetectedCircle]:
        """ターゲットサイズに近い円をフィルタリング."""
        target_radius_px = (self._target_diameter_mm / 2) * self._pixel_per_mm
        tolerance_px = (self._diameter_tolerance_mm / 2) * self._pixel_per_mm

        return [c for c in circles if abs(c.radius - target_radius_px) <= tolerance_px]

    def detect_with_statistics(
        self, images: Iterable[Image]
    ) -> OffsetStatistics | None:
        """複数画像から円を検出し、オフセットの統計を返す.

        Args:
            images: 入力画像のイテラブル

        Returns:
            OffsetStatistics または有効な検出がない場合はNone
        """
        offsets_x: list[float] = []
        offsets_y: list[float] = []

        for image in images:
            detected = self.detect_nearest_center(image)
            if detected is not None:
                offsets_x.append(detected.offset.px.x)
                offsets_y.append(detected.offset.px.y)

        if not offsets_x:
            return None

        return OffsetStatistics(
            mean=Point2d(
                x=statistics.mean(offsets_x),
                y=statistics.mean(offsets_y),
            ),
            std=Point2d(
                x=statistics.pstdev(offsets_x),
                y=statistics.pstdev(offsets_y),
            ),
            pixel_per_mm=self._pixel_per_mm,
            sample_count=len(offsets_x),
        )
