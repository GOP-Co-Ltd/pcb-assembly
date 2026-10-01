"""塗布前後画像から、はんだの円の直径を測る.

塗布後のはんだは銅板より暗いので、``pre - post`` の差分に現れる。差分を使うのは、
銅板のヘアラインや照明ムラが塗布前後で共通なので引き算で消えるため。

判定は 2 段構えにする。まず差分の分位点が ``min_contrast`` に届かなければ直径 0 と
する（blank ガード）。届いた場合だけ Otsu で閾値を決める。Otsu は「必ず何かを 2 値化
する」ので、これ単独では塗布していない blank セルの背景ノイズを拾ってしまう。

直径は最大連結成分の面積から求めた等価直径にする。円形度で除外すると、潰れた点や
隣接ノイズと接した点まで除外してしまう。

2 値化と連結成分の段は :mod:`pcbasm.vision.dot` にあり、単一フレームから背景を
推定する :class:`~pcbasm.vision.PasteDotDetector`（ツールヘッドオフセット計測）と
共有する。
"""

from __future__ import annotations

import math

import attrs
import numpy as np

from pcbasm.utils import is_finite_number
from pcbasm.vision.dot import (
    DotDetectionSpec,
    dark_spots,
    darkening,
    segment_darkening,
)
from pcbasm.vision.image import ImageArray

__all__ = [
    "DETECTION_KIND",
    "DotDetectionSpec",
    "DotMeasurement",
    "detection_mask",
    "measure_dot",
]

# 校正ファイルへ記録する検出方式の識別子（将来別方式を足したときに区別する）
DETECTION_KIND = "diameter_otsu_v1"


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

    segmentation = segment_darkening(darkening(pre_bgr, post_bgr), spec)
    if segmentation.mask is None:
        return _undetected(segmentation.contrast), None
    area_px = _largest_component_area(segmentation.mask)
    if area_px < spec.min_area_px:
        return (
            _undetected(segmentation.contrast, threshold=segmentation.threshold),
            None,
        )

    return (
        DotMeasurement(
            diameter_mm=2.0 * math.sqrt(area_px / math.pi) / pixel_per_mm,
            area_px=area_px,
            contrast=segmentation.contrast,
            threshold=segmentation.threshold,
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

    2 値化の結果を見るためのマスクなので、最大連結成分が下限未満でも
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
    segmentation = segment_darkening(darkening(pre_bgr, post_bgr), spec)
    if segmentation.mask is None:
        return np.zeros(pre_bgr.shape[:2], dtype=np.uint8), None
    return segmentation.mask, None


def _image_error(pre_bgr: ImageArray, post_bgr: ImageArray) -> str | None:
    """塗布前後の画像が対で 3 channel かを確かめる（不正なら理由文）."""
    if pre_bgr.shape != post_bgr.shape:
        return f"塗布前後の画像サイズが違います: {pre_bgr.shape} != {post_bgr.shape}"
    if pre_bgr.ndim != 3 or pre_bgr.shape[2] != 3:
        return f"3 channelの画像が必要です: shape={pre_bgr.shape}"
    return None


def _undetected(contrast: float, *, threshold: float = 0.0) -> DotMeasurement:
    """はんだを検出しなかった結果（blank セルの真値 0 と同じ形）."""
    return DotMeasurement(
        diameter_mm=0.0,
        area_px=0,
        contrast=contrast,
        threshold=threshold,
        detected=False,
    )


def _largest_component_area(mask: ImageArray) -> int:
    """最大連結成分の面積 [px]（背景を除く。成分が無ければ 0）."""
    return max((spot.area_px for spot in dark_spots(mask)), default=0)
