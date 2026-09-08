"""Initial purge の対象解決（任意点 / pad / 順路先頭）の公開契約テスト."""

import pytest
from shapely import Polygon

from pcbasm.geometry import Point2d
from pcbasm.pasting.initial_purge import (
    ResolvedInitialPurge,
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


def _outline(size: float = 20.0) -> Polygon:
    """基板外形（原点中心の正方形）。point 検証に使う."""
    half = size / 2.0
    return Polygon([(-half, -half), (half, -half), (half, half), (-half, half)])


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
            point=None,
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[first_routed, later],
            outline=_outline(),
        )

        assert error is None
        assert isinstance(resolved, ResolvedInitialPurge)
        assert resolved.pad_id == "U1.2"
        assert resolved.point == first_routed.center
        assert resolved.amount_ul == pytest.approx(0.1)

    def test_explicit_disabled_top_pad_is_allowed(self):
        enabled = _pad("R1", "1", center=Point2d(0.0, 0.0))
        disabled = _pad("U1", "1", center=Point2d(3.0, 4.0))
        hierarchy = _hierarchy([enabled, disabled])

        resolved, error = resolve_initial_purge(
            amount_ul=0.25,
            point=None,
            pad_id="U1.1",
            hierarchy=hierarchy,
            routed_pads=[enabled],
            outline=_outline(),
        )

        assert error is None
        assert isinstance(resolved, ResolvedInitialPurge)
        assert resolved.pad_id == "U1.1"
        assert resolved.point == disabled.center
        assert resolved.amount_ul == pytest.approx(0.25)

    def test_amount_zero_disables_initial_purge(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        resolved, error = resolve_initial_purge(
            amount_ul=0.0,
            point=None,
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[pad],
            outline=_outline(),
        )

        assert error is None
        assert resolved is None

    def test_unknown_explicit_pad_returns_error_text(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            point=None,
            pad_id="R9.9",
            hierarchy=hierarchy,
            routed_pads=[pad],
            outline=_outline(),
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
            point=None,
            pad_id="R3.1",
            hierarchy=hierarchy,
            routed_pads=[top],
            outline=_outline(),
        )

        assert resolved is None
        assert error is not None
        assert "R3.1" in error
        assert "Top" in error


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
            point=None,
            pad_id="R1.1",
            hierarchy=hierarchy,
            routed_pads=[pad],
            outline=_outline(),
        )

        assert error is None

    def test_amount_zero_without_pad_returns_none(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        error = validate_initial_purge(
            amount_ul=0.0,
            point=None,
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[pad],
            outline=_outline(),
        )

        assert error is None

    def test_amount_zero_valid_pad_returns_none(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        error = validate_initial_purge(
            amount_ul=0.0,
            point=None,
            pad_id="R1.1",
            hierarchy=hierarchy,
            routed_pads=[pad],
            outline=_outline(),
        )

        assert error is None

    def test_amount_zero_unknown_pad_returns_error(self):
        # resolve_initial_purge は amount=0 で pad 解決をスキップするが、
        # validate は不正 pad の保存を弾くため存在検証する。
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        error = validate_initial_purge(
            amount_ul=0.0,
            point=None,
            pad_id="R9.9",
            hierarchy=hierarchy,
            routed_pads=[pad],
            outline=_outline(),
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
            point=None,
            pad_id="R3.1",
            hierarchy=hierarchy,
            routed_pads=[top],
            outline=_outline(),
        )

        assert error is not None
        assert "R3.1" in error
        assert "Top" in error

    def test_negative_amount_returns_error(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        error = validate_initial_purge(
            amount_ul=-0.1,
            point=None,
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[pad],
            outline=_outline(),
        )

        assert error is not None
        assert "0以上" in error

    def test_error_text_matches_resolve_initial_purge(self):
        # エラー文言は resolve_initial_purge と同一ソース（文言の乖離防止）
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        _, resolve_error = resolve_initial_purge(
            amount_ul=0.1,
            point=None,
            pad_id="R9.9",
            hierarchy=hierarchy,
            routed_pads=[pad],
            outline=_outline(),
        )
        validate_error = validate_initial_purge(
            amount_ul=0.1,
            point=None,
            pad_id="R9.9",
            hierarchy=hierarchy,
            routed_pads=[pad],
            outline=_outline(),
        )

        assert validate_error == resolve_error


class TestResolveInitialPurgePoint:
    """任意点の指定は pad 指定と順路先頭より優先する."""

    def test_point_takes_priority_over_pad_id_and_route_head(self):
        routed = _pad("R1", "1", center=Point2d(0.0, 0.0))
        explicit = _pad("U1", "1", center=Point2d(3.0, 4.0))
        hierarchy = _hierarchy([routed, explicit])

        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            point=Point2d(-6.0, 7.5),
            pad_id="U1.1",
            hierarchy=hierarchy,
            routed_pads=[routed],
            outline=_outline(),
        )

        assert error is None
        assert isinstance(resolved, ResolvedInitialPurge)
        assert resolved.source == "point"
        assert resolved.point == Point2d(-6.0, 7.5)
        assert resolved.pad is None
        assert resolved.pad_id is None

    def test_point_label_shows_the_coordinates(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            point=Point2d(1.5, -2.25),
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[pad],
            outline=_outline(),
        )

        assert error is None
        assert resolved is not None
        assert "1.50" in resolved.label
        assert "-2.25" in resolved.label

    def test_pad_resolution_reports_the_pad_source_and_center(self):
        pad = _pad("R1", "1", center=Point2d(2.0, 3.0))
        hierarchy = _hierarchy([pad])

        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            point=None,
            pad_id="R1.1",
            hierarchy=hierarchy,
            routed_pads=[pad],
            outline=_outline(),
        )

        assert error is None
        assert resolved is not None
        assert resolved.source == "pad"
        assert resolved.pad is not None
        assert resolved.point == pad.center
        assert resolved.label == "R1.1"

    def test_point_outside_the_board_outline_returns_error_text(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            point=Point2d(50.0, 0.0),
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[pad],
            outline=_outline(),
        )

        assert resolved is None
        assert error is not None
        assert "基板外形" in error

    @pytest.mark.parametrize("value", [float("nan"), float("inf")])
    def test_non_finite_point_returns_error_text(self, value: float):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            point=Point2d(value, 0.0),
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[pad],
            outline=_outline(),
        )

        assert resolved is None
        assert error is not None

    def test_amount_zero_disables_the_point_purge(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        resolved, error = resolve_initial_purge(
            amount_ul=0.0,
            point=Point2d(1.0, 1.0),
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[pad],
            outline=_outline(),
        )

        assert error is None
        assert resolved is None

    def test_amount_zero_still_validates_an_off_board_point(self):
        # 無効化中でも不正な点を保存させない（pad 指定と同じ規則）
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        error = validate_initial_purge(
            amount_ul=0.0,
            point=Point2d(50.0, 0.0),
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[pad],
            outline=_outline(),
        )

        assert error is not None
        assert "基板外形" in error

    def test_point_resolution_ignores_a_missing_route(self):
        # 順路が空でも点指定なら解決できる（pad を必要としない）
        hierarchy = _hierarchy([_pad("R1", "1", center=Point2d(0.0, 0.0))])

        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            point=Point2d(1.0, 1.0),
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[],
            outline=_outline(),
        )

        assert error is None
        assert resolved is not None
        assert resolved.source == "point"
