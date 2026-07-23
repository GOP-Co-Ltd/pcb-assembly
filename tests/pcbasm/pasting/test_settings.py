"""階層 override 解決と JSON round-trip のテスト.

仕様: docs plan `claude-webui-1-pad-extract-eager-pine.md` Phase 1。 実
Pad/Component と実 PasteDispenser config を使う（モック禁止）。
"""

from collections.abc import Sequence

import pytest
from shapely import Polygon

from pcbasm.config import Machine
from pcbasm.geometry.transform import Point2d
from pcbasm.pasting.settings import (
    PASTE_OVERRIDE_FIELDS,
    LevelSetting,
    PasteOverride,
    PasteSettingsModel,
    base_override_from_config,
    find_orphans,
    is_pad_enabled,
    resolve_node_settings,
    resolve_pad_settings,
    select_enabled_pads,
    settings_from_dict,
    settings_to_dict,
    validate_field_names,
    validate_override_values,
)
from pcbasm.pcb import Component, Layer, Pad
from pcbasm.pcb.grouping import PadHierarchy, build_pad_hierarchy
from tests.helpers import TESTING_DATA_DIR

TESTING_MACHINE_TOML = TESTING_DATA_DIR / "machine.toml"


def _rect(cx: float, cy: float, w: float, h: float) -> Polygon:
    hw, hh = w / 2.0, h / 2.0
    return Polygon(
        [
            (cx - hw, cy - hh),
            (cx + hw, cy - hh),
            (cx + hw, cy + hh),
            (cx - hw, cy + hh),
        ]
    )


def _component(designator: str, package: str) -> Component:
    return Component(
        designator=designator,
        value="",
        package=package,
        position=Point2d(x=0.0, y=0.0),
        rotation=0.0,
        layer=Layer.TOP,
    )


def _pad(designator: str, pad_number: str, polygon: Polygon) -> Pad:
    return Pad(
        designator=designator,
        pad_number=pad_number,
        net_name="",
        layer=Layer.TOP,
        polygon=polygon,
    )


def _full_base() -> PasteOverride:
    """全 override 項目が非 None の base override（= L0 確定値）."""
    return PasteOverride(
        dispense_mode="auto",
        paste_height=0.05,
        ul_per_mm2=0.1,
        prime_extra_delay=0.8,
        bead_width_factor=1.0,
        overlap=0.0,
        boundary_margin=0.0,
    )


def _two_component_hierarchy() -> (
    tuple[Sequence[Component], Sequence[Pad], PadHierarchy]
):
    components = [_component("R1", "0402"), _component("U1", "QFN-8")]
    pads = [
        _pad("R1", "1", _rect(0.0, 0.0, 0.5, 0.9)),
        _pad("R1", "2", _rect(1.0, 0.0, 0.5, 0.9)),
        _pad("U1", "1", _rect(10.0, 0.0, 0.3, 0.5)),
        _pad("U1", "9", _rect(11.0, 0.0, 4.2, 4.2)),
    ]
    return components, pads, build_pad_hierarchy(components, pads)


def _duplicate_pad_number_hierarchy() -> (
    tuple[Sequence[Component], Sequence[Pad], PadHierarchy]
):
    components = [_component("U1", "LFCSP-24")]
    pads = [
        _pad("U1", "", _rect(0.0, 0.0, 0.93, 0.93)),
        _pad("U1", "", _rect(1.0, 0.0, 0.93, 0.93)),
        _pad("U1", "", _rect(0.0, 1.0, 0.93, 0.93)),
        _pad("U1", "", _rect(1.0, 1.0, 0.93, 0.93)),
    ]
    return components, pads, build_pad_hierarchy(components, pads)


class TestBaseOverrideFromConfig:
    """base_override_from_config は PasteDispenser の override 項目を写す。"""

    @pytest.fixture
    def config(self):
        return Machine(TESTING_MACHINE_TOML).paste_dispenser

    def test_copies_all_override_fields(self, config):
        override = base_override_from_config(config)

        assert override.dispense_mode == config.dispense_mode
        assert override.paste_height == config.paste_height
        assert override.ul_per_mm2 == pytest.approx(config.ul_per_mm2)
        assert override.prime_extra_delay == pytest.approx(config.prime_extra_delay)
        assert override.bead_width_factor == pytest.approx(config.bead_width_factor)
        assert override.overlap == pytest.approx(config.overlap)
        assert override.boundary_margin == pytest.approx(config.boundary_margin)

    def test_all_fields_are_non_none(self, config):
        override = base_override_from_config(config)

        for field in PASTE_OVERRIDE_FIELDS:
            assert getattr(override, field) is not None


class TestResolvePadSettingsKeys:
    """resolve_pad_settings の戻りキーと確定値。"""

    def test_unique_pad_numbers_use_designator_pad_number_pairs(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(base=_full_base())

        resolved = resolve_pad_settings(hierarchy, model)

        assert set(resolved.keys()) == {
            ("R1", "1"),
            ("R1", "2"),
            ("U1", "1"),
            ("U1", "9"),
        }

    def test_duplicate_pad_numbers_are_distinct_resolved_keys(self):
        _, pads, hierarchy = _duplicate_pad_number_hierarchy()
        model = PasteSettingsModel(base=_full_base())

        resolved = resolve_pad_settings(hierarchy, model)

        assert set(resolved.keys()) == {
            ("U1", "#1"),
            ("U1", "#2"),
            ("U1", "#3"),
            ("U1", "#4"),
        }
        assert [hierarchy.pad_ref_for_pad(pad) for pad in pads] == [
            ("U1", "#1"),
            ("U1", "#2"),
            ("U1", "#3"),
            ("U1", "#4"),
        ]

    def test_all_values_resolve_to_base_when_no_levels(self):
        _, _, hierarchy = _two_component_hierarchy()
        base = _full_base()
        model = PasteSettingsModel(base=base, base_enabled=True)

        resolved = resolve_pad_settings(hierarchy, model)

        for paste in resolved.values():
            assert paste.enabled is True
            assert paste.dispense_mode == base.dispense_mode
            assert paste.paste_height == pytest.approx(base.paste_height)
            assert paste.ul_per_mm2 == pytest.approx(base.ul_per_mm2)
            assert paste.prime_extra_delay == pytest.approx(base.prime_extra_delay)
            assert paste.bead_width_factor == pytest.approx(base.bead_width_factor)
            assert paste.overlap == pytest.approx(base.overlap)
            assert paste.boundary_margin == pytest.approx(base.boundary_margin)


class TestOverrideMerge:
    """Override の階層マージ（非 None 項目のみ上書き、未指定は継承）。"""

    def test_level_overrides_single_field_keeps_others_inherited(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L2", "U1"): LevelSetting(override=PasteOverride(ul_per_mm2=0.5)),
            },
        )

        resolved = resolve_pad_settings(hierarchy, model)

        u1 = resolved[("U1", "1")]
        assert u1.ul_per_mm2 == pytest.approx(0.5)  # 上書き
        assert u1.prime_extra_delay == pytest.approx(0.8)  # 継承
        # 別部品 R1 は影響を受けない
        assert resolved[("R1", "1")].ul_per_mm2 == pytest.approx(0.1)

    def test_mode_and_auto_height_override_inherit_like_numeric_fields(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L2", "U1"): LevelSetting(
                    override=PasteOverride(
                        dispense_mode="line",
                        paste_height="auto",
                    )
                ),
            },
        )

        resolved = resolve_pad_settings(hierarchy, model)

        u1 = resolved[("U1", "1")]
        assert u1.dispense_mode == "line"
        assert u1.paste_height == "auto"
        assert u1.ul_per_mm2 == pytest.approx(0.1)
        r1 = resolved[("R1", "1")]
        assert r1.dispense_mode == "auto"
        assert r1.paste_height == pytest.approx(0.05)

    def test_l0_override_applies_to_all_pads_over_machine_default(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L0",): LevelSetting(override=PasteOverride(prime_extra_delay=0.45)),
            },
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert {paste.prime_extra_delay for paste in resolved.values()} == {0.45}
        assert {paste.ul_per_mm2 for paste in resolved.values()} == {0.1}

    def test_l0_override_can_be_overridden_by_more_specific_level(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L0",): LevelSetting(override=PasteOverride(prime_extra_delay=0.45)),
                ("L2", "U1"): LevelSetting(
                    override=PasteOverride(prime_extra_delay=0.9)
                ),
            },
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert resolved[("R1", "1")].prime_extra_delay == pytest.approx(0.45)
        assert resolved[("U1", "1")].prime_extra_delay == pytest.approx(0.9)

    def test_more_specific_level_wins_over_less_specific(self):
        # L4 が L2 を上書き、未指定 field は L2 から継承
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L2", "U1"): LevelSetting(
                    override=PasteOverride(ul_per_mm2=0.5, prime_extra_delay=1.5)
                ),
                ("L4", "U1", "9"): LevelSetting(override=PasteOverride(ul_per_mm2=0.9)),
            },
        )

        resolved = resolve_pad_settings(hierarchy, model)

        thermal = resolved[("U1", "9")]
        assert thermal.ul_per_mm2 == pytest.approx(0.9)  # L4 が勝つ
        assert thermal.prime_extra_delay == pytest.approx(1.5)  # L2 から継承
        assert thermal.paste_height == pytest.approx(0.05)  # base から継承
        # 同部品の別 pad は L4 の影響を受けず L2 が効く
        other = resolved[("U1", "1")]
        assert other.ul_per_mm2 == pytest.approx(0.5)
        assert other.prime_extra_delay == pytest.approx(1.5)

    def test_l1_package_override_applies_to_all_same_package(self):
        components = [_component("R1", "0402"), _component("R2", "0402")]
        pads = [
            _pad("R1", "1", _rect(0.0, 0.0, 0.5, 0.9)),
            _pad("R2", "1", _rect(5.0, 0.0, 0.5, 0.9)),
        ]
        hierarchy = build_pad_hierarchy(components, pads)
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L1", "0402"): LevelSetting(
                    override=PasteOverride(bead_width_factor=0.7)
                ),
            },
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert resolved[("R1", "1")].bead_width_factor == pytest.approx(0.7)
        assert resolved[("R2", "1")].bead_width_factor == pytest.approx(0.7)


class TestEnabledResolution:
    """Enabled は最具体レベルの明示値が勝つ。"""

    def test_l2_disable_disables_all_pads_of_component(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={("L2", "U1"): LevelSetting(enabled=False)},
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert resolved[("U1", "1")].enabled is False
        assert resolved[("U1", "9")].enabled is False
        assert resolved[("R1", "1")].enabled is True  # 他部品は無関係

    def test_l4_enable_revives_pad_disabled_at_l2(self):
        # L2=False で配下無効でも、同 pad の L4=True で有効に復活
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L2", "U1"): LevelSetting(enabled=False),
                ("L4", "U1", "9"): LevelSetting(enabled=True),
            },
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert resolved[("U1", "9")].enabled is True  # L4 が勝つ
        assert resolved[("U1", "1")].enabled is False  # L4 指定なしは L2 のまま

    def test_base_enabled_false_defaults_all_pads_disabled(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(base=_full_base(), base_enabled=False)

        resolved = resolve_pad_settings(hierarchy, model)

        assert all(paste.enabled is False for paste in resolved.values())

    def test_base_enabled_false_individual_enable_revives(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            base_enabled=False,
            levels={("L4", "U1", "9"): LevelSetting(enabled=True)},
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert resolved[("U1", "9")].enabled is True
        assert resolved[("U1", "1")].enabled is False
        assert resolved[("R1", "1")].enabled is False

    def test_enabled_none_at_level_does_not_override(self):
        # LevelSetting.enabled=None（override のみ）は enabled を上書きしない
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            base_enabled=True,
            levels={
                ("L2", "U1"): LevelSetting(
                    enabled=None, override=PasteOverride(ul_per_mm2=0.5)
                ),
            },
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert resolved[("U1", "1")].enabled is True  # base_enabled を維持

    def test_l4_disable_affects_only_one_duplicate_pad_fragment(self):
        _, _, hierarchy = _duplicate_pad_number_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={("L4", "U1", "#2"): LevelSetting(enabled=False)},
        )

        resolved = resolve_pad_settings(hierarchy, model)

        assert resolved[("U1", "#1")].enabled is True
        assert resolved[("U1", "#2")].enabled is False
        assert resolved[("U1", "#3")].enabled is True
        assert resolved[("U1", "#4")].enabled is True


class TestIsPadEnabled:
    """is_pad_enabled は解決済み enabled と階層外 pad の後方互換を判定する。"""

    def test_enabled_pad_returns_true(self):
        _, pads, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(base=_full_base())
        resolved = resolve_pad_settings(hierarchy, model)

        assert is_pad_enabled(pads[0], hierarchy, resolved) is True

    def test_disabled_pad_follows_resolved_enabled(self):
        _, pads, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={("L2", "R1"): LevelSetting(enabled=False)},
        )
        resolved = resolve_pad_settings(hierarchy, model)

        r1_pads = [pad for pad in pads if pad.designator == "R1"]
        u1_pads = [pad for pad in pads if pad.designator == "U1"]
        assert all(is_pad_enabled(p, hierarchy, resolved) is False for p in r1_pads)
        assert all(is_pad_enabled(p, hierarchy, resolved) is True for p in u1_pads)

    def test_pad_outside_hierarchy_is_enabled_for_backward_compat(self):
        # 対応 Component が無い pad は階層から除外される。除外 pad は
        # 全無効設定でも後方互換で有効扱い。
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(base=_full_base(), base_enabled=False)
        resolved = resolve_pad_settings(hierarchy, model)
        orphan = _pad("X9", "1", _rect(20.0, 0.0, 1.0, 1.0))

        assert is_pad_enabled(orphan, hierarchy, resolved) is True


class TestSelectEnabledPads:
    """select_enabled_pads は有効 pad のみを元の順序で返す。"""

    def test_returns_all_pads_when_no_overrides(self):
        _, pads, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(base=_full_base())

        assert select_enabled_pads(pads, hierarchy, model) == list(pads)

    def test_excludes_disabled_pads_and_keeps_order(self):
        _, pads, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={("L4", "U1", "1"): LevelSetting(enabled=False)},
        )

        selected = select_enabled_pads(pads, hierarchy, model)

        assert [(p.designator, p.pad_number) for p in selected] == [
            ("R1", "1"),
            ("R1", "2"),
            ("U1", "9"),
        ]

    def test_pad_without_component_is_kept_for_backward_compat(self):
        # 階層に居ない pad（対応 Component 無し）は、全無効設定でも選ばれる。
        _, pads, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(base=_full_base(), base_enabled=False)
        orphan = _pad("X9", "1", _rect(20.0, 0.0, 1.0, 1.0))

        selected = select_enabled_pads([*pads, orphan], hierarchy, model)

        assert selected == [orphan]


class TestResolveNodeSettings:
    """resolve_node_settings は階層の全ノード（L0–L4）を pad と同一規則で解決する。

    UI の階層表が各ノード行に解決済み値を表示するための算出。``resolve_pad_settings``
    と同じ継承規則（最具体が勝つ・enabled は明示値が勝つ）を、ツリーのルートから
    自ノードまでのパスで適用する。
    """

    def test_returns_all_hierarchy_node_keys(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(base=_full_base())

        resolved = resolve_node_settings(hierarchy, model)

        assert set(resolved.keys()) == hierarchy.all_keys()

    def test_root_key_resolves_to_base_values(self):
        _, _, hierarchy = _two_component_hierarchy()
        base = _full_base()
        model = PasteSettingsModel(base=base, base_enabled=True)

        resolved = resolve_node_settings(hierarchy, model)

        root = resolved[("L0",)]
        assert root.enabled is True
        assert root.dispense_mode == base.dispense_mode
        assert root.paste_height == pytest.approx(base.paste_height)
        assert root.ul_per_mm2 == pytest.approx(base.ul_per_mm2)
        assert root.prime_extra_delay == pytest.approx(base.prime_extra_delay)
        assert root.bead_width_factor == pytest.approx(base.bead_width_factor)
        assert root.overlap == pytest.approx(base.overlap)
        assert root.boundary_margin == pytest.approx(base.boundary_margin)

    def test_root_enabled_follows_base_enabled_false(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(base=_full_base(), base_enabled=False)

        resolved = resolve_node_settings(hierarchy, model)

        assert resolved[("L0",)].enabled is False

    def test_node_without_override_inherits_ancestor_values(self):
        # L2:U1 に override を置くと、その配下の L3/L4 ノードは override 無しでも
        # L2 の値を継承する。
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={("L2", "U1"): LevelSetting(override=PasteOverride(ul_per_mm2=0.5))},
        )

        resolved = resolve_node_settings(hierarchy, model)

        assert resolved[("L2", "U1")].ul_per_mm2 == pytest.approx(0.5)
        assert resolved[("L4", "U1", "1")].ul_per_mm2 == pytest.approx(0.5)
        assert resolved[("L4", "U1", "9")].ul_per_mm2 == pytest.approx(0.5)
        # 継承していない field は base のまま
        assert resolved[("L4", "U1", "1")].prime_extra_delay == pytest.approx(0.8)

    def test_override_propagates_to_node_and_descendants_only(self):
        # L2:U1 の override は U1 サブツリーにのみ伝播し、兄弟 L2:R1 と
        # 祖先（L0/L1）には及ばない。
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L2", "U1"): LevelSetting(
                    override=PasteOverride(prime_extra_delay=0.3)
                )
            },
        )

        resolved = resolve_node_settings(hierarchy, model)

        # 自ノードと子孫は上書き
        assert resolved[("L2", "U1")].prime_extra_delay == pytest.approx(0.3)
        assert resolved[("L4", "U1", "1")].prime_extra_delay == pytest.approx(0.3)
        assert resolved[("L4", "U1", "9")].prime_extra_delay == pytest.approx(0.3)
        # 兄弟部品 R1 は影響なし
        assert resolved[("L2", "R1")].prime_extra_delay == pytest.approx(0.8)
        assert resolved[("L4", "R1", "1")].prime_extra_delay == pytest.approx(0.8)
        # 祖先 L0 は影響なし
        assert resolved[("L0",)].prime_extra_delay == pytest.approx(0.8)

    def test_l1_package_override_reaches_same_package_nodes(self):
        # L1 ノードに置いた override は同 package のサブツリー全体へ伝播する。
        components = [_component("R1", "0402"), _component("R2", "0402")]
        pads = [
            _pad("R1", "1", _rect(0.0, 0.0, 0.5, 0.9)),
            _pad("R2", "1", _rect(5.0, 0.0, 0.5, 0.9)),
        ]
        hierarchy = build_pad_hierarchy(components, pads)
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L1", "0402"): LevelSetting(
                    override=PasteOverride(bead_width_factor=0.7)
                ),
            },
        )

        resolved = resolve_node_settings(hierarchy, model)

        assert resolved[("L1", "0402")].bead_width_factor == pytest.approx(0.7)
        assert resolved[("L4", "R1", "1")].bead_width_factor == pytest.approx(0.7)
        assert resolved[("L4", "R2", "1")].bead_width_factor == pytest.approx(0.7)

    def test_more_specific_node_override_wins_over_ancestor(self):
        # L4 ノードの override が祖先 L2 を上書きし、未指定 field は L2 から継承。
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L2", "U1"): LevelSetting(
                    override=PasteOverride(ul_per_mm2=0.5, prime_extra_delay=1.5)
                ),
                ("L4", "U1", "9"): LevelSetting(override=PasteOverride(ul_per_mm2=0.9)),
            },
        )

        resolved = resolve_node_settings(hierarchy, model)

        thermal = resolved[("L4", "U1", "9")]
        assert thermal.ul_per_mm2 == pytest.approx(0.9)  # L4 が勝つ
        assert thermal.prime_extra_delay == pytest.approx(1.5)  # L2 から継承
        assert thermal.paste_height == pytest.approx(0.05)  # base から継承
        # 同部品の別 L4 ノードは L4 override の影響を受けず L2 のまま
        other = resolved[("L4", "U1", "1")]
        assert other.ul_per_mm2 == pytest.approx(0.5)

    def test_enabled_none_keeps_inherited_enabled(self):
        # LevelSetting.enabled=None（override のみ）は enabled を上書きしない。
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            base_enabled=True,
            levels={
                ("L2", "U1"): LevelSetting(
                    enabled=None, override=PasteOverride(ul_per_mm2=0.5)
                ),
            },
        )

        resolved = resolve_node_settings(hierarchy, model)

        assert resolved[("L2", "U1")].enabled is True
        assert resolved[("L4", "U1", "1")].enabled is True

    def test_explicit_disable_propagates_and_explicit_enable_revives(self):
        # L2=False で配下無効化、配下 L4=True で当該ノードのみ復活。
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L2", "U1"): LevelSetting(enabled=False),
                ("L4", "U1", "9"): LevelSetting(enabled=True),
            },
        )

        resolved = resolve_node_settings(hierarchy, model)

        assert resolved[("L2", "U1")].enabled is False
        assert resolved[("L4", "U1", "9")].enabled is True  # L4 で復活
        assert resolved[("L4", "U1", "1")].enabled is False  # L2 のまま
        # 兄弟部品 R1 は無関係
        assert resolved[("L2", "R1")].enabled is True

    def test_l4_node_values_match_resolve_pad_settings(self):
        # parity: 各 L4 ノードキーの解決値が、その L4 配下 pad に対する
        # resolve_pad_settings の解決値と一致する（同一規則の担保）。
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            base_enabled=True,
            levels={
                ("L1", "0402"): LevelSetting(override=PasteOverride(overlap=0.3)),
                ("L2", "U1"): LevelSetting(
                    enabled=False,
                    override=PasteOverride(ul_per_mm2=0.5, prime_extra_delay=1.5),
                ),
                ("L4", "U1", "9"): LevelSetting(
                    enabled=True, override=PasteOverride(ul_per_mm2=0.9)
                ),
            },
        )

        node_resolved = resolve_node_settings(hierarchy, model)
        pad_resolved = resolve_pad_settings(hierarchy, model)

        for pad in hierarchy.iter_pads():
            l4_key = hierarchy.node_keys_for_pad(pad)[-1]
            pad_ref = hierarchy.pad_ref_for_pad(pad)
            assert node_resolved[l4_key] == pad_resolved[pad_ref]

    def test_l4_node_values_match_resolve_pad_settings_for_duplicate_pads(self):
        # parity（分割 pad）: 同一 pad_number の分割片も L4 key が個別に分かれ、
        # pad-level 解決と一致する。
        _, _, hierarchy = _duplicate_pad_number_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={("L4", "U1", "#2"): LevelSetting(enabled=False)},
        )

        node_resolved = resolve_node_settings(hierarchy, model)
        pad_resolved = resolve_pad_settings(hierarchy, model)

        for pad in hierarchy.iter_pads():
            l4_key = hierarchy.node_keys_for_pad(pad)[-1]
            pad_ref = hierarchy.pad_ref_for_pad(pad)
            assert node_resolved[l4_key] == pad_resolved[pad_ref]


class TestSettingsRoundTrip:
    """settings_to_dict -> settings_from_dict の round-trip。"""

    def test_base_and_enabled_round_trip(self):
        model = PasteSettingsModel(base=_full_base(), base_enabled=False)

        restored = settings_from_dict(settings_to_dict(model))

        assert restored.base_enabled is False
        assert restored.base.dispense_mode == "auto"
        assert restored.base.prime_extra_delay == pytest.approx(0.8)
        assert restored.base.paste_height == pytest.approx(0.05)
        assert restored.base.ul_per_mm2 == pytest.approx(0.1)

    def test_mode_and_auto_height_round_trip(self):
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L2", "U1"): LevelSetting(
                    override=PasteOverride(
                        dispense_mode="area",
                        paste_height="auto",
                    )
                ),
            },
        )

        restored = settings_from_dict(settings_to_dict(model))
        override = restored.levels[("L2", "U1")].override

        assert override.dispense_mode == "area"
        assert override.paste_height == "auto"

    def test_levels_tuple_keys_are_restored(self):
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L1", "0402"): LevelSetting(override=PasteOverride(ul_per_mm2=0.08)),
                ("L2", "U1"): LevelSetting(enabled=False),
                ("L4", "U1", "9"): LevelSetting(
                    enabled=True, override=PasteOverride(prime_extra_delay=1.2)
                ),
            },
        )

        restored = settings_from_dict(settings_to_dict(model))

        assert set(restored.levels.keys()) == {
            ("L1", "0402"),
            ("L2", "U1"),
            ("L4", "U1", "9"),
        }
        assert restored.levels[("L1", "0402")].override.ul_per_mm2 == pytest.approx(
            0.08
        )
        assert restored.levels[("L2", "U1")].enabled is False
        assert restored.levels[("L4", "U1", "9")].enabled is True
        assert restored.levels[
            ("L4", "U1", "9")
        ].override.prime_extra_delay == pytest.approx(1.2)

    def test_unset_override_fields_stay_inherited_after_round_trip(self):
        # override の欠落（None = 継承）が round-trip 後も None のまま
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L2", "U1"): LevelSetting(override=PasteOverride(ul_per_mm2=0.5)),
            },
        )

        restored = settings_from_dict(settings_to_dict(model))
        override = restored.levels[("L2", "U1")].override

        assert override.ul_per_mm2 == pytest.approx(0.5)
        assert override.prime_extra_delay is None
        assert override.paste_height is None
        assert override.boundary_margin is None

    def test_serialized_override_omits_none_fields(self):
        # JSON 形式: override は非 None 項目のみ含む（欠落 = 継承）
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L1", "0402"): LevelSetting(override=PasteOverride(ul_per_mm2=0.08)),
            },
        )

        data = settings_to_dict(model)

        levels = data["levels"]
        assert isinstance(levels, list)
        level = next(lv for lv in levels if lv["key"] == ["L1", "0402"])
        assert level["override"] == {"ul_per_mm2": 0.08}

    def test_resolution_is_stable_across_round_trip(self):
        # round-trip 後のモデルが同じ解決結果を生む（振る舞いの保存）
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L2", "U1"): LevelSetting(
                    enabled=False, override=PasteOverride(ul_per_mm2=0.5)
                ),
                ("L4", "U1", "9"): LevelSetting(enabled=True),
            },
        )

        before = resolve_pad_settings(hierarchy, model)
        after = resolve_pad_settings(
            hierarchy, settings_from_dict(settings_to_dict(model))
        )

        for key in before:
            assert before[key].enabled == after[key].enabled
            assert before[key].ul_per_mm2 == pytest.approx(after[key].ul_per_mm2)


class TestFindOrphans:
    """find_orphans は現存階層に無い levels キーを返す。"""

    def test_returns_keys_absent_from_hierarchy(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L2", "U1"): LevelSetting(enabled=False),  # 現存
                ("L2", "Q9"): LevelSetting(enabled=False),  # 階層に無い
                ("L4", "U1", "404"): LevelSetting(enabled=True),  # pad 番号が無い
            },
        )

        orphans = find_orphans(model, hierarchy)

        assert set(orphans) == {("L2", "Q9"), ("L4", "U1", "404")}

    def test_returns_empty_when_all_keys_exist(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={
                ("L1", "0402"): LevelSetting(enabled=True),
                ("L2", "U1"): LevelSetting(enabled=False),
            },
        )

        assert find_orphans(model, hierarchy) == []


class TestValidateOverrideValues:
    """validate_override_values / validate_field_names の None 返却バリデーション。"""

    def test_valid_values_return_none(self):
        assert (
            validate_override_values(
                {"ul_per_mm2": 0.5, "dispense_mode": "line", "paste_height": "auto"}
            )
            is None
        )

    def test_unknown_field_is_rejected(self):
        message = validate_override_values({"bogus": 1.0})

        assert message is not None
        assert "bogus" in message

    def test_unknown_dispense_mode_is_rejected(self):
        assert validate_override_values({"dispense_mode": "spray"}) is not None

    def test_non_positive_paste_height_is_rejected(self):
        assert validate_override_values({"paste_height": 0.0}) is not None
        assert validate_override_values({"paste_height": -1.0}) is not None

    def test_auto_paste_height_is_allowed(self):
        assert validate_override_values({"paste_height": "auto"}) is None

    def test_bool_numeric_is_rejected(self):
        assert validate_override_values({"ul_per_mm2": True}) is not None

    def test_non_numeric_value_is_rejected(self):
        assert validate_override_values({"prime_extra_delay": "fast"}) is not None

    def test_validate_field_names_separates_unknown(self):
        assert validate_field_names(["bogus"]) is not None
        assert validate_field_names(["ul_per_mm2", "prime_extra_delay"]) is None


class TestWithLevelPatch:
    """PasteSettingsModel.with_level_patch の upsert / clear / enabled 保持。"""

    def test_upsert_values_keeps_other_fields_inherited(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(base=_full_base())

        patched = model.with_level_patch(("L2", "U1"), values={"ul_per_mm2": 0.5})

        resolved = resolve_pad_settings(hierarchy, patched)
        assert resolved[("U1", "1")].ul_per_mm2 == pytest.approx(0.5)
        assert resolved[("U1", "1")].prime_extra_delay == pytest.approx(0.8)  # 継承

    def test_clear_restores_inheritance(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={("L2", "U1"): LevelSetting(override=PasteOverride(ul_per_mm2=0.5))},
        )

        patched = model.with_level_patch(("L2", "U1"), clear=["ul_per_mm2"])

        resolved = resolve_pad_settings(hierarchy, patched)
        assert resolved[("U1", "1")].ul_per_mm2 == pytest.approx(0.1)  # base へ復帰

    def test_enabled_not_sent_keeps_existing(self):
        model = PasteSettingsModel(
            base=_full_base(),
            levels={("L2", "U1"): LevelSetting(enabled=False)},
        )

        patched = model.with_level_patch(("L2", "U1"), values={"ul_per_mm2": 0.5})

        assert patched.levels[("L2", "U1")].enabled is False  # enabled は保持

    def test_enabled_sent_updates_enabled(self):
        model = PasteSettingsModel(base=_full_base())

        patched = model.with_level_patch(("L2", "U1"), enabled=False, enabled_sent=True)

        assert patched.levels[("L2", "U1")].enabled is False

    def test_empty_result_removes_level(self):
        model = PasteSettingsModel(
            base=_full_base(),
            levels={("L2", "U1"): LevelSetting(override=PasteOverride(ul_per_mm2=0.5))},
        )

        patched = model.with_level_patch(("L2", "U1"), clear=["ul_per_mm2"])

        assert ("L2", "U1") not in patched.levels

    def test_returns_new_model_without_mutating_original(self):
        model = PasteSettingsModel(base=_full_base())

        model.with_level_patch(("L2", "U1"), values={"ul_per_mm2": 0.5})

        assert model.levels == {}


class TestWithPadsEnabled:
    """with_pads_enabled は指定 L4 群の enabled を一括設定する。"""

    def test_disables_listed_pads_only(self):
        _, _, hierarchy = _two_component_hierarchy()
        model = PasteSettingsModel(base=_full_base())
        l4_keys, _ = hierarchy.l4_keys_for_pad_ids(["U1.9"])

        patched = model.with_pads_enabled(l4_keys, enabled=False)

        resolved = resolve_pad_settings(hierarchy, patched)
        assert resolved[("U1", "9")].enabled is False
        assert resolved[("U1", "1")].enabled is True
        assert resolved[("R1", "1")].enabled is True
