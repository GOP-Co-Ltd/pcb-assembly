"""pcbasm.pasting.persist のテスト（保存 JSON の encode / decode と legacy 移行）."""

import pytest

from pcbasm.pasting.params import PasteParams, PasteParamsPatch
from pcbasm.pasting.persist import (
    BOARD_SETTINGS_SCHEMA_VERSION,
    decode_board_settings,
    encode_board_settings,
)
from pcbasm.pasting.settings import LevelSetting, PasteSettingsModel


def _base() -> PasteParams:
    return PasteParams(
        dispense_mode="auto",
        line_direction="unconstrained",
        ul_per_mm2=0.1,
        paste_height=0.05,
        prime_extra_delay=0.8,
        bead_width_factor=1.0,
        overlap=0.0,
        boundary_margin=0.0,
    )


def _decode(doc: dict, base: PasteParams | None = None) -> PasteSettingsModel:
    decoded, error = decode_board_settings(doc, base=base or _base())
    assert error is None, error
    assert decoded is not None
    return decoded.model


class TestEncode:
    def test_doc_shape_omits_base_and_unset_purge_pad(self):
        model = PasteSettingsModel(
            base=_base(),
            levels=(
                LevelSetting(("L1", "0402"), patch=PasteParamsPatch(ul_per_mm2=0.08)),
            ),
        )

        doc = encode_board_settings(model, source_pcb="boards/a.kicad_pcb")

        assert doc == {
            "version": BOARD_SETTINGS_SCHEMA_VERSION,
            "source_pcb": "boards/a.kicad_pcb",
            "settings": {
                "levels": [
                    {
                        "key": ["L1", "0402"],
                        "enabled": None,
                        "override": {"ul_per_mm2": 0.08},
                    }
                ]
            },
        }

    def test_doc_includes_signature_and_purge_pad_when_set(self):
        model = PasteSettingsModel(base=_base(), initial_purge_pad_id="U1.1")

        doc = encode_board_settings(
            model, source_pcb="boards/a.kicad_pcb", board_signature="sig"
        )

        assert doc["board_signature"] == "sig"
        assert doc["settings"]["initial_purge_pad_id"] == "U1.1"


class TestRoundTrip:
    def test_levels_enum_and_auto_height_survive(self):
        model = PasteSettingsModel(
            base=_base(),
            initial_purge_pad_id="U1.9",
            levels=(
                LevelSetting(
                    ("L2", "U1"),
                    patch=PasteParamsPatch(
                        dispense_mode="area",
                        line_direction="inward",
                        paste_height="auto",
                    ),
                ),
                LevelSetting(("L4", "U1", "9"), enabled=True),
                LevelSetting(("L2", "R1"), enabled=False),
            ),
        )

        restored = _decode(encode_board_settings(model, source_pcb="a"))

        assert restored == model

    def test_unset_patch_fields_stay_inherited(self):
        model = PasteSettingsModel(
            base=_base(),
            levels=(
                LevelSetting(("L2", "U1"), patch=PasteParamsPatch(ul_per_mm2=0.5)),
            ),
        )

        restored = _decode(encode_board_settings(model, source_pcb="a"))
        patch = restored.levels[0].patch

        assert patch.ul_per_mm2 == pytest.approx(0.5)
        assert patch.prime_extra_delay is None
        assert patch.paste_height is None

    def test_decode_uses_current_base_not_saved_one(self):
        doc = encode_board_settings(PasteSettingsModel(base=_base()), source_pcb="a")
        new_base = PasteParams(**{**_base().to_dict(), "ul_per_mm2": 0.3})

        restored = _decode(doc, base=new_base)

        assert restored.base == new_base


class TestDecodeErrors:
    def test_unknown_version_is_an_error(self):
        decoded, error = decode_board_settings(
            {"version": 99, "settings": {"levels": []}}, base=_base()
        )

        assert decoded is None
        assert error is not None
        assert "schema version" in error

    def test_missing_settings_is_an_error(self):
        decoded, error = decode_board_settings({"version": 1}, base=_base())

        assert decoded is None
        assert error is not None

    def test_missing_levels_decodes_to_empty_model(self):
        restored = _decode({"version": 1, "settings": {}})

        assert restored.levels == ()
        assert restored.initial_purge_pad_id is None


class TestLegacyBaseMigration:
    """初期 v1 が settings 直下に持っていた base / base_enabled を L0 へ畳む."""

    def test_saved_base_equal_to_current_is_not_an_override(self):
        doc = {
            "version": 1,
            "settings": {"base": _base().to_dict(), "base_enabled": True, "levels": []},
        }

        assert _decode(doc).levels == ()

    def test_saved_base_difference_becomes_l0_patch(self):
        saved = {**_base().to_dict(), "ul_per_mm2": 0.25, "overlap": 0.3}
        doc = {
            "version": 1,
            "settings": {"base": saved, "base_enabled": True, "levels": []},
        }

        restored = _decode(doc)

        assert restored.levels == (
            LevelSetting(("L0",), patch=PasteParamsPatch(ul_per_mm2=0.25, overlap=0.3)),
        )

    def test_saved_base_enabled_false_becomes_l0_disable(self):
        doc = {
            "version": 1,
            "settings": {
                "base": _base().to_dict(),
                "base_enabled": False,
                "levels": [],
            },
        }

        restored = _decode(doc)

        assert restored.levels == (LevelSetting(("L0",), enabled=False),)

    def test_explicit_l0_level_takes_precedence_over_legacy_base(self):
        doc = {
            "version": 1,
            "settings": {
                "base": {**_base().to_dict(), "ul_per_mm2": 0.25},
                "levels": [
                    {"key": ["L0"], "enabled": None, "override": {"overlap": 0.4}}
                ],
            },
        }

        restored = _decode(doc)

        assert restored.levels == (
            LevelSetting(("L0",), patch=PasteParamsPatch(overlap=0.4)),
        )
