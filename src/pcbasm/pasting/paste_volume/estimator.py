"""校正から塗布量を推定する公開 API.

抽象基底（Protocol / ABC）は作らない。直径以外の推定方式へ移るときは、並存させず
この具象を置き換える見込みが高いので、いま抽象を用意しても使われない。

推定できないことは例外にせず ``accepted=False`` と理由文字列で返す。運転時
キャリブレーションでは複数点を推定して集約するので、1 点の失敗でジョブ全体を
失敗させると運用が止まる。不正な校正ファイルは load 時の error として、推定不能とは
区別する。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import attrs

from pcbasm.pasting.paste_volume.aggregate import DotDiameter, aggregate_views
from pcbasm.pasting.paste_volume.calibration import (
    PasteVolumeCalibration,
    load_calibration,
)
from pcbasm.pasting.paste_volume.detect import DotMeasurement, measure_dot
from pcbasm.vision.image import ImageArray


@attrs.frozen
class PasteVolumePrediction:
    """1 セルぶんの推定結果.

    Attributes:
        mean_volume_ul: 推定塗布体積 [µL]（不採用なら 0.0）
        std_volume_ul: 推定の標準偏差 [µL]
        relative_std: ``std / mean``（校正時の相対残差そのもの）
        accepted: 補正の材料として採用できるか
        rejection_reason: 不採用の理由（採用時は ``None``）
    """

    mean_volume_ul: float
    std_volume_ul: float
    relative_std: float
    accepted: bool
    rejection_reason: str | None


class DiameterVolumeEstimator:
    """円直径の 3 次モデルで塗布量を推定する.

    不確かさは校正時に記録した相対残差から与える。直径方式には 1 点ごとの不確かさ源
    が無いので、分布としての当てはまり具合を使うほかない。

    校正の条件（ペースト・ノズル径・塗布高さ・撮影スケール）が推定時と一致するかは
    ここでは見ない。

    照合して報告するのは評価側の責務で、推定器が推定を拒否すると条件不一致の session を
    評価すること自体ができなくなる。

    ``diagnostics.monotonic_in_range`` も同じく参照しない。

    校正生成時に確かめて記録する診断であり、推定のたびに再判定はしない。
    """

    def __init__(
        self,
        calibration: PasteVolumeCalibration,
        *,
        reliable_range_only: bool = False,
    ) -> None:
        """校正を束ねた推定器を作る.

        Args:
            calibration: 使う校正
            reliable_range_only: 被覆域の下端付近を採用しないか。
                運転時キャリブレーションの補正材料を選ぶときだけ ``True`` にする。
                校正の生成・検証は被覆域そのものを使う。
        """
        self._calibration = calibration
        self._reliable_range_only = reliable_range_only

    @property
    def calibration(self) -> PasteVolumeCalibration:
        """推定に使っている校正."""
        return self._calibration

    def predict(
        self, pre_bgr: ImageArray, post_bgr: ImageArray, *, pixel_per_mm: float
    ) -> PasteVolumePrediction:
        """1 view の塗布前後画像（OpenCV の BGR）から塗布量を推定する."""
        return self.predict_views([(pre_bgr, post_bgr)], pixel_per_mm=pixel_per_mm)

    def predict_views(
        self,
        views: Sequence[tuple[ImageArray, ImageArray]],
        *,
        pixel_per_mm: float,
    ) -> PasteVolumePrediction:
        """同じセルの複数 view（OpenCV の BGR）から塗布量を推定する.

        view ごとに直径を測って中央値を採り、モデルへ 1 回だけ通す。

        被覆域で単調なモデルなら、view 数が奇数のとき集約の順序を入れ替えられる。

        偶数のときは中央 2 つの平均になり、モデルの 2 次以上のぶんだけ差が出る。

        1 view でも構造的不正があればセル全体を不採用にする。

        構造的不正は呼び出し側のバグなので、残りの view で補わず、その時点で不採用を返す。
        """
        if not views:
            return _rejected("no_views")

        measurements: list[DotMeasurement] = []
        for pre_bgr, post_bgr in views:
            measurement, error = measure_dot(
                pre_bgr,
                post_bgr,
                pixel_per_mm=pixel_per_mm,
                spec=self._calibration.detection,
            )
            if measurement is None:
                return _rejected(f"image_invalid: {error}")
            measurements.append(measurement)

        return self.predict_diameter(aggregate_views(measurements))

    def predict_diameter(self, diameter: DotDiameter) -> PasteVolumePrediction:
        """集約済みの直径から塗布量を推定する.

        画像を読み直さずに済むので、session 全体を計測してから評価する経路で使う。
        """
        if diameter.detected_view_count == 0:
            return _rejected("no_deposit_detected")
        model = self._calibration.model
        if not model.covers(diameter.diameter_mm):
            return _rejected("diameter_out_of_calibrated_range")
        if self._reliable_range_only and not model.covers_reliably(
            diameter.diameter_mm
        ):
            return _rejected("diameter_below_reliable_range")
        mean = model.volume_ul(diameter.diameter_mm)
        if mean <= 0.0:
            return _rejected("no_deposit_detected")
        relative = self._calibration.diagnostics.residual_relative_std
        return PasteVolumePrediction(
            mean_volume_ul=mean,
            std_volume_ul=mean * relative,
            relative_std=relative,
            accepted=True,
            rejection_reason=None,
        )


def load_diameter_estimator(
    path: Path, *, reliable_range_only: bool = False
) -> tuple[DiameterVolumeEstimator | None, str | None]:
    """校正ファイルから推定器を組む（読めない・不正は理由を返す）."""
    calibration, error = load_calibration(path)
    if calibration is None:
        return None, error
    return (
        DiameterVolumeEstimator(calibration, reliable_range_only=reliable_range_only),
        None,
    )


def _rejected(reason: str) -> PasteVolumePrediction:
    """補正の材料にしない結果（例外の代わり）."""
    return PasteVolumePrediction(
        mean_volume_ul=0.0,
        std_volume_ul=0.0,
        relative_std=0.0,
        accepted=False,
        rejection_reason=reason,
    )
