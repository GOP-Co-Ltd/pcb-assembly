"""暗い斑点（塗布痕）の 2 値化と連結成分.

塗布痕は板より暗いので、「背景より暗くなった量」を差分画像にしてから Otsu で
2 値化する。差分の作り方だけが用途で変わる。

- 塗布前後の 2 枚があるなら :func:`darkening`。板のヘアラインや照明ムラは塗布前後で
    共通なので引き算で消える
- 1 枚しかないなら :func:`background_darkening`。斑点より大きいカーネルの
    morphological closing で背景を推定し、照明ムラをそちらへ吸収させる

差分から先の段（分位点コントラストによる blank ガード → 下限付き Otsu → open →
連結成分）はどちらでも共通。blank ガードが要るのは、Otsu が「必ず何かを 2 値化する」
ので、これ単独では塗布していない箇所の背景ノイズを拾ってしまうため。
"""

from __future__ import annotations

import math

import attrs
import cv2
import numpy as np

from pcbasm.geometry import Point2d
from pcbasm.utils import is_finite_number
from pcbasm.vision.image import ImageArray

# open カーネルの上限 [px]。crop より大きい値は塗布痕ごと消すため意味を持たない一方、
# k×k の確保が MemoryError や数 GiB になる
MAX_OPEN_KERNEL_PX = 99

# 背景推定 closing のカーネルを想定最大直径の何倍にするか。斑点を覆いきれないと
# closing が斑点を消せず、差分が輪郭だけになる
_BACKGROUND_KERNEL_RATIO = 1.5


@attrs.frozen
class DotDetectionSpec:
    """円検出のハイパーパラメータ.

    Attributes:
        min_contrast: 塗布ありと判定する差分の下限（0-255）。分位点がこれ未満なら直径 0
        contrast_percentile: コントラストを測る分位点 [%]（外れ画素へ寄りすぎない値）
        threshold_floor_ratio: Otsu 閾値の下限を ``min_contrast`` の何倍にするか
        open_kernel_px: モルフォロジー open の正方カーネル [px]（0 で無効、正の奇数）
        min_area_px: 連結成分の面積下限 [px]（未満は検出なし）
    """

    min_contrast: float = 20.0
    contrast_percentile: float = 99.0
    threshold_floor_ratio: float = 0.5
    open_kernel_px: int = 3
    min_area_px: int = 4

    def validate(self) -> str | None:
        """設定値の型・符号・範囲を検証する（不正なら理由文）."""
        if not is_finite_number(self.min_contrast) or self.min_contrast < 0:
            return f"最小コントラストは0以上の有限値が必要です: {self.min_contrast!r}"
        if (
            not is_finite_number(self.contrast_percentile)
            or not 0 < self.contrast_percentile <= 100
        ):
            return (
                "コントラストの分位点は0より大きく100以下が必要です: "
                f"{self.contrast_percentile!r}"
            )
        if (
            not is_finite_number(self.threshold_floor_ratio)
            or self.threshold_floor_ratio < 0
        ):
            return (
                "Otsu閾値の下限比は0以上の有限値が必要です: "
                f"{self.threshold_floor_ratio!r}"
            )
        if type(self.open_kernel_px) is not int or self.open_kernel_px < 0:
            return f"openカーネルは0以上の整数が必要です: {self.open_kernel_px!r}"
        if self.open_kernel_px > 0 and self.open_kernel_px % 2 == 0:
            return f"openカーネルは0か正の奇数が必要です: {self.open_kernel_px!r}"
        if self.open_kernel_px > MAX_OPEN_KERNEL_PX:
            # カーネルは k×k バイトを確保するので、打ち間違いが MemoryError や
            # 数 GiB の確保になる。crop より大きい open は塗布痕を消すだけなので
            # 上限を置いても失うものが無い（点の直径は 53 px crop で 17〜26 px）
            return (
                f"openカーネルは{MAX_OPEN_KERNEL_PX}以下が必要です: "
                f"{self.open_kernel_px!r}"
            )
        if type(self.min_area_px) is not int or self.min_area_px < 1:
            # 0 を許すと面積 0 の成分が「検出できた」ことになり、「検出できた」と
            # 「直径が正」の対応が壊れる
            return f"最小面積は1以上の整数が必要です: {self.min_area_px!r}"
        return None


