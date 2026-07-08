"""FlowCalibration / FlowCalibrationSet のテスト."""

import statistics

import attrs
import pytest

from pcbasm.pasting.calibration import (
    FlowCalibration,
    FlowCalibrationSet,
    MassFlowCalibration,
    MassFlowEstimate,
    estimate_mass_flow,
)


class TestMassFlowCalibration:
    """MassFlowCalibration クラスのテスト."""

    def test_rotations_per_ul_from_mass_and_density(self):
        calibration = MassFlowCalibration(
            rotations=5.0,
            mass_mg=10.0,
            density_mg_per_ul=3.78,
        )

        assert calibration.volume_ul == pytest.approx(10.0 / 3.78)
        assert calibration.rotations_per_ul == pytest.approx(5.0 * 3.78 / 10.0)

    def test_dispense_rate_for(self):
        # rotations=5, mass=10, density=3.78 → rotations_per_ul = 1.89
        calibration = MassFlowCalibration(
            rotations=5.0,
            mass_mg=10.0,
            density_mg_per_ul=3.78,
        )

        assert calibration.rotations_per_ul == pytest.approx(1.89)
        assert calibration.dispense_rate_for(0.5) == pytest.approx(0.5 / 1.89)

    def test_dispense_accel_for(self):
        # rotations=5, mass=10, density=3.78 → rotations_per_ul = 1.89
        calibration = MassFlowCalibration(
            rotations=5.0,
            mass_mg=10.0,
            density_mg_per_ul=3.78,
        )

        assert calibration.rotations_per_ul == pytest.approx(1.89)
        assert calibration.dispense_accel_for(0.5) == pytest.approx(0.5 / 1.89)

    @pytest.mark.parametrize(
        ("rotations", "mass_mg", "density_mg_per_ul"),
        [
            (0.0, 10.0, 3.78),
            (5.0, 0.0, 3.78),
            (5.0, 10.0, 0.0),
        ],
    )
    def test_values_must_be_positive(
        self, rotations: float, mass_mg: float, density_mg_per_ul: float
    ):
        with pytest.raises(ValueError):  # noqa: PT011 - attrs の詳細文言は固定しない
            MassFlowCalibration(
                rotations=rotations,
                mass_mg=mass_mg,
                density_mg_per_ul=density_mg_per_ul,
            )


class TestFlowCalibration:
    """FlowCalibration クラスのテスト."""

    @pytest.mark.parametrize(
        ("rotations", "mass_mg", "specific_gravity", "expected"),
        [
            # 一般的なソルダーペースト: 10rev × 4.4 / 200mg = 0.22
            (10.0, 200.0, 4.4, 0.22),
            # 単位確認: 1rev で 1g (=1000mg) の水 (SG=1) → 1mL → 0.001 rev/μL
            (1.0, 1000.0, 1.0, 0.001),
            # 中間ケース: 10rev × 2.0 / 1000mg = 0.02
            (10.0, 1000.0, 2.0, 0.02),
        ],
    )
    def test_rotations_per_ul(
        self,
        rotations: float,
        mass_mg: float,
        specific_gravity: float,
        expected: float,
    ):
        fc = FlowCalibration(
            rotations=rotations, mass_mg=mass_mg, specific_gravity=specific_gravity
        )
        assert fc.rotations_per_ul == pytest.approx(expected)

    def test_frozen(self):
        fc = FlowCalibration(rotations=10.0, mass_mg=200.0, specific_gravity=4.4)
        with pytest.raises(attrs.exceptions.FrozenInstanceError):
            fc.mass_mg = 300.0  # type: ignore[misc]


