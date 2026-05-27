"""FlowCalibration のテスト."""

import attrs
import pytest

from pcb_assembly.pasting.calibration import FlowCalibration


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
