"""階層ノードごとの実際の塗布方式と auto の塗布高さの集計テスト（実 Pad / Component を使う）.

形状はノズル径 1.0・ビード幅係数 1.0・外周余白 0（inset 0.5）・面塗布の短辺閾値 3.0 で
方式が決まるように選ぶ。

- 4 x 4: auto で面、線指定でも線が成立する
- 0.8 x 5: auto で微小線（短辺 0.8 <= ノズル内径 1.0）、面指定は inset で消えて線へ落ちる
- 0.8 x 0.8: auto で点、線指定は長さが 2 * inset 以下で点へ落ちる
"""

from collections.abc import Mapping

import attrs
import pytest
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
from pcbasm.pasting.pad_editor import AppliedValueSummary, applied_value_summaries
from pcbasm.pasting.params import PasteParams, PasteParamValue
from tests.support.solder import solder_config

type Patch = tuple[HierKey, Mapping[str, PasteParamValue] | None, bool | None]


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


def _all_auto_base() -> PasteParams:
    """高さを 3 つとも auto にした基板既定値（方式は auto）."""
    return attrs.evolve(
        _base(),
        paste_height="auto",
        micro_line_z_start="auto",
        micro_line_z_end="auto",
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
    base: PasteParams, *patches: Patch
) -> dict[HierKey, dict[str, AppliedValueSummary]]:
    """``(key, values, enabled)`` の override を重ねたモデルで集計する."""
    hierarchy = _hierarchy()
    model = PasteSettingsModel(base=base)
    for key, values, enabled in patches:
        model = model.with_level_patch(
            key, values=values, enabled=enabled, enabled_sent=enabled is not None
        )
    return applied_value_summaries(
        hierarchy,
        resolve_pad_settings(hierarchy, model),
        resolve_node_settings(hierarchy, model),
        _config(),
    )


def _modes(*patches: Patch) -> dict[HierKey, AppliedValueSummary]:
    """高さを数値にした既定値で、塗布方式の集計だけを取り出す."""
    summaries = _summaries(_base(), *patches)
    assert all(set(fields) == {"dispense_mode"} for fields in summaries.values())
    return {key: fields["dispense_mode"] for key, fields in summaries.items()}


class TestAutoNodes:
    """解決済みの方式が auto のノードは、配下の有効 pad の実際の方式を出す."""

    def test_mixed_modes_are_counted_in_choice_order(self):
        summaries = _modes()

        root = summaries[("L0",)]
        assert root.counts == {"dot": 1, "area": 1, "micro_line": 2}
        assert list(root.counts) == ["dot", "area", "micro_line"]
        assert root.label == "→ 点 1 / 面 1 / 微小線 2"
        assert "有効 pad 4 件" in root.title
        assert "微小線 2 件" in root.title

    def test_a_single_mode_shows_only_the_mode(self):
        summaries = _modes()

        assert summaries[("L2", "R1")].label == "→ 微小線"
        assert summaries[("L2", "R1")].counts == {"micro_line": 2}
        assert summaries[("L4", "U1", "1")].label == "→ 面"


class TestExplicitModes:
    """明示した方式は、選択値と実際の方式が食い違うノードにだけ出す."""

    def test_dot_everywhere_has_nothing_to_show(self):
        assert _modes((("L0",), {"dispense_mode": "dot"}, None)) == {}

    def test_line_fallback_to_dot_is_shown_only_where_it_happens(self):
        summaries = _modes((("L0",), {"dispense_mode": "line"}, None))

        # 小 pad だけ線が成立せず点へ落ちる
        assert summaries[("L4", "U1", "2")].label == "→ 点"
        assert summaries[("L0",)].counts == {"dot": 1, "line": 3}
        # 線がそのまま通るノードには出さない
        assert ("L2", "R1") not in summaries
        assert ("L4", "U1", "1") not in summaries

    def test_area_fallback_to_line_is_shown(self):
        summaries = _modes((("L2", "R1"), {"dispense_mode": "area"}, None))

        assert summaries[("L2", "R1")].label == "→ 線"

    def test_descendant_override_under_an_explicit_parent_is_shown(self):
        summaries = _modes(
            (("L0",), {"dispense_mode": "line"}, None),
            (("L4", "U1", "2"), {"dispense_mode": "dot"}, None),
        )

        # 自ノードは点を明示しているので出さないが、親の線ノードは混在として出す
        assert ("L4", "U1", "2") not in summaries
        assert summaries[("L2", "U1")].counts == {"dot": 1, "line": 1}


