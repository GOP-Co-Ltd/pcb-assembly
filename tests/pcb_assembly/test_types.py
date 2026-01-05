import pytest

from pcb_assembly.types import Position


class TestPosition:
    """Position型のテスト."""

    @pytest.mark.parametrize(
        ("x", "y", "z"),
        [
            (1.0, 2.0, 3.0),
            (0.0, 0.0, 0.0),
            (-1.5, -2.5, -3.5),
        ],
    )
    def test_create(self, x: float, y: float, z: float) -> None:
        pos = Position(x=x, y=y, z=z)

        assert pos.x == x
        assert pos.y == y
        assert pos.z == z
