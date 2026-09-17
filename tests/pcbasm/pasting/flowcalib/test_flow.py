"""flowcalib.flow（質量計測 → 係数、掃引の量計算、収束判定）のテスト."""

import math
import statistics

import pytest

from pcbasm.pasting.flowcalib.flow import (
    CONVERGENCE_REL_TOL,
    DispenseRateCalibration,
    FlowCalibration,
    MassFlowEstimate,
    RateMeasurement,
    RotationsPerUlRound,
    rate_sweep_amount_ul,
    slot_area,
)


class TestFlowCalibration:
    """質量計測（1 回または複数回）から rotations_per_ul を算出する."""

    @pytest.mark.parametrize(
        ("rotations", "masses", "density", "expected"),
        [
            # 一般的なソルダーペースト: 10rev × 4.4 / 200mg = 0.22
            (10.0, (200.0,), 4.4, 0.22),
            # 単位確認: 1rev で 1g (=1000mg) の水 (1 mg/μL) → 1mL → 0.001 rev/μL
            (1.0, (1000.0,), 1.0, 0.001),
            # 同値 3 回 → 平均 200mg → 0.22
            (10.0, (200.0, 200.0, 200.0), 4.4, 0.22),
            # 異なる質量: 平均 (100+200)/2 = 150mg → 10 × 2.0 / 150
            (10.0, (100.0, 200.0), 2.0, 10.0 * 2.0 / 150.0),
        ],
    )
    def test_rotations_per_ul_uses_mean_mass(
        self, rotations, masses, density, expected
    ):
        calib = FlowCalibration(
            rotations=rotations, masses_mg=masses, density_mg_per_ul=density
        )

        assert calib.rotations_per_ul == pytest.approx(expected)

    def test_volume_and_mean_mass(self):
        calib = FlowCalibration(
            rotations=10.0, masses_mg=(100.0, 200.0, 300.0), density_mg_per_ul=2.0
        )

        assert calib.mean_mass_mg == pytest.approx(200.0)
        assert calib.volume_ul == pytest.approx(100.0)

    def test_stdev_matches_sample_stdev_of_per_measurement_values(self):
        calib = FlowCalibration(
            rotations=10.0, masses_mg=(100.0, 200.0), density_mg_per_ul=2.0
        )

        assert calib.stdev_rotations_per_ul == pytest.approx(
            statistics.stdev([0.2, 0.1])
        )

    @pytest.mark.parametrize("masses", [(200.0,), (200.0, 200.0, 200.0)])
    def test_stdev_zero_for_single_or_identical_measurements(self, masses):
        calib = FlowCalibration(rotations=10.0, masses_mg=masses, density_mg_per_ul=4.4)

        assert calib.stdev_rotations_per_ul == 0.0

    def test_dispense_rate_and_accel_for(self):
        # rotations_per_ul = 50 / 1000 = 0.05
        calib = FlowCalibration(
            rotations=50.0, masses_mg=(1000.0,), density_mg_per_ul=1.0
        )

        assert calib.dispense_rate_for(2.5) == pytest.approx(50.0)
        assert calib.dispense_accel_for(25.0) == pytest.approx(500.0)

    def test_rescaled_dispense_accel_preserves_rotation_accel(self):
        # 新 rpu = 0.05。旧 (accel=2.0, rpu=0.1) → 回転加速度 0.2 rev/s² → 4.0 μL/s²
        calib = FlowCalibration(
            rotations=50.0, masses_mg=(1000.0,), density_mg_per_ul=1.0
        )

        new_accel = calib.rescaled_dispense_accel(2.0, 0.1)

        assert new_accel == pytest.approx(4.0)
        assert new_accel * calib.rotations_per_ul == pytest.approx(2.0 * 0.1)

    @pytest.mark.parametrize(
        ("rotations", "masses", "density"),
        [
            (0.0, (10.0,), 3.78),
            (5.0, (), 3.78),
            (5.0, (0.0,), 3.78),
            (5.0, (10.0,), 0.0),
        ],
    )
    def test_non_positive_inputs_are_rejected(self, rotations, masses, density):
        with pytest.raises(ValueError):  # noqa: PT011 - attrs の詳細文言は固定しない
            FlowCalibration(
                rotations=rotations, masses_mg=masses, density_mg_per_ul=density
            )


