"""pcbasm.pasting.params のテスト（実 PasteDispenser config を使う）."""

import attrs
import pytest

from pcbasm.config import Machine
from pcbasm.pasting.params import (
    PASTE_PARAM_FIELDS,
    PASTE_PARAM_NAMES,
    DispenseSettings,
    PasteParams,
    PasteParamsPatch,
    validate_field_names,
    validate_param_values,
)
from tests.helpers import TESTING_DATA_DIR

TESTING_MACHINE_TOML = TESTING_DATA_DIR / "machine.toml"


@pytest.fixture
def config():
    return Machine(TESTING_MACHINE_TOML).paste_dispenser


def _params(**overrides: object) -> PasteParams:
    values: dict = {
        "dispense_mode": "auto",
        "line_direction": "unconstrained",
        "ul_per_mm2": 0.1,
        "paste_height": 0.05,
        "prime_extra_delay": 0.8,
        "bead_width_factor": 1.0,
        "overlap": 0.0,
        "boundary_margin": 0.0,
    }
    values.update(overrides)
    return PasteParams(**values)


class TestPasteParamFields:
    def test_field_metadata_matches_attrs_fields_in_order(self):
        assert PASTE_PARAM_NAMES == tuple(f.name for f in attrs.fields(PasteParams))
        assert PASTE_PARAM_NAMES == tuple(
            f.name for f in attrs.fields(PasteParamsPatch)
        )
        # WebUI の widget 種別も field メタデータが単一ソース
        by_name = {field.name: field for field in PASTE_PARAM_FIELDS}
        assert by_name["paste_height"].kind == "height"
        assert by_name["ul_per_mm2"].kind == "number"


class TestPasteParams:
    def test_patched_overrides_only_non_none_fields(self):
        params = _params()

        patched = params.patched(PasteParamsPatch(ul_per_mm2=0.5, paste_height="auto"))

        assert patched.ul_per_mm2 == pytest.approx(0.5)
        assert patched.paste_height == "auto"
        assert patched.prime_extra_delay == pytest.approx(0.8)
        assert params.ul_per_mm2 == pytest.approx(0.1)  # 元は不変

    @pytest.mark.parametrize(
        ("paste_height", "ul_per_mm2", "expected"),
        [(0.3, 0.1, 0.3), ("auto", 0.12, 0.12)],
    )
    def test_paste_height_mm_resolves_auto(self, paste_height, ul_per_mm2, expected):
        params = _params(paste_height=paste_height, ul_per_mm2=ul_per_mm2)

        assert params.paste_height_mm == pytest.approx(expected)


class TestPasteParamsPatch:
    def test_from_dict_and_to_dict_round_trip_sparse_values(self):
        patch = PasteParamsPatch.from_dict({"ul_per_mm2": 0.5, "dispense_mode": "line"})

        assert patch.to_dict() == {"dispense_mode": "line", "ul_per_mm2": 0.5}
        assert patch.paste_height is None
        assert patch.is_empty is False

    def test_updated_applies_values_then_clears(self):
        patch = PasteParamsPatch(ul_per_mm2=0.5, overlap=0.2)

        updated = patch.updated({"paste_height": "auto"}, clear=["ul_per_mm2"])

        assert updated.to_dict() == {"paste_height": "auto", "overlap": 0.2}

    def test_empty_patch(self):
        assert PasteParamsPatch().is_empty is True
        assert PasteParamsPatch().to_dict() == {}


class TestValidateParamValues:
    def test_valid_values_return_none(self):
        assert (
            validate_param_values(
                {"ul_per_mm2": 0.5, "dispense_mode": "line", "paste_height": "auto"}
            )
            is None
        )

    def test_unknown_field_is_rejected(self):
        message = validate_param_values({"bogus": 1.0})

        assert message is not None
        assert "bogus" in message

    @pytest.mark.parametrize(
        "values",
        [
            {"dispense_mode": "spray"},
            {"line_direction": "sideways"},
            {"paste_height": 0.0},
            {"paste_height": -1.0},
            {"paste_height": float("nan")},
            {"ul_per_mm2": True},
            {"prime_extra_delay": "fast"},
            {"overlap": float("inf")},
            {"overlap": 1.0},
            {"overlap": -0.1},
            {"ul_per_mm2": 0.0},
            {"bead_width_factor": 0.0},
            {"prime_extra_delay": -1.0},
            {"boundary_margin": -0.01},
        ],
    )
    def test_invalid_values_are_rejected(self, values: dict):
        assert validate_param_values(values) is not None

    def test_validate_field_names_separates_unknown(self):
        assert validate_field_names(["bogus"]) is not None
        assert validate_field_names(["ul_per_mm2", "prime_extra_delay"]) is None


class TestDispenseSettings:
    def test_from_config_uses_effective_retract_rate(self, config):
        settings = DispenseSettings.from_config(config)

        assert settings.retract_rate == pytest.approx(config.effective_retract_rate)
        assert settings.lift_height == pytest.approx(config.lift_height)

    def test_lift_height_override(self, config):
        assert DispenseSettings.from_config(config, lift_height=5.0).lift_height == 5.0
