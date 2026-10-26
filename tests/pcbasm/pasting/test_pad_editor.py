"""階層ノードごとの実際の塗布方式の集計テスト（実 Pad / Component を使う）.

形状はノズル径 1.0・ビード幅係数 1.0・外周余白 0（inset 0.5）・面塗布の短辺閾値 3.0 で
方式が決まるように選ぶ。

- 4 x 4: auto で面、線指定でも線が成立する
- 0.8 x 5: auto で微小線（短辺 0.8 <= ノズル内径 1.0）、面指定は inset で消えて線へ落ちる
- 0.8 x 0.8: auto で点、線指定は長さが 2 * inset 以下で点へ落ちる
"""

from collections.abc import Mapping

from shapely import Polygon

from pcbasm.board import Component, Layer, Pad
from pcbasm.config.solder import AutoModeConfig, SolderConfig
from pcbasm.geometry import Point2d
from pcbasm.pasting.hierarchy import HierKey, PadHierarchy
from pcbasm.pasting.overrides import (
    PasteSettingsModel,
    resolve_node_settings,
    resolve_pad_settings,
)
from pcbasm.pasting.pad_editor import AppliedModeSummary, applied_mode_summaries
from pcbasm.pasting.params import PasteParams, PasteParamValue
from tests.support.solder import solder_config


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


def _config() -> SolderConfig:
    return solder_config(
        nozzle_diameter=1.0,
        auto=AutoModeConfig(line_aspect_ratio=1.618, area_short_side_factor=3.0),
        defaults=PasteParams(ul_per_mm2=0.05, paste_height=0.5),
    )


def _base() -> PasteParams:
    return PasteParams(
        dispense_mode="auto",
        line_direction="unconstrained",
        paste_height=0.05,
        ul_per_mm2=0.1,
        prime_extra_delay=0.0,
        bead_width_factor=1.0,
        overlap=0.0,
        boundary_margin=0.0,
        micro_line_z_start=0.07,
        micro_line_z_end=0.07,
    )


def _hierarchy() -> PadHierarchy:
    """R1 = 細長 2 枚（auto で線）、U1 = 大 1 枚（面）+ 小 1 枚（点）."""
    components = [_component("R1", "0805"), _component("U1", "QFN-8")]
    pads = [
        _pad("R1", "1", _rect(0.0, 0.0, 0.8, 5.0)),
        _pad("R1", "2", _rect(2.0, 0.0, 0.8, 5.0)),
        _pad("U1", "1", _rect(10.0, 0.0, 4.0, 4.0)),
        _pad("U1", "2", _rect(14.0, 0.0, 0.8, 0.8)),
    ]
    return PadHierarchy.build(components, pads)


def _summaries(
    *patches: tuple[HierKey, Mapping[str, PasteParamValue] | None, bool | None],
) -> dict[HierKey, AppliedModeSummary]:
    """``(key, values, enabled)`` の override を重ねたモデルで集計する."""
    hierarchy = _hierarchy()
    model = PasteSettingsModel(base=_base())
    for key, values, enabled in patches:
        model = model.with_level_patch(
            key, values=values, enabled=enabled, enabled_sent=enabled is not None
        )
    return applied_mode_summaries(
        hierarchy,
        resolve_pad_settings(hierarchy, model),
        resolve_node_settings(hierarchy, model),
        _config(),
    )


class TestAutoNodes:
    """解決済みの方式が auto のノードは、配下の有効 pad の実際の方式を出す."""

    def test_mixed_modes_are_counted_in_choice_order(self):
        summaries = _summaries()

        root = summaries[("L0",)]
        assert root.counts == {"dot": 1, "area": 1, "micro_line": 2}
        assert list(root.counts) == ["dot", "area", "micro_line"]
        assert root.label == "→ 点 1 / 面 1 / 微小線 2"
        assert "有効 pad 4 件" in root.title
        assert "微小線 2 件" in root.title

    def test_a_single_mode_shows_only_the_mode(self):
        summaries = _summaries()

        assert summaries[("L2", "R1")].label == "→ 微小線"
        assert summaries[("L2", "R1")].counts == {"micro_line": 2}
        assert summaries[("L4", "U1", "1")].label == "→ 面"


class TestExplicitModes:
    """明示した方式は、選択値と実際の方式が食い違うノードにだけ出す."""

    def test_dot_everywhere_has_nothing_to_show(self):
        assert _summaries((("L0",), {"dispense_mode": "dot"}, None)) == {}

    def test_line_fallback_to_dot_is_shown_only_where_it_happens(self):
        summaries = _summaries((("L0",), {"dispense_mode": "line"}, None))

        # 小 pad だけ線が成立せず点へ落ちる
        assert summaries[("L4", "U1", "2")].label == "→ 点"
        assert summaries[("L0",)].counts == {"dot": 1, "line": 3}
        # 線がそのまま通るノードには出さない
        assert ("L2", "R1") not in summaries
        assert ("L4", "U1", "1") not in summaries

    def test_area_fallback_to_line_is_shown(self):
        summaries = _summaries((("L2", "R1"), {"dispense_mode": "area"}, None))

        assert summaries[("L2", "R1")].label == "→ 線"

    def test_descendant_override_under_an_explicit_parent_is_shown(self):
        summaries = _summaries(
            (("L0",), {"dispense_mode": "line"}, None),
            (("L4", "U1", "2"), {"dispense_mode": "dot"}, None),
        )

        # 自ノードは点を明示しているので出さないが、親の線ノードは混在として出す
        assert ("L4", "U1", "2") not in summaries
        assert summaries[("L2", "U1")].counts == {"dot": 1, "line": 1}


class TestDisabledPads:
    """無効 pad は塗らないので数えない."""

    def test_disabled_pads_are_left_out_of_the_counts(self):
        summaries = _summaries((("L4", "U1", "2"), None, False))

        assert summaries[("L0",)].counts == {"area": 1, "micro_line": 2}
        assert ("L4", "U1", "2") not in summaries

    def test_a_node_without_enabled_pads_has_nothing_to_show(self):
        summaries = _summaries((("L2", "U1"), None, False))

        for key in (("L1", "QFN-8"), ("L2", "U1"), ("L4", "U1", "1")):
            assert key not in summaries
        assert summaries[("L0",)].counts == {"micro_line": 2}