@attrs.frozen
class DarkSegmentation:
    """差分画像を 2 値化した結果.

    Attributes:
        mask: 2 値マスク（blank ガードで打ち切ったときは ``None``）
        contrast: 差分の ``contrast_percentile`` 分位点（0-255）
        threshold: 実際に使った 2 値化閾値（打ち切ったときは 0.0）
    """

    mask: ImageArray | None
    contrast: float
    threshold: float


@attrs.frozen
class DarkSpot:
    """2 値マスクの 1 連結成分.

    Attributes:
        area_px: 面積 [px]
        center: 重心 [px]
        perimeter_px: 外周長 [px]（成分だけを切り出して測った外側の輪郭長）
    """

    area_px: int
    center: Point2d
    perimeter_px: float

    @property
    def diameter_px(self) -> float:
        """面積等価直径 [px]."""
        return 2.0 * math.sqrt(self.area_px / math.pi)

    @property
    def circularity(self) -> float:
        """円形度 4πA/P²（外周長が 0 なら 0.0）.

        真円で 1.0 前後、細長い成分ほど 0 に近づく。ただし **0-1 に収まる保証は
        ない**。``cv2.arcLength`` は画素階段の周長を過小評価するので、直径が
        10 px を下回る小片では 1 を超える（r=1 で約 2.0、r=2 で約 1.3）。
        小片を弾くのはこの指標ではなく面積・直径の下限の役目。
        """
        if self.perimeter_px <= 0.0:
            return 0.0
        return 4.0 * math.pi * self.area_px / self.perimeter_px**2


def darkening(pre_bgr: ImageArray, post_bgr: ImageArray) -> ImageArray:
    """塗布によって暗くなった量（0-255 の uint8）。明るくなった側は 0 に潰す."""
    return _clipped_difference(_gray(pre_bgr), _gray(post_bgr))


def background_darkening(bgr: ImageArray, *, kernel_px: int) -> ImageArray:
    """1 枚の画像で、推定背景より暗くなった量（0-255 の uint8）を返す.

    背景は ``kernel_px`` の closing で推定する。斑点より大きいカーネルなら斑点は
    周囲の明るさで埋まり、照明ムラだけが背景として残る。

    Args:
        bgr: 入力画像（OpenCV の BGR 3 channel）
        kernel_px: 背景推定 closing の正方カーネル [px]（1 以上）

    Raises:
        ValueError: ``kernel_px`` が 1 未満
    """
    if kernel_px < 1:
        raise ValueError(f"背景推定カーネルは1以上が必要です: {kernel_px!r}")
    gray = _gray(bgr)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_px, kernel_px))
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
    return _clipped_difference(background, gray)


def background_kernel_px(max_diameter_px: float) -> int:
    """想定する最大直径から背景推定 closing のカーネル [px] を決める（3 以上の奇数）."""
    return max(3, math.ceil(max_diameter_px * _BACKGROUND_KERNEL_RATIO) | 1)


def segment_darkening(
    difference: ImageArray, spec: DotDetectionSpec
) -> DarkSegmentation:
    """差分から 2 値マスクを作る（blank ガードに掛かればマスクは ``None``）.

    塗布していない箇所の背景ノイズを Otsu が拾わないよう、分位点コントラストが
    ``min_contrast`` に届かない時点で打ち切る。
    """
    contrast = float(np.percentile(difference, spec.contrast_percentile))
    if contrast < spec.min_contrast:
        return DarkSegmentation(mask=None, contrast=contrast, threshold=0.0)
    threshold = max(
        _otsu_threshold(difference), spec.min_contrast * spec.threshold_floor_ratio
    )
    return DarkSegmentation(
        mask=_binary_mask(difference, threshold, spec.open_kernel_px),
        contrast=contrast,
        threshold=threshold,
    )


