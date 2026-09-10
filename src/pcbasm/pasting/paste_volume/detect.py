"""塗布前後画像から、はんだの円の直径を測る.

塗布後のはんだは銅板より暗いので、``pre - post`` の差分に現れる。差分を使うのは、
銅板のヘアラインや照明ムラが塗布前後で共通なので引き算で消えるため。

判定は 2 段構えにする。まず差分の分位点が ``min_contrast`` に届かなければ直径 0 と
する（blank ガード）。届いた場合だけ Otsu で閾値を決める。Otsu は「必ず何かを 2 値化
する」ので、これ単独では塗布していない blank セルの背景ノイズを拾ってしまう。

直径は最大連結成分の面積から求めた等価直径にする。円形度で弾くと、潰れた点や
隣接ノイズと接した点を落としてしまう。
"""

from __future__ import annotations

import math

import attrs
import cv2
import numpy as np

from pcbasm.utils import is_finite_number
from pcbasm.vision.image import ImageArray

# 校正ファイルへ記録する検出方式の識別子（将来別方式を足したときに区別する）
DETECTION_KIND = "diameter_otsu_v1"


@attrs.frozen
class DotDetectionSpec:
    """円検出のハイパーパラメータ.

    Attributes:
        min_contrast: 塗布ありと判定する差分の下限（0-255）。分位点がこれ未満なら直径 0
        contrast_percentile: コントラストを測る分位点 [%]（外れ画素へ寄りすぎない値）
        threshold_floor_ratio: Otsu 閾値の下限を ``min_contrast`` の何倍にするか
        open_kernel_px: モルフォロジー open の正方カーネル [px]（0 で無効、正の奇数）
        min_area_px: 最大連結成分の面積下限 [px]（未満は直径 0）
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
        if type(self.min_area_px) is not int or self.min_area_px < 1:
            # 0 を許すと面積 0 の成分が「検出できた」になり、detected と直径 0 の
            # 対応（:class:`DotMeasurement` の不変条件）が壊れる
            return f"最小面積は1以上の整数が必要です: {self.min_area_px!r}"
        return None


@attrs.frozen
class DotMeasurement:
    """1 view ぶんの計測結果.

    塗布が写っていないことは失敗ではない。``detected`` が ``False`` で
    ``diameter_mm`` が厳密に 0.0 になる（blank セルの真値 0 をそのまま表せる）。

    Attributes:
        diameter_mm: 面積等価直径 [mm]（未検出は 0.0）
        area_px: 採用した連結成分の面積 [px]（未検出は 0）
        contrast: 差分の ``contrast_percentile`` 分位点（0-255）
        threshold: 実際に使った 2 値化閾値（blank ガードで抜けたら 0.0）
        detected: はんだを検出したか
    """

    diameter_mm: float
    area_px: int
    contrast: float
    threshold: float
    detected: bool


def measure_dot(
    pre_bgr: ImageArray,
    post_bgr: ImageArray,
    *,
    pixel_per_mm: float,
    spec: DotDetectionSpec = DotDetectionSpec(),
) -> tuple[DotMeasurement | None, str | None]:
    """塗布前後画像（OpenCV の BGR）から円の面積等価直径を測る.

    ``(None, 理由)`` を返すのは構造的な不正（shape 不一致・3 channel でない・
    ``pixel_per_mm`` が非正・spec が不正）だけ。はんだが写っていないのは正常系で、
    ``detected=False`` / ``diameter_mm=0.0`` を返す。

    Args:
        pre_bgr: 塗布前画像（OpenCV の BGR 3 channel）
        post_bgr: 塗布後画像（``pre_bgr`` と同じ shape）
        pixel_per_mm: 画像の物理スケール [px/mm]
        spec: 検出ハイパーパラメータ

    Returns:
        ``(計測結果, None)`` または ``(None, 理由)``
    """
    error = spec.validate()
    if error is not None:
        return None, error
    if not is_finite_number(pixel_per_mm) or pixel_per_mm <= 0:
        return None, f"pixel_per_mmは正の有限値が必要です: {pixel_per_mm!r}"
    error = _image_error(pre_bgr, post_bgr)
    if error is not None:
        return None, error

    mask, contrast, threshold = _segment(pre_bgr, post_bgr, spec)
    if mask is None:
        return _undetected(contrast), None
    area_px = _largest_component_area(mask)
    if area_px < spec.min_area_px:
        return _undetected(contrast, threshold=threshold), None

    return (
        DotMeasurement(
            diameter_mm=2.0 * math.sqrt(area_px / math.pi) / pixel_per_mm,
            area_px=area_px,
            contrast=contrast,
            threshold=threshold,
            detected=True,
        ),
        None,
    )


def detection_mask(
    pre_bgr: ImageArray,
    post_bgr: ImageArray,
    *,
    spec: DotDetectionSpec = DotDetectionSpec(),
) -> tuple[ImageArray | None, str | None]:
    """:func:`measure_dot` が面積を数えた 2 値マスクを返す（ハイパラ調整の目視用）.

    blank ガードで打ち切ったときは全 0 のマスクを返す。検出されなかったことと、
    検出した結果が空だったことを図の上で区別しないためで、失敗ではない。

    ``min_area_px`` による棄却は反映しない。

    2 値化がどう効いたかを見るためのマスクなので、最大連結成分が下限未満でも
    斑点は残る（そのとき :func:`measure_dot` の直径は 0 になる）。

    Args:
        pre_bgr: 塗布前画像（OpenCV の BGR 3 channel）
        post_bgr: 塗布後画像（``pre_bgr`` と同じ shape）
        spec: 検出ハイパーパラメータ

    Returns:
        ``(マスク, None)`` または ``(None, 理由)``
    """
    error = spec.validate() or _image_error(pre_bgr, post_bgr)
    if error is not None:
        return None, error
    mask, _, _ = _segment(pre_bgr, post_bgr, spec)
    if mask is None:
        return np.zeros(pre_bgr.shape[:2], dtype=np.uint8), None
    return mask, None


def _image_error(pre_bgr: ImageArray, post_bgr: ImageArray) -> str | None:
    """塗布前後の画像が対で 3 channel かを確かめる（不正なら理由文）."""
    if pre_bgr.shape != post_bgr.shape:
        return f"塗布前後の画像サイズが違います: {pre_bgr.shape} != {post_bgr.shape}"
    if pre_bgr.ndim != 3 or pre_bgr.shape[2] != 3:
        return f"3 channelの画像が必要です: shape={pre_bgr.shape}"
    return None


def _segment(
    pre_bgr: ImageArray, post_bgr: ImageArray, spec: DotDetectionSpec
) -> tuple[ImageArray | None, float, float]:
    """差分から 2 値マスクを作る（blank ガードに掛かればマスクは ``None``）.

    塗布していないセルの背景ノイズを Otsu が拾わないよう、分位点コントラストが
    ``min_contrast`` に届かない時点で打ち切る。
    """
    difference = _darkening(pre_bgr, post_bgr)
    contrast = float(np.percentile(difference, spec.contrast_percentile))
    if contrast < spec.min_contrast:
        return None, contrast, 0.0
    threshold = max(
        _otsu_threshold(difference), spec.min_contrast * spec.threshold_floor_ratio
    )
    return _binary_mask(difference, threshold, spec.open_kernel_px), contrast, threshold


def _undetected(contrast: float, *, threshold: float = 0.0) -> DotMeasurement:
    """はんだを検出しなかった結果（blank セルの真値 0 と同じ形）."""
    return DotMeasurement(
        diameter_mm=0.0,
        area_px=0,
        contrast=contrast,
        threshold=threshold,
        detected=False,
    )


def _darkening(pre_bgr: ImageArray, post_bgr: ImageArray) -> ImageArray:
    """塗布によって暗くなった量（0-255 の uint8）。明るくなった側は 0 に潰す."""
    pre_gray = cv2.cvtColor(pre_bgr, cv2.COLOR_BGR2GRAY).astype(np.int16)
    post_gray = cv2.cvtColor(post_bgr, cv2.COLOR_BGR2GRAY).astype(np.int16)
    return np.clip(pre_gray - post_gray, 0, 255).astype(np.uint8)


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


def _largest_component_area(mask: ImageArray) -> int:
    """最大連結成分の面積 [px]（背景を除く。成分が無ければ 0）."""
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        return 0
    return int(stats[1:, cv2.CC_STAT_AREA].max())