class TestFlowCalibrationSet:
    """FlowCalibrationSet クラスのテスト（複数回計測の集約）."""

    @pytest.mark.parametrize(
        ("rotations", "masses", "specific_gravity", "expected"),
        [
            # 1 回計測は単一 FlowCalibration と同じ: 10rev × 4.4 / 200mg = 0.22
            (10.0, (200.0,), 4.4, 0.22),
            # 同値 3 回 → 平均 200mg → 0.22
            (10.0, (200.0, 200.0, 200.0), 4.4, 0.22),
            # 異なる質量: 平均 (100+200)/2 = 150mg → 10 × 2.0 / 150 = 0.13333...
            (10.0, (100.0, 200.0), 2.0, 10.0 * 2.0 / 150.0),
        ],
    )
    def test_rotations_per_ul_uses_mean_mass(
        self,
        rotations: float,
        masses: tuple[float, ...],
        specific_gravity: float,
        expected: float,
    ):
        fcs = FlowCalibrationSet(
            rotations=rotations, masses_mg=masses, specific_gravity=specific_gravity
        )
        assert fcs.rotations_per_ul == pytest.approx(expected)

    def test_mean_mass_mg(self):
        fcs = FlowCalibrationSet(
            rotations=10.0, masses_mg=(100.0, 200.0, 300.0), specific_gravity=2.0
        )
        assert fcs.mean_mass_mg == pytest.approx(200.0)

    def test_per_measurement(self):
        fcs = FlowCalibrationSet(
            rotations=10.0, masses_mg=(100.0, 200.0), specific_gravity=2.0
        )
        per = fcs.per_measurement
        assert [c.mass_mg for c in per] == [100.0, 200.0]
        assert all(c.rotations == 10.0 and c.specific_gravity == 2.0 for c in per)
        assert per[0].rotations_per_ul == pytest.approx(0.2)
        assert per[1].rotations_per_ul == pytest.approx(0.1)

    def test_stdev_matches_sample_stdev(self):
        fcs = FlowCalibrationSet(
            rotations=10.0, masses_mg=(100.0, 200.0), specific_gravity=2.0
        )
        expected = statistics.stdev([0.2, 0.1])
        assert fcs.stdev_rotations_per_ul == pytest.approx(expected)

    @pytest.mark.parametrize(
        "masses",
        [
            (200.0,),  # 1 回計測 → ばらつきなし
            (200.0, 200.0, 200.0),  # 全値同一 → ばらつきなし
        ],
    )
    def test_stdev_zero(self, masses: tuple[float, ...]):
        fcs = FlowCalibrationSet(rotations=10.0, masses_mg=masses, specific_gravity=4.4)
        assert fcs.stdev_rotations_per_ul == 0.0

    def test_single_measurement_matches_flow_calibration(self):
        single = FlowCalibration(rotations=10.0, mass_mg=200.0, specific_gravity=4.4)
        fcs = FlowCalibrationSet(
            rotations=10.0, masses_mg=(200.0,), specific_gravity=4.4
        )
        assert fcs.rotations_per_ul == pytest.approx(single.rotations_per_ul)

    def test_dispense_rate_for_rotation_rate(self):
        fcs = FlowCalibrationSet(
            rotations=50.0, masses_mg=(1000.0,), specific_gravity=1.0
        )

        assert fcs.dispense_rate_for(2.5) == pytest.approx(50.0)

    def test_dispense_accel_for_rotation_accel(self):
        fcs = FlowCalibrationSet(
            rotations=50.0, masses_mg=(1000.0,), specific_gravity=1.0
        )

        assert fcs.dispense_accel_for(25.0) == pytest.approx(500.0)

    def test_rescaled_dispense_accel(self):
        # この計測の rotations_per_ul = 0.05 rev/μL。旧 (accel=2.0, rpu=0.1) を
        # 採用する → rotation_accel = 2.0*0.1 = 0.2 rev/s² を 0.05 で割り 4.0 μL/s²。
        fcs = FlowCalibrationSet(
            rotations=50.0, masses_mg=(1000.0,), specific_gravity=1.0
        )

        assert fcs.rescaled_dispense_accel(
            previous_dispense_accel=2.0, previous_rotations_per_ul=0.1
        ) == pytest.approx(4.0)

    def test_rescaled_dispense_accel_preserves_rotation_accel(self):
        # 再算出後の (accel * rpu) は旧 (accel * rpu) と一致する（回転加速度保存）。
        fcs = FlowCalibrationSet(
            rotations=50.0, masses_mg=(1000.0,), specific_gravity=1.0
        )
        new_accel = fcs.rescaled_dispense_accel(
            previous_dispense_accel=2.0, previous_rotations_per_ul=0.1
        )

        assert new_accel * fcs.rotations_per_ul == pytest.approx(2.0 * 0.1)

    def test_masses_accepts_list(self):
        fcs = FlowCalibrationSet(
            rotations=10.0, masses_mg=[200.0, 200.0], specific_gravity=4.4
        )
        assert fcs.masses_mg == (200.0, 200.0)

    def test_empty_masses_raises(self):
        with pytest.raises(ValueError):  # noqa: PT011 - attrs min_len のメッセージは固定でない
            FlowCalibrationSet(rotations=10.0, masses_mg=(), specific_gravity=4.4)

    def test_frozen(self):
        fcs = FlowCalibrationSet(
            rotations=10.0, masses_mg=(200.0,), specific_gravity=4.4
        )
        with pytest.raises(attrs.exceptions.FrozenInstanceError):
            fcs.specific_gravity = 3.0  # type: ignore[misc]