def fill_dark_spot_holes(mask: ImageArray) -> ImageArray:
    """成分に囲まれた背景（穴）を前景へ変えたマスクを返す.

    光沢のある塗布痕は中心のハイライトが 2 値化から抜け、マスクが環になる。環のままだと
    面積が減るのに外周長は変わらないので円形度が落ち、塗布痕として採れない （外半径の 1/3 を超えるハイライトで既定の下限 0.7
    を割る）。穴を埋めれば直径も 円形度も中実の円と同じに戻る。

    穴は「画像の縁から届かない背景」として求める。したがって別の成分の穴の中にある
    成分は、間の背景ごと埋まって外側の成分と繋がる。呼び出し側はその塊を直径の 範囲で弾くことになる。
    """
    height, width = mask.shape[:2]
    padded = np.zeros((height + 2, width + 2), dtype=np.uint8)
    padded[1:-1, 1:-1] = mask
    flood_mask = np.zeros((height + 4, width + 4), dtype=np.uint8)
    # 縁を 1 px 足してから縁の背景を塗り潰す。塗り残った背景が穴
    cv2.floodFill(padded, flood_mask, (0, 0), 255)
    filled = mask.copy()
    filled[padded[1:-1, 1:-1] == 0] = 255
    return filled


def dark_spots(mask: ImageArray) -> tuple[DarkSpot, ...]:
    """2 値マスクの連結成分（背景を除く）を返す."""
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )
    return tuple(
        DarkSpot(
            area_px=int(stats[label, cv2.CC_STAT_AREA]),
            center=Point2d(x=float(centroids[label][0]), y=float(centroids[label][1])),
            perimeter_px=_perimeter(labels, stats, label),
        )
        for label in range(1, count)
    )


def _gray(bgr: ImageArray) -> ImageArray:
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def _clipped_difference(brighter: ImageArray, darker: ImageArray) -> ImageArray:
    """``brighter - darker`` を 0-255 に収めた uint8（負側は 0）."""
    return np.clip(brighter.astype(np.int16) - darker.astype(np.int16), 0, 255).astype(
        np.uint8
    )


def _otsu_threshold(difference: ImageArray) -> float:
    """差分画像の Otsu 閾値."""
    threshold, _ = cv2.threshold(
        difference, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    return float(threshold)


def _binary_mask(
    difference: ImageArray, threshold: float, open_kernel_px: int
) -> ImageArray:
    """閾値で 2 値化し、必要なら open で孤立点を落とす."""
    _, mask = cv2.threshold(difference, threshold, 255, cv2.THRESH_BINARY)
    if open_kernel_px <= 0:
        return mask
    kernel = np.ones((open_kernel_px, open_kernel_px), np.uint8)
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)


def _perimeter(labels: ImageArray, stats: ImageArray, label: int) -> float:
    """1 成分の外周長。その成分だけを bbox で切り出して測る.

    マスク全体に ``RETR_EXTERNAL`` をかけると、別の成分の穴の中にある成分の輪郭が
    返らず周長 0（円形度 0）になる。成分を切り出せばその取り違えが起きない。
    """
    x = int(stats[label, cv2.CC_STAT_LEFT])
    y = int(stats[label, cv2.CC_STAT_TOP])
    width = int(stats[label, cv2.CC_STAT_WIDTH])
    height = int(stats[label, cv2.CC_STAT_HEIGHT])
    component = (labels[y : y + height, x : x + width] == label).astype(np.uint8)
    contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    return sum(float(cv2.arcLength(contour, True)) for contour in contours)
