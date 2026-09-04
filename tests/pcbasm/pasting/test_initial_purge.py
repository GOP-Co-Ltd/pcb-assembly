"""Initial purge pad resolution public-contract tests."""

import pytest
from shapely import Polygon

from pcbasm.geometry import Point2d
from pcbasm.pasting.initial_purge import (
    DATASET_PURGE_PAD_ID,
    ResolvedInitialPurge,
    resolve_dataset_initial_purge,
    resolve_initial_purge,
    validate_initial_purge,
)
from pcbasm.pcb import Component, Layer, Pad, PadHierarchy


def _rect(cx: float, cy: float, w: float = 1.0, h: float = 1.0) -> Polygon:
    hw = w / 2.0
    hh = h / 2.0
    return Polygon(
        [
            (cx - hw, cy - hh),
            (cx + hw, cy - hh),
            (cx + hw, cy + hh),
            (cx - hw, cy + hh),
        ]
    )


def _pad(
    designator: str,
    pad_number: str,
    *,
    center: Point2d,
    layer: Layer = Layer.TOP,
) -> Pad:
    return Pad(
        designator=designator,
        pad_number=pad_number,
        net_name="",
        layer=layer,
        polygon=_rect(center.x, center.y),
    )


def _hierarchy(pads: list[Pad]):
    components = [
        Component(
            designator=designator,
            value="",
            package="0402",
            position=Point2d(0.0, 0.0),
            rotation=0.0,
            layer=Layer.TOP,
        )
        for designator in sorted({pad.designator for pad in pads})
    ]
    return PadHierarchy.build(components, pads)


class TestResolveInitialPurge:
    """resolve_initial_purge selects the purge target from routed/top pads."""

    def test_default_uses_first_routed_enabled_pad(self):
        later = _pad("R1", "1", center=Point2d(5.0, 0.0))
        first_routed = _pad("U1", "2", center=Point2d(1.0, 2.0))
        hierarchy = _hierarchy([later, first_routed])

        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[first_routed, later],
        )

        assert error is None
        assert isinstance(resolved, ResolvedInitialPurge)
        assert resolved.pad_id == "U1.2"
        assert resolved.pad.center == first_routed.center
        assert resolved.amount_ul == pytest.approx(0.1)

    def test_explicit_disabled_top_pad_is_allowed(self):
        enabled = _pad("R1", "1", center=Point2d(0.0, 0.0))
        disabled = _pad("U1", "1", center=Point2d(3.0, 4.0))
        hierarchy = _hierarchy([enabled, disabled])

        resolved, error = resolve_initial_purge(
            amount_ul=0.25,
            pad_id="U1.1",
            hierarchy=hierarchy,
            routed_pads=[enabled],
        )

        assert error is None
        assert isinstance(resolved, ResolvedInitialPurge)
        assert resolved.pad_id == "U1.1"
        assert resolved.pad.center == disabled.center
        assert resolved.amount_ul == pytest.approx(0.25)

    def test_amount_zero_disables_initial_purge(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        resolved, error = resolve_initial_purge(
            amount_ul=0.0,
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[pad],
        )

        assert error is None
        assert resolved is None

    def test_unknown_explicit_pad_returns_error_text(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            pad_id="R9.9",
            hierarchy=hierarchy,
            routed_pads=[pad],
        )

        assert resolved is None
        assert error is not None
        assert "R9.9" in error
        assert "未知" in error

    def test_bottom_explicit_pad_returns_error_text(self):
        top = _pad("R1", "1", center=Point2d(0.0, 0.0))
        bottom = _pad("R3", "1", center=Point2d(1.0, 1.0), layer=Layer.BOTTOM)
        hierarchy = _hierarchy([top, bottom])

        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            pad_id="R3.1",
            hierarchy=hierarchy,
            routed_pads=[top],
        )

        assert resolved is None
        assert error is not None
        assert "R3.1" in error
        assert "Top" in error