class TestMassFlowEstimate:
    """MassFlowEstimate.estimate は部分入力から導出可能な値だけを丸めて返す."""

    def test_arithmetic_matches_flow_calibration(self):
        calib = FlowCalibration(
            rotations=5.0, masses_mg=(10.0,), density_mg_per_ul=3.78
        )

        estimate = MassFlowEstimate.estimate(
            mass_mg=10.0, rotations=5.0, rate=0.5, accel=0.7, density_mg_per_ul=3.78
        )

        assert estimate.volume_ul == round(10.0 / 3.78, 6)
        assert estimate.rotations_per_ul == round(calib.rotations_per_ul, 6)
        assert estimate.max_dispense_rate == round(calib.dispense_rate_for(0.5), 6)
        assert estimate.dispense_accel == round(calib.dispense_accel_for(0.7), 6)

    @pytest.mark.parametrize(
        "value", [0.0, -1.0, float("nan"), float("inf"), -float("inf")]
    )
    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("mass_mg", MassFlowEstimate(None, None, None, None)),
            ("density_mg_per_ul", MassFlowEstimate(None, None, None, None)),
            ("rotations", MassFlowEstimate(2.0, None, None, None)),
            ("rate", MassFlowEstimate(2.0, 8.0, None, 0.25)),
            ("accel", MassFlowEstimate(2.0, 8.0, 1.0, None)),
        ],
    )
    def test_unusable_input_nulls_only_dependent_values(self, field, value, expected):
        inputs = dict(
            mass_mg=8.0, rotations=16.0, rate=8.0, accel=2.0, density_mg_per_ul=4.0
        )
        inputs[field] = value

        assert MassFlowEstimate.estimate(**inputs) == expected

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            pytest.param(
                {"mass_mg": 1e308, "density_mg_per_ul": 1e-308},
                MassFlowEstimate(None, None, None, None),
                id="volume-overflow",
            ),
            pytest.param(
                {"mass_mg": 1e-308, "density_mg_per_ul": 1e308},
                MassFlowEstimate(None, None, None, None),
                id="volume-underflow",
            ),
            pytest.param(
                {"mass_mg": 1e-308},
                MassFlowEstimate(None, None, None, None),
                id="coefficient-overflow",
            ),
            pytest.param(
                {"mass_mg": 1e308, "density_mg_per_ul": 1.0, "rotations": 1e-308},
                MassFlowEstimate(1e308, None, None, None),
                id="coefficient-underflow",
            ),
            pytest.param(
                {"rotations": 1e-308},
                MassFlowEstimate(2.0, None, None, None),
                id="rate-and-accel-overflow",
            ),
            pytest.param(
                {"rate": 1e-8, "accel": 1e-8},
                MassFlowEstimate(2.0, 8.0, None, None),
                id="rounding-would-yield-zero",
            ),
        ],
    )
    def test_unrepresentable_results_are_not_offered_for_application(
        self, overrides, expected
    ):
        inputs = dict(
            mass_mg=8.0, rotations=16.0, rate=8.0, accel=2.0, density_mg_per_ul=4.0
        )

        assert MassFlowEstimate.estimate(**(inputs | overrides)) == expected


class TestSweepAmounts:
    """線 1 本の面積と吐出量の導出."""

    def test_slot_area_is_rectangle_plus_circle_cap(self):
        assert slot_area(10.0, 0.4) == pytest.approx(10.0 * 0.4 + math.pi * 0.2**2)

    def test_slot_area_zero_length_is_full_circle(self):
        assert slot_area(0.0, 0.6) == pytest.approx(math.pi * 0.3**2)

    def test_rate_sweep_amount_scales_linearly_with_rate(self):
        # rate=0.5, L=10, v=0.8 → 0.5*10/0.8 = 6.25 uL
        assert rate_sweep_amount_ul(0.5, 10.0, 0.8) == pytest.approx(6.25)
        assert rate_sweep_amount_ul(1.0, 10.0, 0.8) == pytest.approx(12.5)


