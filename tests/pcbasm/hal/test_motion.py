"""`pcbasm.hal.motion` の仕様テスト."""

import pytest

from pcbasm.hal import Axis, homing_order


class TestHomingOrder:
    """原点出しの軸の順（Z → X → Y、Z の原点が未なら Z を先に出す）."""

    @pytest.mark.parametrize(
        ("axes", "homed", "expected"),
        [
            ((), (), ("z", "x", "y")),
            ((), ("x", "y", "z"), ("z", "x", "y")),
            (("y", "x"), (), ("z", "x", "y")),
            (("y", "x"), ("z",), ("x", "y")),
            (("x",), (), ("z", "x")),
            (("x",), ("z",), ("x",)),
            (("z",), (), ("z",)),
            (("y", "z"), ("x", "y", "z"), ("z", "y")),
        ],
    )
    def test_orders_z_first_and_adds_unhomed_z(
        self,
        axes: tuple[Axis, ...],
        homed: tuple[Axis, ...],
        expected: tuple[Axis, ...],
    ):
        assert homing_order(axes, homed) == expected
