"""銅箔領域検出: エッジ検出で画像から面状の銅箔領域を抽出."""

import attrs
import cv2
import numpy as np

from pcbasm.geometry import Point2d

from .image import Image, ImageArray


@attrs.frozen
class DetectedCopper:
    """検出された銅箔領域.

    座標系は入力画像の左上原点・pixel単位。
    """

    contour: ImageArray = attrs.field(eq=False)  # cv2輪郭点列 (N, 1, 2)
    bbox: tuple[int, int, int, int]  # x, y, w, h (pixel)
    area_px: float  # 輪郭面積 (pixel^2)
    center: Point2d  # 重心 (pixel)


class CopperDetector:
    """エッジ検出で画像から銅箔領域を抽出する."""

    def __init__(
        self,
        canny_low: float = 50.0,
        canny_high: float = 150.0,
        blur_ksize: int = 5,
        close_ksize: int = 5,
        min_area_px: float = 500.0,
    ) -> None:
        """CopperDetectorを初期化.

        Args:
            canny_low: Cannyエッジ検出の下側閾値
            canny_high: Cannyエッジ検出の上側閾値
            blur_ksize: GaussianBlurのカーネルサイズ (奇数)
            close_ksize: モルフォロジーclosingのカーネルサイズ
            min_area_px: この面積 (pixel^2) 未満の輪郭はノイズとして除外
        """
        self._canny_low = canny_low
        self._canny_high = canny_high
        self._blur_ksize = blur_ksize
        self._close_ksize = close_ksize
        self._min_area_px = min_area_px

    def compute_mask(self, image: Image) -> ImageArray:
        """前処理済みの2値マスク (uint8, 0/255) を返す.

        グレースケール → GaussianBlur → Canny → MORPH_CLOSE の結果。
        デバッグ表示やハイパーパラメータのチューニングに使う。

        Args:
            image: 入力画像

        Returns:
            入力と同サイズの2値マスク
        """
        gray = cv2.cvtColor(image.numpy(), cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (self._blur_ksize, self._blur_ksize), 0)
        edges = cv2.Canny(blurred, self._canny_low, self._canny_high)
        kernel = np.ones((self._close_ksize, self._close_ksize), np.uint8)
        return cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

    def detect(self, image: Image) -> list[DetectedCopper]:
        """銅箔領域を検出する.

        Args:
            image: 入力画像

        Returns:
            検出された領域の面積降順リスト (0件なら空リスト)
        """
        return self._regions_from_mask(self.compute_mask(image))

    def _regions_from_mask(self, mask: ImageArray) -> list[DetectedCopper]:
        """2値マスクから輪郭を抽出し、領域リストに変換する."""
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        result = []
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < self._min_area_px:
                continue

            moments = cv2.moments(contour)
            if moments["m00"] == 0:
                continue
            center = Point2d(
                x=float(moments["m10"] / moments["m00"]),
                y=float(moments["m01"] / moments["m00"]),
            )

            x, y, w, h = cv2.boundingRect(contour)
            result.append(
                DetectedCopper(
                    contour=contour,
                    bbox=(int(x), int(y), int(w), int(h)),
                    area_px=area,
                    center=center,
                )
            )

        return sorted(result, key=lambda r: r.area_px, reverse=True)