class TestDispenseRateCalibration:
    """② 吐出効率の落ち検出."""

    @staticmethod
    def _calib(efficiencies, **kwargs):
        # commanded_ul=10 固定で、欲しい efficiency になる measured_ul を与える
        measurements = [
            RateMeasurement(
                rate=float(i + 1), measured_ul=eff * 10.0, commanded_ul=10.0
            )
            for i, eff in enumerate(efficiencies)
        ]
        return DispenseRateCalibration(measurements=measurements, **kwargs)

    def test_baseline_is_median_of_low_rate_points(self):
        calib = self._calib([1.0, 0.9, 0.95, 0.5], baseline_count=3)
        # 低レート 3 点 [1.0, 0.9, 0.95] の中央値 = 0.95
        assert calib.baseline_efficiency == pytest.approx(0.95)

    def test_detects_drop_returns_rate_before_drop(self):
        # baseline ~1.0、4 点目(rate=4)で 10% 超落ち → 直前 rate=3 が上限
        calib = self._calib([1.0, 1.0, 1.0, 0.85], drop_frac=0.10)
        assert calib.max_dispense_rate == pytest.approx(3.0)

    @pytest.mark.parametrize(
        "efficiencies",
        [
            [1.0, 0.98, 0.96, 0.95],  # 全域で baseline から 10% 以内
            [1.0, 1.0, 1.0, 1.0],
            [0.95, 1.0, 0.97, 0.99, 0.96],  # ノイズで上下するが落ちは無い
        ],
    )
    def test_no_drop_returns_none(self, efficiencies):
        assert self._calib(efficiencies, drop_frac=0.10).max_dispense_rate is None

    def test_drop_at_first_point_returns_none(self):
        # baseline=median([0.5,1.0])=0.75, threshold=0.675。1 点目(0.5)が既に閾値割れ
        calib = self._calib([0.5, 1.0], baseline_count=2, drop_frac=0.10)
        assert calib.max_dispense_rate is None

    def test_noisy_with_real_drop_detected(self):
        # baseline 中央値 ~0.97、最後で 0.80 に落ちる → 直前 rate=4
        calib = self._calib([0.95, 1.0, 0.97, 0.99, 0.80], drop_frac=0.10)
        assert calib.max_dispense_rate == pytest.approx(4.0)

    def test_empty_measurements_yield_none_without_raising(self):
        calib = DispenseRateCalibration(measurements=())

        assert calib.baseline_efficiency is None
        assert calib.max_dispense_rate is None


class TestRotationsPerUlRound:
    """① の 1 ラウンド評価と収束判定."""

    def test_round_from_mass_computes_new_rpu_and_rescaled_accel(self):
        # 10 本 × 0.5 uL × rpu 1.0 = 5 rev。10 mg / 3.78 → 2.6455 uL → rpu 1.89。
        # 回転加速度 10 × 1.0 = 10 rev/s² を新 rpu で割ると 5.291 uL/s²。
        round_ = RotationsPerUlRound.evaluate(
            mass_mg=10.0,
            line_count=10,
            amount_ul=0.5,
            previous_rotations_per_ul=1.0,
            previous_dispense_accel=10.0,
            density_mg_per_ul=3.78,
        )

        assert round_.rotations_used == pytest.approx(5.0)
        assert round_.previous == 1.0
        assert round_.computed == pytest.approx(1.89)
        assert round_.dispense_accel == pytest.approx(10.0 / 1.89)
        assert round_.relative_change == pytest.approx(0.89)

    @pytest.mark.parametrize(
        ("previous", "computed", "rel_tol", "expected"),
        [
            (2.0, 2.02, 0.05, True),
            (2.0, 2.5, 0.05, False),
            # relative_change (0.5/2.0) ちょうどの許容は inclusive (<=)
            (2.0, 2.5, 0.25, True),
            # rel_tol 省略時は CONVERGENCE_REL_TOL を使う。
            # 浮動小数の丸めで境界値が rel_tol を僅かに超えないよう半分の変化量にする。
            (1.0, 1.0 + CONVERGENCE_REL_TOL / 2, None, True),
            (1.0, 1.0 + 2 * CONVERGENCE_REL_TOL, None, False),
        ],
    )
    def test_converged_within_tolerance(self, previous, computed, rel_tol, expected):
        round_ = RotationsPerUlRound(
            previous, computed, dispense_accel=1.0, rotations_used=1.0
        )

        converged = (
            round_.converged() if rel_tol is None else round_.converged(rel_tol=rel_tol)
        )

        assert converged is expected

    @pytest.mark.parametrize(
        ("previous", "computed"),
        [(0.0, 2.0), (-1.0, 2.0), (2.0, 0.0), (2.0, -1.0)],
    )
    def test_non_positive_values_are_rejected(self, previous, computed):
        with pytest.raises(ValueError):  # noqa: PT011 - attrs の詳細文言は固定しない
            RotationsPerUlRound(
                previous, computed, dispense_accel=1.0, rotations_used=1.0
            )
