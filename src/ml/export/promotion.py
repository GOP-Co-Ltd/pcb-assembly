"""推論候補の promote 可否判定.

FP32 と static INT8 の候補を並べ、精度と実機 latency の証拠から 1 個を選ぶ。

判定は事後の純関数とし、候補を作る側と選ぶ側を分ける。

実機 latency の証拠が無い候補は構造的に promote できない。

「生成しただけの INT8 を採用しない」を、運用の心掛けではなくコードの形にする。

判定そのものが成立しない場合と、全候補が落ちた場合は区別する。

前者は理由文字列で、後者は ``promoted_candidate_id`` が ``None`` の決定で返す。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

import attrs

from ml.artifact.document import DocumentKind
from ml.export.manifest import PRECISIONS, Precision, QuantizationRecord
from ml.serialization import make_strict_converter

PROMOTION_DECISION_DOCUMENT = DocumentKind(
    kind="ml-promotion-decision", schema_version=1
)
CALIBRATION_SPLIT = "train"

_BASELINE_PRECISION: Precision = "float32"
_CONVERTER = make_strict_converter()


@attrs.frozen
class AccuracyGate:
    """精度側の合格条件.

    ``reference_coverage`` は 1 標準偏差区間の理論値で、そこからの乖離を見る。
    """

    maximum_primary_score: float = 0.10
    reference_coverage: float = 0.683
    maximum_coverage_difference: float = 0.03
    maximum_primary_score_regression: float = 0.01

    def validate(self) -> str | None:
        """精度 gate の各閾値が使える範囲にあるかを検証する."""

        for name in (
            "maximum_primary_score",
            "reference_coverage",
            "maximum_coverage_difference",
            "maximum_primary_score_regression",
        ):
            value: float = getattr(self, name)
            if not math.isfinite(value) or value < 0.0:
                return f"{name} は 0 以上の有限値が必要です: {value}"
        if not 0.0 < self.reference_coverage <= 1.0:
            return (
                "reference_coverage は 0 より大きく 1 以下が必要です: "
                f"{self.reference_coverage}"
            )
        if self.maximum_primary_score <= 0.0:
            return (
                "maximum_primary_score は正の値が必要です: "
                f"{self.maximum_primary_score}"
            )
        return None


@attrs.frozen
class LatencyGate:
    """Latency 側の合格条件と、差を無視できる範囲.

    ``negligible_difference_ratio`` 未満の p95 差は「同点」として扱う。
    """

    maximum_p95_seconds: float = 1.0
    negligible_difference_ratio: float = 0.05

    def validate(self) -> str | None:
        """Latency gate の閾値が使える範囲にあるかを検証する."""

        if not math.isfinite(self.maximum_p95_seconds) or (
            self.maximum_p95_seconds <= 0.0
        ):
            return (
                "maximum_p95_seconds は正の有限値が必要です: "
                f"{self.maximum_p95_seconds}"
            )
        # 0 以上 1 未満の範囲検査が nan と ±inf をすべて弾くので isfinite は要らない
        if not 0.0 <= self.negligible_difference_ratio < 1.0:
            return (
                "negligible_difference_ratio は 0 以上 1 未満が必要です: "
                f"{self.negligible_difference_ratio}"
            )
        return None


@attrs.frozen
class AccuracyEvidence:
    """1 候補ぶんの精度の実測.

    ``evaluated_split`` は候補間で揃っている必要がある。

    候補選択中は validation、固定後は凍結 test を使い、混ぜたら比較にならない。
    """

    primary_score: float
    one_standard_deviation_coverage: float
    evaluated_split: str

    def validate(self) -> str | None:
        """精度の実測値が数として成立しているかを検証する."""

        if not math.isfinite(self.primary_score) or self.primary_score < 0.0:
            return f"primary_score は 0 以上の有限値が必要です: {self.primary_score}"
        if not 0.0 <= self.one_standard_deviation_coverage <= 1.0:
            return (
                "one_standard_deviation_coverage は 0 以上 1 以下が必要です: "
                f"{self.one_standard_deviation_coverage}"
            )
        if not self.evaluated_split:
            return "evaluated_split は空にできません"
        return None


@attrs.frozen
class LatencyEvidence:
    """1 候補ぶんの実機 latency の実測.

    この証拠を作れるのは実機で benchmark を回した利用者だけとする。
    """

    device_label: str
    worst_p95_seconds: float
    cold_start_seconds: float

    def validate(self) -> str | None:
        """実測 latency が数として成立しているかを検証する."""

        if not self.device_label:
            return "device_label は空にできません"
        for name in ("worst_p95_seconds", "cold_start_seconds"):
            value: float = getattr(self, name)
            if not math.isfinite(value) or value <= 0.0:
                return f"{name} は正の有限値が必要です: {value}"
        return None


@attrs.frozen
class PromotionCandidate:
    """Promote を争う 1 候補と、その全証拠.

    ``latency`` が ``None`` の候補は実機実測が無いので採用され得ない。
    """

    candidate_id: str
    precision: Precision
    artifact_bytes: int
    export_parity_passed: bool
    accuracy: AccuracyEvidence
    latency: LatencyEvidence | None = None
    quantization: QuantizationRecord | None = None

    def validate(self) -> str | None:
        """候補の記述が判定に使える形かを検証する."""

        if not self.candidate_id:
            return "candidate_id は空にできません"
        if self.precision not in PRECISIONS:
            return f"未知の precision です: {self.precision!r}"
        if self.artifact_bytes <= 0:
            return f"artifact_bytes は正の整数が必要です: {self.artifact_bytes}"
        if error := self.accuracy.validate():
            return f"{self.candidate_id}: {error}"
        if self.latency is not None and (error := self.latency.validate()):
            return f"{self.candidate_id}: {error}"
        if self.quantization is not None and (error := self.quantization.validate()):
            return f"{self.candidate_id}: {error}"
        return None


@attrs.frozen
class RejectedCandidate:
    """却下した候補と、その理由."""

    candidate_id: str
    reason: str


@attrs.frozen
class PromotionDecision:
    """候補の突き合わせ結果.

    ``promoted_candidate_id`` が ``None`` なら、全候補が gate に落ちたことを表す。
    """

    promoted_candidate_id: str | None
    baseline_candidate_id: str
    evaluated_split: str
    rejected: tuple[RejectedCandidate, ...]
    summary: str

    @classmethod
    def decide(
        cls,
        candidates: Sequence[PromotionCandidate],
        *,
        accuracy_gate: AccuracyGate,
        latency_gate: LatencyGate,
    ) -> tuple[PromotionDecision | None, str | None]:
        """全候補の証拠を突き合わせ、promote する 1 個を選ぶ.

        判定そのものが成立しないときだけ第 2 戻り値へ理由を返す。

        候補が全部落ちた場合は決定を返し、候補ごとの却下理由を載せる。
        """

        baseline, error = _baseline_candidate(candidates, accuracy_gate, latency_gate)
        if baseline is None:
            return None, error

        rejected: list[RejectedCandidate] = []
        survivors: list[PromotionCandidate] = []
        for candidate in candidates:
            reason = _rejection_reason(
                candidate,
                baseline=baseline,
                accuracy_gate=accuracy_gate,
                latency_gate=latency_gate,
            )
            if reason is None:
                survivors.append(candidate)
            else:
                rejected.append(
                    RejectedCandidate(
                        candidate_id=candidate.candidate_id, reason=reason
                    )
                )

        promoted = _best_candidate(survivors, latency_gate)
        return (
            cls(
                promoted_candidate_id=None
                if promoted is None
                else promoted.candidate_id,
                baseline_candidate_id=baseline.candidate_id,
                evaluated_split=baseline.accuracy.evaluated_split,
                rejected=tuple(rejected),
                summary=_summary(promoted, survivors, rejected),
            ),
            None,
        )

    def save(self, path: Path) -> None:
        """判定結果を envelope 付き JSON として atomic に書き出す."""

        PROMOTION_DECISION_DOCUMENT.save(path, self, converter=_CONVERTER)

    @classmethod
    def load(cls, path: Path) -> tuple[PromotionDecision | None, str | None]:
        """判定結果を読み、構造が崩れていれば理由を返す."""

        return PROMOTION_DECISION_DOCUMENT.load(path, cls, converter=_CONVERTER)


def _baseline_candidate(
    candidates: Sequence[PromotionCandidate],
    accuracy_gate: AccuracyGate,
    latency_gate: LatencyGate,
) -> tuple[PromotionCandidate | None, str | None]:
    """判定が成立する前提を確かめ、比較の基準になる候補を返す."""

    if error := accuracy_gate.validate():
        return None, f"accuracy_gate: {error}"
    if error := latency_gate.validate():
        return None, f"latency_gate: {error}"
    if not candidates:
        return None, "候補が 1 個もありません"
    for candidate in candidates:
        if error := candidate.validate():
            return None, error
    identifiers = [candidate.candidate_id for candidate in candidates]
    if len(set(identifiers)) != len(identifiers):
        return None, f"candidate_id が重複しています: {sorted(identifiers)}"
    splits = sorted({candidate.accuracy.evaluated_split for candidate in candidates})
    if len(splits) != 1:
        return None, f"候補間で evaluated_split が一致しません: {splits}"
    baselines = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.precision == _BASELINE_PRECISION
        ),
        key=lambda candidate: candidate.candidate_id,
    )
    if not baselines:
        return None, f"precision が {_BASELINE_PRECISION} の候補がありません"
    return baselines[0], None


def _rejection_reason(
    candidate: PromotionCandidate,
    *,
    baseline: PromotionCandidate,
    accuracy_gate: AccuracyGate,
    latency_gate: LatencyGate,
) -> str | None:
    """候補が落ちる理由を 1 つ返す.

    落ちなければ ``None`` を返す。
    """

    if not candidate.export_parity_passed:
        return "export parity が通っていません"
    accuracy = candidate.accuracy
    if accuracy.primary_score > accuracy_gate.maximum_primary_score:
        return (
            f"primary_score が gate を超えています: {accuracy.primary_score}"
            f"（上限 {accuracy_gate.maximum_primary_score}）"
        )
    coverage_difference = abs(
        accuracy.one_standard_deviation_coverage - accuracy_gate.reference_coverage
    )
    if coverage_difference > accuracy_gate.maximum_coverage_difference:
        return (
            f"coverage が基準から離れています: "
            f"{accuracy.one_standard_deviation_coverage}"
            f"（基準 {accuracy_gate.reference_coverage}、"
            f"許容差 {accuracy_gate.maximum_coverage_difference}）"
        )
    if candidate.candidate_id != baseline.candidate_id:
        regression = accuracy.primary_score - baseline.accuracy.primary_score
        if regression > accuracy_gate.maximum_primary_score_regression:
            return (
                f"baseline に対する primary_score の悪化が大きすぎます: "
                f"{regression}"
                f"（上限 {accuracy_gate.maximum_primary_score_regression}）"
            )
        baseline_coverage_difference = abs(
            accuracy.one_standard_deviation_coverage
            - baseline.accuracy.one_standard_deviation_coverage
        )
        if baseline_coverage_difference > accuracy_gate.maximum_coverage_difference:
            return (
                f"baseline に対する coverage の差が大きすぎます: "
                f"{baseline_coverage_difference}"
                f"（許容差 {accuracy_gate.maximum_coverage_difference}）"
            )
    if (
        candidate.quantization is not None
        and candidate.quantization.calibration_split != CALIBRATION_SPLIT
    ):
        return (
            f"校正に {CALIBRATION_SPLIT} 以外の split を使っています: "
            f"{candidate.quantization.calibration_split!r}"
        )
    latency = candidate.latency
    if latency is None:
        return "実機 latency の実測がありません"
    if latency.worst_p95_seconds > latency_gate.maximum_p95_seconds:
        return (
            f"p95 latency が gate を超えています: {latency.worst_p95_seconds} 秒"
            f"（上限 {latency_gate.maximum_p95_seconds} 秒）"
        )
    return None


def _best_candidate(
    survivors: Sequence[PromotionCandidate], latency_gate: LatencyGate
) -> PromotionCandidate | None:
    """残った候補から、最も速く最も小さい 1 個を選ぶ.

    Gate を通った候補は必ず実機 latency を持つ（``_rejection_reason`` が検査済み）。
    """

    measured = [
        (candidate, candidate.latency.worst_p95_seconds)
        for candidate in survivors
        if candidate.latency is not None
    ]
    if not measured:
        return None
    fastest = min(seconds for _, seconds in measured)
    tied = [
        candidate
        for candidate, seconds in measured
        if (seconds - fastest) / fastest < latency_gate.negligible_difference_ratio
    ]
    return min(
        tied,
        key=lambda candidate: (
            candidate.artifact_bytes,
            candidate.precision != _BASELINE_PRECISION,
            candidate.candidate_id,
        ),
    )


def _summary(
    promoted: PromotionCandidate | None,
    survivors: Sequence[PromotionCandidate],
    rejected: Sequence[RejectedCandidate],
) -> str:
    if promoted is None:
        return (
            f"promote できる候補がありません（却下 {len(rejected)} 件）。"
            "実機 latency の実測と精度 gate を確認してください"
        )
    latency = promoted.latency
    p95 = "不明" if latency is None else f"{latency.worst_p95_seconds} 秒"
    return (
        f"{promoted.candidate_id} を promote します"
        f"（precision={promoted.precision}、p95={p95}、"
        f"artifact={promoted.artifact_bytes} bytes、"
        f"候補 {len(survivors)} 件が gate 通過、却下 {len(rejected)} 件）"
    )


__all__ = [
    "CALIBRATION_SPLIT",
    "PROMOTION_DECISION_DOCUMENT",
    "AccuracyEvidence",
    "AccuracyGate",
    "LatencyEvidence",
    "LatencyGate",
    "PromotionCandidate",
    "PromotionDecision",
    "RejectedCandidate",
]