class TestDisabledPads:
    """無効 pad は塗らないので数えない."""

    def test_disabled_pads_are_left_out_of_the_counts(self):
        summaries = _modes((("L4", "U1", "2"), None, False))

        assert summaries[("L0",)].counts == {"area": 1, "micro_line": 2}
        assert ("L4", "U1", "2") not in summaries

    def test_a_node_without_enabled_pads_has_nothing_to_show(self):
        summaries = _modes((("L2", "U1"), None, False))

        for key in (("L1", "QFN-8"), ("L2", "U1"), ("L4", "U1", "1")):
            assert key not in summaries
        assert summaries[("L0",)].counts == {"micro_line": 2}


class TestAutoHeights:
    """Auto の高さには、塗布に使う計算値を ``(X)`` で添える（値の名前と単位は付けない）.

    点・線・面の auto は ``ul_per_mm2``（0.1）。
    微小線（0.8 x 5、ノズル外径 1.0）の auto は 塗布量 0.4 uL / (幅 0.8 x (線長 4.0 + 外径 1.0)) = 0.1。
    4 x 4 を微小線で塗ると 1.6 uL / (幅 1.0 x (線長 3.0 + 外径 1.0)) = 0.4。
    """

    def test_an_auto_mode_row_shows_both_paste_height_and_micro_line_heights(self):
        # R1 だけ量を半分にして、微小線の値（0.05）を塗布高さ（0.1）と見分ける
        summaries = _summaries(
            _all_auto_base(), (("L2", "R1"), {"ul_per_mm2": 0.05}, None)
        )

        root = summaries[("L0",)]
        assert set(root) == {"dispense_mode", "paste_height"}
        assert root["paste_height"].label == "(0.10) / 微小線 (0.05)"
        assert root["paste_height"].counts == {"auto 0.10": 2, "微小線 auto 0.05": 2}
        # 何の値を何件数えたかを、値の名前ごとに書く
        assert root["paste_height"].title == (
            "計算した高さ（auto: 有効 pad 2 件、微小線 auto: 有効 pad 2 件）"
        )
        # 方式 auto のまま微小線になる pad だけのノードは、微小線の値だけを出す
        assert summaries[("L2", "R1")]["paste_height"].label == "微小線 (0.05)"
        assert summaries[("L2", "U1")]["paste_height"].label == "(0.10)"

    def test_micro_line_rows_show_the_start_and_end_heights(self):
        summaries = _summaries(
            _all_auto_base(), (("L2", "R1"), {"dispense_mode": "micro_line"}, None)
        )

        r1 = summaries[("L2", "R1")]
        assert set(r1) == {"micro_line_z_start", "micro_line_z_end"}
        assert r1["micro_line_z_start"].label == "(0.10)"
        assert r1["micro_line_z_end"].label == "(0.10)"
        assert r1["micro_line_z_start"].counts == {"auto 0.10": 2}

    def test_only_the_auto_end_of_a_micro_line_is_shown(self):
        summaries = _summaries(
            _all_auto_base(),
            (("L2", "R1"), {"dispense_mode": "micro_line"}, None),
            (("L2", "R1"), {"micro_line_z_start": 0.07}, None),
        )

        assert set(summaries[("L2", "R1")]) == {"micro_line_z_end"}

    @pytest.mark.parametrize(
        ("z_start", "expected"),
        [("auto", "(0.10)"), (0.09, "(0.09)")],
    )
    def test_an_end_following_the_start_shows_the_value_it_follows(
        self, z_start: float | str, expected: str
    ):
        base = attrs.evolve(_all_auto_base(), micro_line_z_end="start")

        summaries = _summaries(
            base,
            (
                ("L2", "R1"),
                {"dispense_mode": "micro_line", "micro_line_z_start": z_start},
                None,
            ),
        )

        end = summaries[("L2", "R1")]["micro_line_z_end"]
        assert end.label == expected
        assert end.title == "計算した高さ（始点に従う: 有効 pad 2 件）"

    @pytest.mark.parametrize(
        ("node_end", "pad_end", "expected"),
        [
            # 子 pad が自分の値で塗る分は、ノードの値の表示に混ぜない
            ("start", "auto", "(0.09)"),
            ("auto", "start", "(0.10)"),
        ],
    )
    def test_pads_with_another_end_keyword_are_left_out(
        self, node_end: str, pad_end: str, expected: str
    ):
        base = attrs.evolve(_all_auto_base(), micro_line_z_end=node_end)

        summaries = _summaries(
            base,
            (
                ("L2", "R1"),
                {"dispense_mode": "micro_line", "micro_line_z_start": 0.09},
                None,
            ),
            (("L4", "R1", "1"), {"micro_line_z_end": pad_end}, None),
        )

        end = summaries[("L2", "R1")]["micro_line_z_end"]
        assert end.label == expected
        assert sum(end.counts.values()) == 1

    def test_pads_that_fall_back_to_dot_are_left_out_of_micro_line_heights(self):
        summaries = _summaries(
            _all_auto_base(), (("L0",), {"dispense_mode": "micro_line"}, None)
        )

        # 0.8 x 0.8 は点へ落ちるので数えない（塗布高さの欄はこの行に無い）
        root = summaries[("L0",)]
        assert set(root) == {"dispense_mode", "micro_line_z_start", "micro_line_z_end"}
        assert root["micro_line_z_start"].label == "(0.10–0.40)"
        assert root["micro_line_z_start"].counts == {"auto 0.10": 2, "auto 0.40": 1}

    def test_micro_line_heights_are_shown_even_when_paste_height_is_explicit(self):
        base = attrs.evolve(_base(), micro_line_z_start="auto", micro_line_z_end="auto")

        summaries = _summaries(base)

        assert summaries[("L0",)]["paste_height"].label == "微小線 (0.10)"
        assert "paste_height" not in summaries[("L2", "U1")]

    def test_differing_values_are_shown_as_a_range(self):
        summaries = _summaries(
            _all_auto_base(), (("L4", "U1", "2"), {"ul_per_mm2": 0.2}, None)
        )

        u1 = summaries[("L2", "U1")]["paste_height"]
        assert u1.label == "(0.10–0.20)"
        assert u1.counts == {"auto 0.10": 1, "auto 0.20": 1}
        pad = summaries[("L4", "U1", "2")]["paste_height"]
        assert pad.label == "(0.20)"

    def test_pads_with_an_explicit_height_are_left_out(self):
        summaries = _summaries(
            _all_auto_base(), (("L4", "U1", "2"), {"paste_height": 0.3}, None)
        )

        assert summaries[("L2", "U1")]["paste_height"].counts == {"auto 0.10": 1}
        assert "paste_height" not in summaries.get(("L4", "U1", "2"), {})

    def test_an_auto_node_under_an_explicit_parent_shows_its_value(self):
        summaries = _summaries(
            _base(), (("L4", "U1", "1"), {"paste_height": "auto"}, None)
        )

        node = summaries[("L4", "U1", "1")]["paste_height"]
        assert node.label == "(0.10)"
        assert "paste_height" not in summaries[("L0",)]

    def test_disabled_pads_are_left_out(self):
        summaries = _summaries(_all_auto_base(), (("L2", "U1"), None, False))

        assert ("L2", "U1") not in summaries
        assert summaries[("L0",)]["paste_height"].label == "微小線 (0.10)"
