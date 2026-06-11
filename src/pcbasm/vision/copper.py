"""銅箔エッジ検出: 画像から銅箔の境界エッジを抽出."""

import cv2

from .image import Image, ImageArray


class CopperEdgeDetector:
    """エッジ検出で画像から銅箔の境界を抽出する.

    銅箔境界は撮像範囲からはみ出すなどして閉じているとは限らないため、 領域（閉輪郭）の抽出はせず、2値エッジマスクをそのまま返す。 mm
    変換や設計データとのマッチングは呼び出し側（posctrl 層）の責務。
    """

    def __init__(
        self,
        canny_low: float = 50.0,
        canny_high: float = 150.0,
        blur_ksize: int = 5,
    ) -> None:
        """CopperEdgeDetectorを初期化.

        Args:
            canny_low: Cannyエッジ検出の下側閾値
            canny_high: Cannyエッジ検出の上側閾値
            blur_ksize: GaussianBlurのカーネルサイズ (奇数)
        """
        self._canny_low = canny_low
        self._canny_high = canny_high
        self._blur_ksize = blur_ksize

    def detect_edges(self, image: Image) -> ImageArray:
        """2値エッジマスク (uint8, 0/255) を返す.

        グレースケール → GaussianBlur → Canny の結果。

        Args:
            image: 入力画像

        Returns:
            入力と同サイズの2値エッジマスク
        """
        gray = cv2.cvtColor(image.numpy(), cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (self._blur_ksize, self._blur_ksize), 0)
        return cv2.Canny(blurred, self._canny_low, self._canny_high)
