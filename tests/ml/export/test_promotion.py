"""Promote 可否を決める純関数の公開契約.

計画 §4.2 / §6.2 / 論点 3 に対応する。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import attrs
import pytest

from ml.export.manifest import QuantizationRecord
from ml.export.promotion import (
    AccuracyEvidence,
    AccuracyGate,
    LatencyEvidence,
    LatencyGate,
    PromotionCandidate,
    PromotionDecision,
)

ACCURACY_GATE = AccuracyGate()
LATENCY_GATE = LatencyGate()

EVALUATED_SPLIT = "validation"
FLOAT32 = "float32-candidate"
INT8 = "static-int8-candidate"

QUANTIZATION = QuantizationRecord(
    method="static-qdq",
    activation_type="QUInt8",
    weight_type="QInt8",
    per_channel=False,
    quantized_operator_types=("Conv", "Gemm"),
    calibration_split="train",
    calibration_sample_ids=("sample-001", "sample-002"),
)


def _accuracy(
    *,
    primary_score: float = 0.05,
    coverage: float = 0.683,
    split: str = EVALUATED_SPLIT,
) -> AccuracyEvidence:
    return AccuracyEvidence(
        primary_score=primary_score,
        one_standard_deviation_coverage=coverage,
        evaluated_split=split,
    )


def _latency(p95_seconds: float) -> LatencyEvidence:
    return LatencyEvidence(
        device_label="raspberry-pi-5",
        worst_p95_seconds=p95_seconds,
        cold_start_seconds=p95_seconds * 4.0,
    )


def _float32(
    *,
    artifact_bytes: int = 200_000,
    p95_seconds: float | None = 0.500,
    primary_score: float = 0.05,
    coverage: float = 0.683,
    split: str = EVALUATED_SPLIT,
    export_parity_passed: bool = True,
    candidate_id: str = FLOAT32,
) -> PromotionCandidate:
    return PromotionCandidate(
        candidate_id=candidate_id,
        precision="float32",
        artifact_bytes=artifact_bytes,
        export_parity_passed=export_parity_passed,
        accuracy=_accuracy(primary_score=primary_score, coverage=coverage, split=split),
        latency=None if p95_seconds is None else _latency(p95_seconds),
    )


def _int8(
    *,
    artifact_bytes: int = 200_000,
    p95_seconds: float | None = 0.200,
    primary_score: float = 0.05,
    coverage: float = 0.683,
    split: str = EVALUATED_SPLIT,
    export_parity_passed: bool = True,
    quantization: QuantizationRecord | None = QUANTIZATION,
    candidate_id: str = INT8,
) -> PromotionCandidate:
    return PromotionCandidate(
        candidate_id=candidate_id,
        precision="static-int8",
        artifact_bytes=artifact_bytes,
        export_parity_passed=export_parity_passed,
        accuracy=_accuracy(primary_score=primary_score, coverage=coverage, split=split),
        latency=None if p95_seconds is None else _latency(p95_seconds),
        quantization=quantization,
    )


def _decide(candidates: Sequence[PromotionCandidate]) -> PromotionDecision:
    decision, error = PromotionDecision.decide(
        candidates, accuracy_gate=ACCURACY_GATE, latency_gate=LATENCY_GATE
    )

    assert error is None
    assert decision is not None
    return decision


def _rejected_ids(decision: PromotionDecision) -> tuple[str, ...]:
    return tuple(sorted(rejected.candidate_id for rejected in decision.rejected))


def _reason_for(decision: PromotionDecision, candidate_id: str) -> str:
    reasons = [
        rejected.reason
        for rejected in decision.rejected
        if rejected.candidate_id == candidate_id
    ]

    assert len(reasons) == 1, reasons
    return reasons[0]


class TestDecideSelectsAWinner:
    """証拠が揃った候補の中から 1 つを選ぶ."""

    def test_promotes_the_only_float32_candidate(self):
        decision = _decide([_float32()])

        assert decision.promoted_candidate_id == FLOAT32
        assert decision.baseline_candidate_id == FLOAT32
        assert decision.rejected == ()

    def test_promotes_the_int8_candidate_that_is_clearly_faster(self):
        decision = _decide([_float32(p95_seconds=0.500), _int8(p95_seconds=0.200)])

        assert decision.promoted_candidate_id == INT8
        assert decision.baseline_candidate_id == FLOAT32

    def test_prefers_the_smaller_artifact_when_the_latency_gap_is_negligible(self):
        # 0.490 と 0.500 の差は 2%。既定の negligible_difference_ratio 5% 未満
        decision = _decide(
            [
                _float32(p95_seconds=0.500, artifact_bytes=200_000),
                _int8(p95_seconds=0.490, artifact_bytes=150_000),
            ]
        )

        assert decision.promoted_candidate_id == INT8

    def test_prefers_the_larger_float32_over_a_negligibly_faster_int8(self):
        decision = _decide(
            [
                _float32(p95_seconds=0.500, artifact_bytes=200_000),
                _int8(p95_seconds=0.490, artifact_bytes=260_000),
            ]
        )

        assert decision.promoted_candidate_id == FLOAT32

    # candidate_id は最後の tie-break なので、INT8 の id が float32 より前後する
    # 両方を通さないと precision の優先そのものを見たことにならない。
    @pytest.mark.parametrize(
        "int8_candidate_id",
        [
            pytest.param(INT8, id="int8-id-sorts-after"),
            pytest.param("a-static-int8-candidate", id="int8-id-sorts-before"),
        ],
    )
    def test_prefers_float32_when_latency_and_size_both_tie(
        self, int8_candidate_id: str
    ):
        decision = _decide(
            [
                _float32(p95_seconds=0.500, artifact_bytes=200_000),
                _int8(
                    p95_seconds=0.500,
                    artifact_bytes=200_000,
                    candidate_id=int8_candidate_id,
                ),
            ]
        )

        assert decision.promoted_candidate_id == FLOAT32


class TestDecideRejectsCandidates:
    """却下条件を 1 つずつ満たす候補が落ちる."""

    def test_rejects_an_int8_candidate_without_latency_evidence(self):
        decision = _decide([_float32(), _int8(p95_seconds=None)])

        assert decision.promoted_candidate_id == FLOAT32
        assert _rejected_ids(decision) == (INT8,)

    def test_promotes_nothing_when_no_candidate_has_latency_evidence(self):
        decision = _decide([_float32(p95_seconds=None), _int8(p95_seconds=None)])

        assert decision.promoted_candidate_id is None
        assert _rejected_ids(decision) == tuple(sorted((FLOAT32, INT8)))
        assert all(rejected.reason for rejected in decision.rejected)

    def test_rejects_a_candidate_slower_than_the_latency_gate(self):
        decision = _decide([_float32(), _int8(p95_seconds=1.5)])

        assert decision.promoted_candidate_id == FLOAT32
        assert _rejected_ids(decision) == (INT8,)

    # 精度 gate は 絶対 primary_score → 絶対 coverage → baseline 比 regression →
    # baseline 比 coverage 差 の順に見るので、先の分岐が後を隠す。理由文まで見て
    # 「どの分岐が発火したか」を固定する。

    def test_rejects_a_candidate_whose_primary_score_exceeds_the_gate(self):
        decision = _decide([_float32(), _int8(primary_score=0.11)])

        assert decision.promoted_candidate_id == FLOAT32
        assert _rejected_ids(decision) == (INT8,)
        assert "primary_score が gate を超えています" in _reason_for(decision, INT8)

    def test_rejects_the_baseline_whose_primary_score_exceeds_the_gate(self):
        # baseline 自身には baseline 比の検査が掛からないので、絶対 gate だけが防壁
        decision = _decide([_float32(primary_score=0.11)])

        assert decision.promoted_candidate_id is None
        assert _rejected_ids(decision) == (FLOAT32,)
        assert "primary_score が gate を超えています" in _reason_for(decision, FLOAT32)

    def test_rejects_a_candidate_whose_coverage_is_too_far_from_the_reference(self):
        # baseline 比の差は 0.01 で許容内。基準 0.683 からの 0.037 だけが gate を超える
        decision = _decide([_float32(coverage=0.710), _int8(coverage=0.720)])

        assert decision.promoted_candidate_id == FLOAT32
        assert _rejected_ids(decision) == (INT8,)
        assert "coverage が基準から離れています" in _reason_for(decision, INT8)

    def test_rejects_the_baseline_whose_coverage_is_too_far_from_the_reference(self):
        decision = _decide([_float32(coverage=0.600)])

        assert decision.promoted_candidate_id is None
        assert _rejected_ids(decision) == (FLOAT32,)
        assert "coverage が基準から離れています" in _reason_for(decision, FLOAT32)

    def test_rejects_a_candidate_that_regresses_against_the_baseline(self):
        # gate 内 (0.10 未満) だが baseline から 0.02 悪化しており 0.01 を超える
        decision = _decide([_float32(primary_score=0.05), _int8(primary_score=0.07)])

        assert decision.promoted_candidate_id == FLOAT32
        assert _rejected_ids(decision) == (INT8,)
        assert "baseline に対する primary_score の悪化" in _reason_for(decision, INT8)

    def test_rejects_a_candidate_whose_coverage_drifts_from_the_baseline(self):
        # どちらも基準 0.683 から 0.023 で絶対 gate 内。両者の差 0.046 だけが超える
        decision = _decide([_float32(coverage=0.660), _int8(coverage=0.706)])

        assert decision.promoted_candidate_id == FLOAT32
        assert _rejected_ids(decision) == (INT8,)
        assert "baseline に対する coverage の差" in _reason_for(decision, INT8)

    def test_rejects_a_candidate_calibrated_on_a_split_other_than_train(self):
        decision = _decide(
            [
                _float32(),
                _int8(
                    quantization=attrs.evolve(
                        QUANTIZATION, calibration_split="validation"
                    )
                ),
            ]
        )

        assert decision.promoted_candidate_id == FLOAT32
        assert _rejected_ids(decision) == (INT8,)

    def test_rejects_a_candidate_that_failed_export_parity(self):
        decision = _decide([_float32(), _int8(export_parity_passed=False)])

        assert decision.promoted_candidate_id == FLOAT32
        assert _rejected_ids(decision) == (INT8,)


class TestGateValidation:
    """Gate の閾値そのものの検証.

    後段の分岐が前段を隠すので、1 ケースにつき 1 分岐だけが発火する値を選ぶ。
    """

    def test_accepts_the_shipping_gates(self):
        assert ACCURACY_GATE.validate() is None
        assert LATENCY_GATE.validate() is None

    @pytest.mark.parametrize(
        "gate",
        [
            pytest.param(
                attrs.evolve(ACCURACY_GATE, maximum_coverage_difference=-0.01),
                id="negative-threshold",
            ),
            pytest.param(
                attrs.evolve(
                    ACCURACY_GATE, maximum_primary_score_regression=float("nan")
                ),
                id="non-finite-threshold",
            ),
            # 有限かつ 0 以上なので、範囲の検査だけが発火する
            pytest.param(
                attrs.evolve(ACCURACY_GATE, reference_coverage=1.5),
                id="coverage-above-one",
            ),
            pytest.param(
                attrs.evolve(ACCURACY_GATE, reference_coverage=0.0),
                id="coverage-at-zero",
            ),
            # 0.0 は「0 以上の有限値」を通るので、正値の検査だけが発火する
            pytest.param(
                attrs.evolve(ACCURACY_GATE, maximum_primary_score=0.0),
                id="primary-score-gate-at-zero",
            ),
        ],
    )
    def test_reports_a_malformed_threshold(self, gate: AccuracyGate):
        assert gate.validate() is not None

    @pytest.mark.parametrize(
        "gate",
        [
            pytest.param(
                attrs.evolve(LATENCY_GATE, maximum_p95_seconds=0.0), id="p95-at-zero"
            ),
            pytest.param(
                attrs.evolve(LATENCY_GATE, negligible_difference_ratio=1.0),
                id="ratio-at-one",
            ),
            pytest.param(
                attrs.evolve(LATENCY_GATE, negligible_difference_ratio=float("nan")),
                id="non-finite-ratio",
            ),
        ],
    )
    def test_reports_a_malformed_latency_threshold(self, gate: LatencyGate):
        assert gate.validate() is not None


class TestEvidenceValidation:
    """精度と latency の実測値そのものの検証."""

    def test_accepts_the_measured_evidence(self):
        assert _accuracy().validate() is None
        assert _latency(0.5).validate() is None

    @pytest.mark.parametrize(
        "evidence",
        [
            pytest.param(_accuracy(primary_score=-0.1), id="negative-primary-score"),
            pytest.param(
                _accuracy(primary_score=float("inf")), id="non-finite-primary-score"
            ),
            pytest.param(_accuracy(coverage=1.5), id="coverage-above-one"),
            pytest.param(_accuracy(split=""), id="empty-evaluated-split"),
        ],
    )
    def test_reports_malformed_accuracy(self, evidence: AccuracyEvidence):
        assert evidence.validate() is not None

    @pytest.mark.parametrize(
        "evidence",
        [
            pytest.param(
                attrs.evolve(_latency(0.5), device_label=""), id="empty-device-label"
            ),
            pytest.param(
                attrs.evolve(_latency(0.5), worst_p95_seconds=0.0), id="p95-at-zero"
            ),
            pytest.param(
                attrs.evolve(_latency(0.5), cold_start_seconds=float("inf")),
                id="non-finite-cold-start",
            ),
        ],
    )
    def test_reports_malformed_latency(self, evidence: LatencyEvidence):
        assert evidence.validate() is not None


class TestCandidateValidation:
    """候補そのものの記述の検証."""

    def test_accepts_the_measured_candidates(self):
        assert _float32().validate() is None
        assert _int8().validate() is None

    @pytest.mark.parametrize(
        "candidate",
        [
            pytest.param(_float32(candidate_id=""), id="empty-candidate-id"),
            pytest.param(_float32(artifact_bytes=0), id="non-positive-artifact-bytes"),
            pytest.param(
                attrs.evolve(_float32(), precision="float64"), id="unknown-precision"
            ),
            pytest.param(
                attrs.evolve(_float32(), accuracy=_accuracy(split="")),
                id="malformed-accuracy",
            ),
            pytest.param(
                attrs.evolve(
                    _float32(), latency=attrs.evolve(_latency(0.5), device_label="")
                ),
                id="malformed-latency",
            ),
            pytest.param(
                _int8(quantization=attrs.evolve(QUANTIZATION, method="")),
                id="malformed-quantization",
            ),
        ],
    )
    def test_reports_a_malformed_candidate(self, candidate: PromotionCandidate):
        assert candidate.validate() is not None


class TestDecideCannotJudge:
    """比較そのものが成立しない入力."""

    @pytest.mark.parametrize(
        "candidates",
        [
            pytest.param([], id="no-candidates"),
            pytest.param(
                [_float32(), _float32()],
                id="duplicate-candidate-id",
            ),
            pytest.param(
                [_float32(), _int8(split="test")],
                id="mismatched-evaluated-split",
            ),
            pytest.param([_int8()], id="no-float32-baseline"),
            pytest.param(
                [_float32(artifact_bytes=-1)],
                id="malformed-candidate",
            ),
        ],
    )
    def test_reports_why_the_comparison_does_not_hold(
        self, candidates: Sequence[PromotionCandidate]
    ):
        decision, error = PromotionDecision.decide(
            candidates, accuracy_gate=ACCURACY_GATE, latency_gate=LATENCY_GATE
        )

        assert decision is None
        assert error is not None

    def test_reports_an_empty_candidate_list(self):
        # 空だと後段の evaluated_split 一致検査も落ちるので、理由文まで見る
        decision, error = PromotionDecision.decide(
            [], accuracy_gate=ACCURACY_GATE, latency_gate=LATENCY_GATE
        )

        assert decision is None
        assert error is not None
        assert "候補が 1 個もありません" in error

    def test_reports_a_malformed_accuracy_gate(self):
        decision, error = PromotionDecision.decide(
            [_float32()],
            accuracy_gate=attrs.evolve(ACCURACY_GATE, maximum_primary_score=-1.0),
            latency_gate=LATENCY_GATE,
        )

        assert decision is None
        assert error is not None

    def test_reports_a_malformed_latency_gate(self):
        decision, error = PromotionDecision.decide(
            [_float32()],
            accuracy_gate=ACCURACY_GATE,
            latency_gate=attrs.evolve(LATENCY_GATE, maximum_p95_seconds=0.0),
        )

        assert decision is None
        assert error is not None


class TestSaveAndLoad:
    """判定結果を document として残す."""

    def test_round_trips_through_a_document(self, tmp_path: Path):
        decision = _decide([_float32(), _int8(p95_seconds=None)])

        decision.save(tmp_path / "promotion.json")
        loaded, error = PromotionDecision.load(tmp_path / "promotion.json")

        assert error is None
        assert loaded == decision

    def test_reports_a_missing_document(self, tmp_path: Path):
        loaded, error = PromotionDecision.load(tmp_path / "absent.json")

        assert loaded is None
        assert error is not None
