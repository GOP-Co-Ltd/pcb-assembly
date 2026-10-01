"""画像から銅箔の境界エッジを抽出する銅箔エッジ検出."""

import attrs
import cv2

from .image import Image, ImageArray


@attrs.frozen
class CopperEdgeDetection:
    """銅箔エッジ検出の前処理済みグレースケール画像と 2 値エッジマスク."""

    processed: Image
    edges: ImageArray = attrs.field(eq=False)


class CopperEdgeDetector:
    """エッジ検出で画像から銅箔の境界を抽出する.

    銅箔境界は撮像範囲からはみ出すなどして閉じているとは限らない。そのため 領域（閉輪郭）は抽出せず、2 値エッジマスクをそのまま返す。mm
    変換や 設計データとのマッチングは呼び出し側（posctrl 層）で行う。
    """

    def __init__(
        self,
        canny_low: float = 50.0,
        canny_high: float = 150.0,
        blur_ksize: int = 5,
    ) -> None:
        """CopperEdgeDetector を初期化.

        Args:
            canny_low: Canny エッジ検出の下側閾値
            canny_high: Canny エッジ検出の上側閾値
            blur_ksize: GaussianBlur のカーネルサイズ (奇数)
        """
        if (
            isinstance(blur_ksize, bool)
            or not isinstance(blur_ksize, int)
            or blur_ksize <= 0
            or blur_ksize % 2 == 0
        ):
            raise ValueError(
                f"blur_ksizeは正の奇数である必要があります: {blur_ksize!r}"
            )
        self._canny_low = canny_low
        self._canny_high = canny_high
        self._blur_ksize = blur_ksize

    def detect(self, image: Image) -> CopperEdgeDetection:
        """前処理画像と 2 値エッジマスク (uint8, 0/255) を返す."""
        gray = cv2.cvtColor(image.numpy(), cv2.COLOR_BGR2GRAY)
        kernel = (self._blur_ksize, self._blur_ksize)
        processed = cv2.GaussianBlur(gray, kernel, 0)
        edges = cv2.Canny(processed, self._canny_low, self._canny_high)
        return CopperEdgeDetection(processed=Image(processed), edges=edges)

    def detect_edges(self, image: Image) -> ImageArray:
        """2 値エッジマスク (uint8, 0/255) を返す.

        detect() と同じ前処理を適用した結果を返す。

        Args:
            image: 入力画像

        Returns:
            入力と同サイズの 2 値エッジマスク
        """
        return self.detect(image).edges