class TestEstimateMassFlow:
    """estimate_mass_flow は部分入力から導出可能な値だけを丸めて返す。"""

    def test_all_positive_inputs_return_full_estimate(self):
        # mass=10, rotations=5, density=3.78 → rotations_per_ul = 1.89。
        # 返却値は小数第 6 位へ丸め済み。
        estimate = estimate_mass_flow(
            mass_mg=10.0, rotations=5.0, rate=0.5, accel=0.5, density_mg_per_ul=3.78
        )

        assert estimate.volume_ul == round(10.0 / 3.78, 6)
        assert estimate.rotations_per_ul == round(1.89, 6)
        assert estimate.max_dispense_rate == round(0.5 / 1.89, 6)
        assert estimate.dispense_accel == round(0.5 / 1.89, 6)

    def test_values_are_rounded_to_six_digits(self):
        estimate = estimate_mass_flow(
            mass_mg=10.0, rotations=5.0, rate=0.5, accel=0.5, density_mg_per_ul=3.78
        )

        assert estimate.volume_ul == round(10.0 / 3.78, 6)
        assert estimate.rotations_per_ul == round(5.0 * 3.78 / 10.0, 6)
        assert estimate.max_dispense_rate == round(0.5 / 1.89, 6)
        assert estimate.dispense_accel == round(0.5 / 1.89, 6)

    def test_arithmetic_delegates_to_mass_flow_calibration(self):
        calib = MassFlowCalibration(rotations=5.0, mass_mg=10.0, density_mg_per_ul=3.78)

        estimate = estimate_mass_flow(
            mass_mg=10.0, rotations=5.0, rate=0.5, accel=0.7, density_mg_per_ul=3.78
        )

        assert estimate.rotations_per_ul == round(calib.rotations_per_ul, 6)
        assert estimate.max_dispense_rate == round(calib.dispense_rate_for(0.5), 6)
        assert estimate.dispense_accel == round(calib.dispense_accel_for(0.7), 6)

    def test_zero_rotations_nulls_rotation_derived_values(self):
        estimate = estimate_mass_flow(
            mass_mg=10.0, rotations=0.0, rate=0.5, accel=0.5, density_mg_per_ul=3.78
        )

        assert estimate.volume_ul == pytest.approx(10.0 / 3.78)
        assert estimate.rotations_per_ul is None
        assert estimate.max_dispense_rate is None
        assert estimate.dispense_accel is None

    @pytest.mark.parametrize(
        ("mass_mg", "density_mg_per_ul"),
        [(0.0, 3.78), (10.0, 0.0), (-1.0, 3.78)],
    )
    def test_non_positive_mass_or_density_nulls_everything(
        self, mass_mg: float, density_mg_per_ul: float
    ):
        estimate = estimate_mass_flow(
            mass_mg=mass_mg,
            rotations=5.0,
            rate=0.5,
            accel=0.5,
            density_mg_per_ul=density_mg_per_ul,
        )

        assert estimate == MassFlowEstimate(
            volume_ul=None,
            rotations_per_ul=None,
            max_dispense_rate=None,
            dispense_accel=None,
        )

    def test_zero_rate_nulls_only_dispense_rate(self):
        estimate = estimate_mass_flow(
            mass_mg=10.0, rotations=5.0, rate=0.0, accel=0.5, density_mg_per_ul=3.78
        )

        assert estimate.rotations_per_ul == round(1.89, 6)
        assert estimate.max_dispense_rate is None
        assert estimate.dispense_accel == round(0.5 / 1.89, 6)

    def test_zero_accel_nulls_only_dispense_accel(self):
        estimate = estimate_mass_flow(
            mass_mg=10.0, rotations=5.0, rate=0.5, accel=0.0, density_mg_per_ul=3.78
        )

        assert estimate.rotations_per_ul == round(1.89, 6)
        assert estimate.max_dispense_rate == round(0.5 / 1.89, 6)
        assert estimate.dispense_accel is None