class TestResolveDatasetInitialPurge:
    """Dataset収集だけ、未指定時にPURGE designatorを自動選択する."""

    def test_default_uses_unique_top_purge_pad(self):
        purge = _pad("PURGE", "1", center=Point2d(1.0, 2.0))
        sample = _pad("U1", "1", center=Point2d(3.0, 4.0))

        resolved, error = resolve_dataset_initial_purge(
            amount_ul=0.1,
            pad_id=None,
            hierarchy=_hierarchy([purge, sample]),
        )

        assert error is None
        assert isinstance(resolved, ResolvedInitialPurge)
        assert resolved.pad is purge
        assert resolved.pad_id == DATASET_PURGE_PAD_ID
        assert resolved.source == "default"

    def test_explicit_board_selection_takes_precedence_over_purge(self):
        purge = _pad("PURGE", "1", center=Point2d(1.0, 2.0))
        selected = _pad("U1", "1", center=Point2d(3.0, 4.0))

        resolved, error = resolve_dataset_initial_purge(
            amount_ul=0.1,
            pad_id="U1.1",
            hierarchy=_hierarchy([purge, selected]),
        )

        assert error is None
        assert isinstance(resolved, ResolvedInitialPurge)
        assert resolved.pad is selected
        assert resolved.pad_id == "U1.1"
        assert resolved.source == "explicit"

    def test_missing_default_returns_error_for_preflight(self):
        sample = _pad("U1", "1", center=Point2d(3.0, 4.0))

        resolved, error = resolve_dataset_initial_purge(
            amount_ul=0.1,
            pad_id=None,
            hierarchy=_hierarchy([sample]),
        )

        assert resolved is None
        assert error is not None
        assert DATASET_PURGE_PAD_ID in error


class TestValidateInitialPurge:
    """validate_initial_purge は resolve と同一規則の None 返却バリデーション。

    ただし ``amount_ul == 0``（機能無効）でも ``pad_id`` 指定時は pad の
    存在とレイヤを検証する。
    """

    def test_valid_settings_return_none(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        error = validate_initial_purge(
            amount_ul=0.1,
            pad_id="R1.1",
            hierarchy=hierarchy,
            routed_pads=[pad],
        )

        assert error is None

    def test_amount_zero_without_pad_returns_none(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        error = validate_initial_purge(
            amount_ul=0.0,
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[pad],
        )

        assert error is None

    def test_amount_zero_valid_pad_returns_none(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        error = validate_initial_purge(
            amount_ul=0.0,
            pad_id="R1.1",
            hierarchy=hierarchy,
            routed_pads=[pad],
        )

        assert error is None

    def test_amount_zero_unknown_pad_returns_error(self):
        # resolve_initial_purge は amount=0 で pad 解決をスキップするが、
        # validate は不正 pad の保存を弾くため存在検証する。
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        error = validate_initial_purge(
            amount_ul=0.0,
            pad_id="R9.9",
            hierarchy=hierarchy,
            routed_pads=[pad],
        )

        assert error is not None
        assert "R9.9" in error
        assert "未知" in error

    def test_amount_zero_bottom_pad_returns_error(self):
        top = _pad("R1", "1", center=Point2d(0.0, 0.0))
        bottom = _pad("R3", "1", center=Point2d(1.0, 1.0), layer=Layer.BOTTOM)
        hierarchy = _hierarchy([top, bottom])

        error = validate_initial_purge(
            amount_ul=0.0,
            pad_id="R3.1",
            hierarchy=hierarchy,
            routed_pads=[top],
        )

        assert error is not None
        assert "R3.1" in error
        assert "Top" in error

    def test_negative_amount_returns_error(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        error = validate_initial_purge(
            amount_ul=-0.1,
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[pad],
        )

        assert error is not None
        assert "0以上" in error

    def test_error_text_matches_resolve_initial_purge(self):
        # エラー文言は resolve_initial_purge と同一ソース（文言の乖離防止）
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        _, resolve_error = resolve_initial_purge(
            amount_ul=0.1,
            pad_id="R9.9",
            hierarchy=hierarchy,
            routed_pads=[pad],
        )
        validate_error = validate_initial_purge(
            amount_ul=0.1,
            pad_id="R9.9",
            hierarchy=hierarchy,
            routed_pads=[pad],
        )

        assert validate_error == resolve_error
