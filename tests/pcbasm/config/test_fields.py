"""`pcbasm.config.fields` の仕様テスト.

設定ページの項目・表示名・単位・選択肢・下限・空欄の扱い・並び順は、設定のスキーマの attrs metadata だけから作る。
"""

import attrs

from pcbasm.config import MachineConfig, load_machine
from pcbasm.config.fields import XYZ, setting_fields
from pcbasm.pasting.params import DISPENSE_MODES, LINE_DIRECTIONS
from tests.support.paths import MINIMAL_MACHINE_TOML


def _by_key():
    return {field.key: field for field in setting_fields()}


class TestSettingFields:
    def test_keys_are_attribute_paths_and_unique(self):
        keys = [field.key for field in setting_fields()]

        assert len(keys) == len(set(keys))
        machine = load_machine(MINIMAL_MACHINE_TOML)
        for field in setting_fields():
            field.resolve(machine)

    def test_sections_follow_the_declared_order(self):
        """節は MachineConfig の ``section`` の並び順で、1 つの節の項目は連続する."""
        sections = [field.section for field in setting_fields()]

        assert list(dict.fromkeys(sections)) == [
            "solder",
            "alignment",
            "probe",
            "reference_point",
            "camera",
            "settle",
            "detection",
            "audio",
        ]
        assert sections == sorted(sections, key=sections.index)

    def test_sections_without_metadata_are_not_settings(self):
        """接続先は設定ページに出さない."""
        sections = {field.section for field in setting_fields()}

        assert "klipper" not in sections
        assert {
            top.name for top in attrs.fields(MachineConfig) if top.metadata
        } == sections

    def test_fields_of_a_section_are_ordered_by_order(self):
        """節の中の並びは metadata の ``order`` で、別モジュールの下位表の項目とも混ぜて並ぶ."""
        keys = [field.key for field in setting_fields() if field.group == "吐出の基本"]

        assert keys == [
            "solder.dispenser.rotations_per_ul",
            "solder.nozzle.diameter",
            "solder.nozzle.outer_diameter",
            "solder.paste_density",
            "solder.defaults.ul_per_mm2",
        ]

    def test_kind_follows_the_field_type(self):
        fields = _by_key()

        assert fields["solder.nozzle.diameter"].kind == "float"
        assert fields["solder.nozzle.outer_diameter"].kind == "float"
        assert fields["probe.min_samples"].kind == "int"
        assert fields["camera.calibration_file"].kind == "str"
        assert fields["solder.defaults.paste_height"].kind == "float_or_auto"
        assert fields["reference_point.offsets.top_left"].kind == "float_pair"
        assert fields["solder.defaults.dispense_mode"].kind == "choice"

    def test_choices_cover_every_allowed_value(self):
        fields = _by_key()

        assert [
            value for value, _ in fields["solder.defaults.dispense_mode"].choices
        ] == list(DISPENSE_MODES)
        assert [
            value for value, _ in fields["solder.defaults.line_direction"].choices
        ] == list(LINE_DIRECTIONS)

    def test_coordinates_are_one_set_of_three_toml_keys(self):
        """座標は TOML では x / y / z の 3 キーで、設定では 1 組として読み書きする."""
        fields = _by_key()

        for key in (
            "solder.nozzle.cap.position",
            "solder.nozzle.clean.position",
        ):
            assert fields[key].kind == "float_triple"
            assert fields[key].set == XYZ
        assert [field.key for field in setting_fields() if field.set] == [
            "solder.nozzle.cap.position",
            "solder.nozzle.clean.position",
        ]

    def test_resolve_reads_the_effective_value(self):
        machine = load_machine(MINIMAL_MACHINE_TOML)
        fields = _by_key()

        assert fields["solder.motion.lift_height"].resolve(machine) == (
            machine.solder.motion.lift_height
        )
        # 未教示の座標は None
        assert fields["solder.nozzle.cap.position"].resolve(machine) is None
