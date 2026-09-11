"""pcbasm.pasting.persist のテスト（保存 JSON の encode / decode と legacy 移行）."""

from datetime import datetime

import pytest

from pcbasm.geometry import Point2d
from pcbasm.pasting.params import PasteParams, PasteParamsPatch
from pcbasm.pasting.persist import (
    BOARD_SETTINGS_SCHEMA_VERSION,
    board_settings_export_filename,
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
    def test_doc_shape_omits_base_and_unset_purge_point(self):
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

    def test_doc_includes_signature_and_purge_point_when_set(self):
        model = PasteSettingsModel(base=_base(), initial_purge_point=Point2d(4.0, 5.0))

        doc = encode_board_settings(
            model, source_pcb="boards/a.kicad_pcb", board_signature="sig"
        )

        assert doc["board_signature"] == "sig"
        assert doc["settings"]["initial_purge_point"] == [4.0, 5.0]


class TestInitialPurgePoint:
    """パージ位置は ``settings.initial_purge_point`` に [x, y] で残す."""

    def test_doc_carries_the_point_when_set(self):
        model = PasteSettingsModel(
            base=_base(), initial_purge_point=Point2d(3.5, -1.25)
        )

        doc = encode_board_settings(model, source_pcb="a")

        assert doc["settings"]["initial_purge_point"] == [3.5, -1.25]

    def test_doc_omits_the_point_when_unset(self):
        doc = encode_board_settings(PasteSettingsModel(base=_base()), source_pcb="a")

        assert "initial_purge_point" not in doc["settings"]

    def test_round_trip_restores_the_point(self):
        model = PasteSettingsModel(
            base=_base(), initial_purge_point=Point2d(3.5, -1.25)
        )

        restored = _decode(encode_board_settings(model, source_pcb="a"))

        assert restored.initial_purge_point == Point2d(3.5, -1.25)

    def test_missing_point_decodes_to_none(self):
        restored = _decode({"version": 1, "settings": {}})

        assert restored.initial_purge_point is None

    @pytest.mark.parametrize(
        "value",
        [
            [1.0],
            [1.0, 2.0, 3.0],
            ["1.0", "2.0"],
            {"x": 1.0, "y": 2.0},
            [float("nan"), 0.0],
        ],
    )
    def test_malformed_point_decodes_to_none(self, value: object):
        # 壊れた保存内容でページを開けなくしない（pad id と同じく黙って捨てる）
        restored = _decode({"version": 1, "settings": {"initial_purge_point": value}})

        assert restored.initial_purge_point is None


class TestFlowCalibrationPoints:
    """流量キャリブレーションの測定位置は ``settings.flow_calibration_points`` に残す."""

    def test_doc_carries_the_points_in_order_when_set(self):
        model = PasteSettingsModel(
            base=_base(),
            flow_calibration_points=(Point2d(9.5, 2.25), Point2d(13.0, 2.25)),
        )

        doc = encode_board_settings(model, source_pcb="a")

        assert doc["settings"]["flow_calibration_points"] == [
            [9.5, 2.25],
            [13.0, 2.25],
        ]

    def test_doc_omits_the_points_when_unset(self):
        doc = encode_board_settings(PasteSettingsModel(base=_base()), source_pcb="a")

        assert "flow_calibration_points" not in doc["settings"]

    def test_round_trip_restores_the_points(self):
        model = PasteSettingsModel(
            base=_base(),
            flow_calibration_points=(Point2d(9.5, 2.25), Point2d(13.0, 2.25)),
        )

        restored = _decode(encode_board_settings(model, source_pcb="a"))

        assert restored.flow_calibration_points == (
            Point2d(9.5, 2.25),
            Point2d(13.0, 2.25),
        )

    def test_missing_points_decode_to_an_empty_set(self):
        restored = _decode({"version": 1, "settings": {}})

        assert restored.flow_calibration_points == ()

    def test_is_independent_of_the_initial_purge_point(self):
        model = PasteSettingsModel(
            base=_base(),
            initial_purge_point=Point2d(1.0, 2.0),
            flow_calibration_points=(Point2d(9.5, 2.25),),
        )

        restored = _decode(encode_board_settings(model, source_pcb="a"))

        assert restored.initial_purge_point == Point2d(1.0, 2.0)
        assert restored.flow_calibration_points == (Point2d(9.5, 2.25),)

    @pytest.mark.parametrize(
        "value",
        [
            [1.0],
            [1.0, 2.0, 3.0],
            ["1.0", "2.0"],
            {"x": 1.0, "y": 2.0},
            [float("nan"), 0.0],
        ],
    )
    def test_malformed_points_are_dropped(self, value: object):
        # 壊れた保存内容でページを開けなくしない（pad id と同じく黙って捨てる）
        restored = _decode(
            {"version": 1, "settings": {"flow_calibration_points": [value]}}
        )

        assert restored.flow_calibration_points == ()

    def test_a_malformed_entry_does_not_drop_the_sound_ones(self):
        restored = _decode(
            {
                "version": 1,
                "settings": {"flow_calibration_points": [[1.0], [9.5, 2.25]]},
            }
        )

        assert restored.flow_calibration_points == (Point2d(9.5, 2.25),)

    def test_a_non_sequence_decodes_to_an_empty_set(self):
        restored = _decode(
            {"version": 1, "settings": {"flow_calibration_points": "nope"}}
        )

        assert restored.flow_calibration_points == ()


class TestRoundTrip:
    def test_levels_enum_and_auto_height_survive(self):
        model = PasteSettingsModel(
            base=_base(),
            initial_purge_point=Point2d(1.0, 2.0),
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
        assert restored.initial_purge_point is None


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


class TestExportFilename:
    """書き出しファイル名 ``<基板名>-paste-overrides-<YYYYMMDDTHHMMSS>.json``."""

    def test_uses_board_stem_and_timestamp(self):
        name = board_settings_export_filename(
            "real/led_blinker.kicad_pcb", datetime(2026, 9, 11, 14, 30, 5)
        )

        assert name == "led_blinker-paste-overrides-20260911T143005.json"

    def test_omitted_timestamp_uses_now(self):
        before = datetime.now().strftime("%Y%m%d")

        name = board_settings_export_filename("board.kicad_pcb")

        assert name.startswith(f"board-paste-overrides-{before}")
        assert name.endswith(".json")

    @pytest.mark.parametrize(
        ("source_pcb", "expected_stem"),
        [
            ("my board.kicad_pcb", "my-board"),
            ("rev/2.0/panel:A.kicad_pcb", "panel-A"),
            ("日本語基板.kicad_pcb", "日本語基板"),
            ("", "board"),
        ],
    )
    def test_stem_is_folded_to_a_safe_name(self, source_pcb: str, expected_stem: str):
        name = board_settings_export_filename(
            source_pcb, datetime(2026, 9, 11, 14, 30, 5)
        )

        assert name == f"{expected_stem}-paste-overrides-20260911T143005.json"
