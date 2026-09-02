"""運転時の画像ベース吐出量キャリブレーション集約."""

from __future__ import annotations

import math
from collections.abc import Iterable

import attrs

from ml.paste_volume.infer import PasteVolumePrediction


@attrs.frozen
class PasteVolumeCalibrationSample:
    """1回の指令体積と画像推定結果."""

    commanded_volume_ul: float
    prediction: PasteVolumePrediction


@attrs.frozen
class PasteVolumeCalibrationResult:
    """一括計算した運転時キャリブレーション結果."""

    old_rotations_per_ul: float
    new_rotations_per_ul: float
    gain: float | None
    clamped: bool
    used_count: int
    rejected_count: int
    model_id: str | None
    rejection_reason: str | None

    @property
    def applied(self) -> bool:
        """補正値を安全に適用できる場合だけ真を返す."""

        return self.rejection_reason is None and self.used_count > 0


def calibrate_rotations_per_ul(
    old_rotations_per_ul: float,
    samples: Iterable[PasteVolumeCalibrationSample],
) -> PasteVolumeCalibrationResult:
    """acceptedな同一modelの結果を一度だけ集約し、1/3〜3倍へclampする.

    不正な個別sampleは棄却する。acceptedな結果に複数modelが混ざる場合や、 集約値が不正な場合は設定を変更しない。
    """

    if not _is_positive_finite(old_rotations_per_ul):
        raise ValueError("old_rotations_per_ulは正の有限値が必要です")

    materialized = tuple(samples)
    usable: list[PasteVolumeCalibrationSample] = []
    for sample in materialized:
        prediction = sample.prediction
        if (
            prediction.accepted
            and _is_positive_finite(sample.commanded_volume_ul)
            and _is_positive_finite(prediction.mean_volume_ul)
            and _is_nonnegative_finite(prediction.std_volume_ul)
            and prediction.model_id
        ):
            usable.append(sample)

    rejected_count = len(materialized) - len(usable)
    if not usable:
        return _rejected_calibration(
            old_rotations_per_ul,
            rejected_count,
            "accepted predictionがありません",
        )

    model_ids = {sample.prediction.model_id for sample in usable}
    if len(model_ids) != 1:
        return _rejected_calibration(
            old_rotations_per_ul,
            len(materialized),
            "複数modelのpredictionを混在できません",
        )

    estimated_total = math.fsum(sample.prediction.mean_volume_ul for sample in usable)
    commanded_total = math.fsum(sample.commanded_volume_ul for sample in usable)
    gain = estimated_total / commanded_total
    if not _is_positive_finite(gain):
        return _rejected_calibration(
            old_rotations_per_ul,
            len(materialized),
            "集約gainが正の有限値ではありません",
        )

    proposed = float(old_rotations_per_ul) / gain
    minimum = float(old_rotations_per_ul) / 3.0
    maximum = float(old_rotations_per_ul) * 3.0
    clamped_value = min(max(proposed, minimum), maximum)
    if not _is_positive_finite(clamped_value):
        return _rejected_calibration(
            old_rotations_per_ul,
            len(materialized),
            "補正値が正の有限値ではありません",
        )
    return PasteVolumeCalibrationResult(
        old_rotations_per_ul=float(old_rotations_per_ul),
        new_rotations_per_ul=clamped_value,
        gain=gain,
        clamped=not math.isclose(clamped_value, proposed, rel_tol=0.0, abs_tol=0.0),
        used_count=len(usable),
        rejected_count=rejected_count,
        model_id=next(iter(model_ids)),
        rejection_reason=None,
    )


def failed_paste_volume_calibration(
    old_rotations_per_ul: float,
    sample_count: int,
    reason: str,
    *,
    model_id: str | None = None,
) -> PasteVolumeCalibrationResult:
    """推定処理全体が失敗した場合の、非適用結果を作る."""

    if not _is_positive_finite(old_rotations_per_ul):
        raise ValueError("old_rotations_per_ulは正の有限値が必要です")
    if type(sample_count) is not int or sample_count < 0:
        raise ValueError("sample_countは0以上の整数が必要です")
    if not reason:
        raise ValueError("reasonを空にできません")
    return _rejected_calibration(
        old_rotations_per_ul,
        sample_count,
        reason,
        model_id=model_id,
    )


def _rejected_calibration(
    old_rotations_per_ul: float,
    rejected_count: int,
    reason: str,
    *,
    model_id: str | None = None,
) -> PasteVolumeCalibrationResult:
    return PasteVolumeCalibrationResult(
        old_rotations_per_ul=float(old_rotations_per_ul),
        new_rotations_per_ul=float(old_rotations_per_ul),
        gain=None,
        clamped=False,
        used_count=0,
        rejected_count=rejected_count,
        model_id=model_id,
        rejection_reason=reason,
    )


def _is_positive_finite(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _is_nonnegative_finite(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )
