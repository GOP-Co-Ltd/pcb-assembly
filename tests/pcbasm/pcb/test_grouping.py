"""階層 group-by（L0–L4）と pad 形状分類のテスト.

仕様: docs plan `claude-webui-1-pad-extract-eager-pine.md` Phase 1。 実
Pad/Component/shapely.Polygon を直接構築し、PadHierarchy.build の
公開振る舞いを検証する（PcbFile/pcbnew は使わない）。
"""

from collections.abc import Sequence

import pytest
from shapely import Polygon
from shapely.affinity import rotate

from pcbasm.geometry.transform import Point2d
from pcbasm.pcb import Component, Layer, Pad
from pcbasm.pcb.grouping import (
    PadHierarchy,
    PadHierarchyNode,
    PadShapeKey,
)


def _rect(cx: float, cy: float, w: float, h: float) -> Polygon:
    """中心 (cx, cy)・幅 w・高さ h の軸並行矩形を作る."""
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


def _pad(
    designator: str,
    pad_number: str,
    polygon: Polygon,
    *,
    is_custom_shape: bool = False,
) -> Pad:
    return Pad(
        designator=designator,
        pad_number=pad_number,
        net_name="",
        layer=Layer.TOP,
        polygon=polygon,
        is_custom_shape=is_custom_shape,
    )


def _node_at(hierarchy: PadHierarchy, key: tuple[str, ...]) -> PadHierarchyNode:
    """Key に一致するノードを階層から 1 つ探す（テスト補助）."""
    stack: list[PadHierarchyNode] = [hierarchy.root]
    while stack:
        node = stack.pop()
        if node.key == key:
            return node
        stack.extend(node.children)
    raise AssertionError(f"node {key} not found")


def _all_keys(hierarchy: PadHierarchy) -> set[tuple[str, ...]]:
    keys: set[tuple[str, ...]] = set()
    stack: list[PadHierarchyNode] = [hierarchy.root]
    while stack:
        node = stack.pop()
        keys.add(node.key)
        stack.extend(node.children)
    return keys


class TestPadShapeKey:
    """PadShapeKey.of の形状分類契約."""

    def test_same_size_rectangles_at_different_positions_share_key(self):
        # 0402 抵抗の 2 端子のように、同サイズ矩形を別位置に置いても同キー
        a = _pad("R1", "1", _rect(0.0, 0.0, 0.5, 0.9))
        b = _pad("R1", "2", _rect(5.0, 7.0, 0.5, 0.9))

        assert PadShapeKey.of(a) == PadShapeKey.of(b)

    def test_rotated_same_shape_pad_shares_key(self):
        # 同じ矩形を 90 度回転して配置 → 回転不変なので同キー（同 shape_label）
        base = _rect(0.0, 0.0, 0.5, 0.9)
        upright = _pad("U1", "1", base)
        turned = _pad("U1", "2", rotate(base, 90, origin="centroid"))

        assert PadShapeKey.of(upright) == PadShapeKey.of(turned)
        assert PadShapeKey.of(upright).label == PadShapeKey.of(turned).label

    def test_thermal_pad_and_signal_pad_have_distinct_keys(self):
        # 大面積の熱パッドと小さな信号 pad は別キー
        signal = _pad("U1", "1", _rect(0.0, 0.0, 0.3, 0.5))
        thermal = _pad("U1", "9", _rect(0.0, 0.0, 4.2, 4.2))

        assert PadShapeKey.of(signal) != PadShapeKey.of(thermal)

    def test_custom_shape_flag_distinguishes_key(self):
        # 同じ外形でも is_custom_shape が異なれば別キー
        poly = _rect(0.0, 0.0, 1.0, 1.0)
        plain = _pad("U1", "1", poly)
        custom = _pad("U1", "2", poly, is_custom_shape=True)

        assert PadShapeKey.of(plain) != PadShapeKey.of(custom)

    def test_quantum_collapses_near_identical_shapes(self):
        # わずかな寸法差は shape_quantum 内で同一視される
        a = _pad("R1", "1", _rect(0.0, 0.0, 0.500, 0.900))
        b = _pad("R1", "2", _rect(0.0, 0.0, 0.503, 0.902))

        assert PadShapeKey.of(a, quantum=0.01) == PadShapeKey.of(b, quantum=0.01)


class TestPadHierarchyBuildKeys:
    """L0–L4 のキー規約."""

    @pytest.fixture
    def simple(self) -> tuple[Sequence[Component], Sequence[Pad]]:
        components = [_component("R1", "0402")]
        pads = [
            _pad("R1", "1", _rect(0.0, 0.0, 0.5, 0.9)),
            _pad("R1", "2", _rect(1.0, 0.0, 0.5, 0.9)),
        ]
        return components, pads

    def test_node_keys_for_pad_returns_five_levels(self, simple):
        components, pads = simple
        hierarchy = PadHierarchy.build(components, pads)
        pad = pads[0]

        keys = hierarchy.node_keys_for_pad(pad)

        shape_label = PadShapeKey.of(pad).label
        assert keys == [
            ("L0",),
            ("L1", "0402"),
            ("L2", "R1"),
            ("L3", "R1", shape_label),
            ("L4", "R1", "1"),
        ]


class TestPadHierarchyBuildDuplicatePadNumbers:
    """同じ pad number を持つ分割ペースト領域の L4 キー."""

    def test_duplicate_pad_numbers_get_distinct_l4_nodes(self):
        components = [_component("U1", "LFCSP-24")]
        pads = [
            _pad("U1", "", _rect(0.0, 0.0, 0.93, 0.93)),
            _pad("U1", "", _rect(1.0, 0.0, 0.93, 0.93)),
            _pad("U1", "", _rect(0.0, 1.0, 0.93, 0.93)),
            _pad("U1", "", _rect(1.0, 1.0, 0.93, 0.93)),
        ]

        hierarchy = PadHierarchy.build(components, pads)

        l2 = _node_at(hierarchy, ("L2", "U1"))
        l3 = l2.children[0]
        assert l3.label == "0.93x0.93mm"
        assert len(l3.pads) == 4
        assert [child.key for child in l3.children] == [
            ("L4", "U1", "#1"),
            ("L4", "U1", "#2"),
            ("L4", "U1", "#3"),
            ("L4", "U1", "#4"),
        ]
        assert [hierarchy.pad_id_for_pad(pad) for pad in pads] == [
            "U1.#1",
            "U1.#2",
            "U1.#3",
            "U1.#4",
        ]

    def test_duplicate_pad_number_node_keys_point_to_each_fragment(self):
        components = [_component("U1", "LFCSP-24")]
        pads = [
            _pad("U1", "EP", _rect(0.0, 0.0, 0.93, 0.93)),
            _pad("U1", "EP", _rect(1.0, 0.0, 0.93, 0.93)),
        ]
        hierarchy = PadHierarchy.build(components, pads)

        assert hierarchy.node_keys_for_pad(pads[0])[-1] == ("L4", "U1", "EP#1")
        assert hierarchy.node_keys_for_pad(pads[1])[-1] == ("L4", "U1", "EP#2")
        assert hierarchy.l4_key_for_pad_id("U1.EP#1") == ("L4", "U1", "EP#1")
        assert hierarchy.l4_key_for_pad_id("U1.EP#2") == ("L4", "U1", "EP#2")


class TestPadHierarchyBuildShapeGrouping:
    """L3（同一種類 pad）の形状分類。"""

    def test_rotated_same_shape_pads_share_l3(self):
        # 同じ矩形 pad を 0 度と 90 度で配置 → 同 L3 グループ（回転不変）
        components = [_component("U1", "SOT-23")]
        base = _rect(0.0, 0.0, 0.5, 0.9)
        pads = [
            _pad("U1", "1", base),
            _pad("U1", "2", rotate(base, 90, origin="centroid")),
        ]
        hierarchy = PadHierarchy.build(components, pads)

        shape_label = PadShapeKey.of(pads[0]).label
        l3 = _node_at(hierarchy, ("L3", "U1", shape_label))
        numbers = {p.pad_number for p in l3.pads}
        assert numbers == {"1", "2"}

    def test_thermal_and_signal_pads_in_separate_l3(self):
        # 同 designator 内で熱パッド（大面積）と信号 pad（小矩形）は別 L3
        components = [_component("U1", "QFN-8")]
        signal = _pad("U1", "1", _rect(0.0, 0.0, 0.3, 0.5))
        thermal = _pad("U1", "9", _rect(0.0, 0.0, 4.2, 4.2))
        hierarchy = PadHierarchy.build(components, [signal, thermal])

        l2 = _node_at(hierarchy, ("L2", "U1"))
        l3_keys = {child.key for child in l2.children}
        assert len(l3_keys) == 2

        signal_l3 = _node_at(hierarchy, ("L3", "U1", PadShapeKey.of(signal).label))
        thermal_l3 = _node_at(hierarchy, ("L3", "U1", PadShapeKey.of(thermal).label))
        assert {p.pad_number for p in signal_l3.pads} == {"1"}
        assert {p.pad_number for p in thermal_l3.pads} == {"9"}


class TestPadHierarchyBuildExclusion:
    """対応 Component の無い pad は階層から除外される。"""

    def test_pad_without_matching_component_is_excluded(self):
        components = [_component("R1", "0402")]
        pads = [
            _pad("R1", "1", _rect(0.0, 0.0, 0.5, 0.9)),
            _pad("X9", "1", _rect(2.0, 0.0, 0.5, 0.9)),  # 対応部品なし
        ]
        hierarchy = PadHierarchy.build(components, pads)

        designators = {p.designator for p in hierarchy.iter_pads()}
        assert designators == {"R1"}
        assert ("L2", "X9") not in _all_keys(hierarchy)


class TestPadHierarchyFindPad:
    """find_pad_ref / find_pad_id は階層外の pad で None を返す。"""

    @pytest.fixture
    def hierarchy_and_pads(self) -> tuple[PadHierarchy, Pad, Pad]:
        components = [_component("R1", "0402")]
        known = _pad("R1", "1", _rect(0.0, 0.0, 0.5, 0.9))
        unknown = _pad("X9", "1", _rect(2.0, 0.0, 0.5, 0.9))  # 対応部品なし
        return PadHierarchy.build(components, [known, unknown]), known, unknown

    def test_known_pad_resolves_to_ref_and_id(self, hierarchy_and_pads):
        hierarchy, known, _ = hierarchy_and_pads
        assert hierarchy.find_pad_ref(known) == ("R1", "1")
        assert hierarchy.find_pad_id(known) == "R1.1"

    def test_unknown_pad_returns_none(self, hierarchy_and_pads):
        hierarchy, _, unknown = hierarchy_and_pads
        assert hierarchy.find_pad_ref(unknown) is None
        assert hierarchy.find_pad_id(unknown) is None

    def test_raise_variants_reject_unknown_pad(self, hierarchy_and_pads):
        hierarchy, _, unknown = hierarchy_and_pads
        with pytest.raises(KeyError):
            hierarchy.pad_ref_for_pad(unknown)
        with pytest.raises(KeyError):
            hierarchy.pad_id_for_pad(unknown)


class TestPadHierarchyNodePads:
    """各ノードの pads が配下の葉 pad をすべて含む。"""

    @pytest.fixture
    def multi(self) -> PadHierarchy:
        components = [
            _component("R1", "0402"),
            _component("R2", "0402"),
            _component("U1", "QFN-8"),
        ]
        pads = [
            _pad("R1", "1", _rect(0.0, 0.0, 0.5, 0.9)),
            _pad("R1", "2", _rect(1.0, 0.0, 0.5, 0.9)),
            _pad("R2", "1", _rect(5.0, 0.0, 0.5, 0.9)),
            _pad("R2", "2", _rect(6.0, 0.0, 0.5, 0.9)),
            _pad("U1", "1", _rect(10.0, 0.0, 0.3, 0.5)),
            _pad("U1", "9", _rect(11.0, 0.0, 4.2, 4.2)),
        ]
        return PadHierarchy.build(components, pads)

    def test_root_holds_all_pads(self, multi: PadHierarchy):
        assert len(multi.root.pads) == 6

    def test_l2_node_holds_all_pads_of_designator(self, multi: PadHierarchy):
        node = _node_at(multi, ("L2", "U1"))
        numbers = {p.pad_number for p in node.pads}
        assert numbers == {"1", "9"}

    def test_l1_node_groups_all_same_package_pads(self, multi: PadHierarchy):
        node = _node_at(multi, ("L1", "0402"))
        designators = {p.designator for p in node.pads}
        assert designators == {"R1", "R2"}
        assert len(node.pads) == 4

    def test_l4_node_holds_single_pad(self, multi: PadHierarchy):
        node = _node_at(multi, ("L4", "U1", "9"))
        assert len(node.pads) == 1
        assert node.pads[0].pad_number == "9"


class TestL4KeysForPadIds:
    """l4_keys_for_pad_ids は pad id 列を L4 キーへ解決し、未知 id を分離する。"""

    @pytest.fixture
    def hierarchy(self) -> PadHierarchy:
        components = [_component("R1", "0402"), _component("U1", "QFN-8")]
        pads = [
            _pad("R1", "1", _rect(0.0, 0.0, 0.5, 0.9)),
            _pad("U1", "9", _rect(11.0, 0.0, 4.2, 4.2)),
        ]
        return PadHierarchy.build(components, pads)

    def test_resolves_known_ids_to_l4_keys(self, hierarchy: PadHierarchy):
        l4_keys, unknown = hierarchy.l4_keys_for_pad_ids(["R1.1", "U1.9"])

        assert l4_keys == [("L4", "R1", "1"), ("L4", "U1", "9")]
        assert unknown == []

    def test_unknown_ids_are_separated(self, hierarchy: PadHierarchy):
        l4_keys, unknown = hierarchy.l4_keys_for_pad_ids(["R1.1", "Q9.1"])

        assert l4_keys == [("L4", "R1", "1")]
        assert unknown == ["Q9.1"]

    def test_empty_input_returns_empty(self, hierarchy: PadHierarchy):
        assert hierarchy.l4_keys_for_pad_ids([]) == ([], [])


class TestSignature:
    """Signature は pad 構成の安定ハッシュ（L4 分割 suffix は含めない）。"""

    def test_same_construction_is_stable(self):
        components = [_component("R1", "0402")]
        pads = [_pad("R1", "1", _rect(0.0, 0.0, 0.5, 0.9))]

        a = PadHierarchy.build(components, pads).signature()
        b = PadHierarchy.build(components, pads).signature()

        assert a == b

    def test_changed_construction_changes_signature(self):
        components = [_component("R1", "0402")]
        base = PadHierarchy.build(
            components, [_pad("R1", "1", _rect(0.0, 0.0, 0.5, 0.9))]
        ).signature()
        moved = PadHierarchy.build(
            components, [_pad("R1", "1", _rect(2.0, 0.0, 0.5, 0.9))]
        ).signature()

        assert base != moved
