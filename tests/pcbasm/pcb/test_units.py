"""pcbasm.pcb.units のテスト."""

import pytest

from pcbasm.pcb.units import (
    KICAD_COORD_MAX_NM,
    KICAD_MAX_COORD_MM,
    KicadCoordinateError,
    from_mm,
    is_kicad_length,
    to_mm,
    vector,
)


class TestUnitConversion:
    @pytest.mark.parametrize(
        ("mm", "nm"), [(0.0, 0), (1.0, 1_000_000), (-2.5, -2_500_000)]
    )
    def test_from_mm_round_trips_through_to_mm(self, mm: float, nm: int):
        assert from_mm(mm) == nm
        assert to_mm(nm) == pytest.approx(mm)


class TestIsKicadLength:
    @pytest.mark.parametrize("value", [1e-6, 1.0, KICAD_MAX_COORD_MM])
    def test_accepts_nonzero_lengths_within_range(self, value: float):
        assert is_kicad_length(value) is True

    @pytest.mark.parametrize("value", [0.0, 1e-7, -1.0, KICAD_MAX_COORD_MM + 1e-3])
    def test_rejects_zero_negative_and_out_of_range(self, value: float):
        assert is_kicad_length(value) is False

    def test_custom_maximum(self):
        assert is_kicad_length(2.0, maximum_mm=1.0) is False


class TestVector:
    def test_converts_mm_to_internal_units(self):
        v = vector(1.5, -0.25)
        assert (v.x, v.y) == (1_500_000, -250_000)

    def test_out_of_range_coordinate_is_an_error(self):
        with pytest.raises(KicadCoordinateError, match="座標範囲"):
            vector(to_mm(KICAD_COORD_MAX_NM) + 1.0, 0.0)
