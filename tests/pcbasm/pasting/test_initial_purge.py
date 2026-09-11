"""初回パージ位置（座標）の解決の公開契約テスト.

パージは pad ではなく座標で扱う。明示指定が無ければ塗布順路先頭 pad の中心を使う。
"""

import pytest
from shapely import Polygon

from pcbasm.geometry import Point2d
from pcbasm.pasting.initial_purge import (
    ResolvedInitialPurge,
    purge_point_label,
    resolve_initial_purge,
    resolve_initial_purge_for,
    validate_initial_purge,
)
from pcbasm.pcb import Layer, Pad


def _outline(size: float = 20.0) -> Polygon:
    """基板外形（原点中心の正方形）."""
    half = size / 2.0
    return Polygon([(-half, -half), (half, -half), (half, half), (-half, half)])


def _pad(designator: str, pad_number: str, *, center: Point2d) -> Pad:
    return Pad(
        designator=designator,
        pad_number=pad_number,
        net_name="",
        layer=Layer.TOP,
        polygon=Polygon(
            [
                (center.x - 0.5, center.y - 0.5),
                (center.x + 0.5, center.y - 0.5),
                (center.x + 0.5, center.y + 0.5),
                (center.x - 0.5, center.y + 0.5),
            ]
        ),
    )


class TestResolveInitialPurge:
    """明示座標 > 順路先頭 pad の中心、の順で塗布点を決める."""

    def test_default_uses_the_first_routed_pad_center(self):
        first = _pad("U1", "2", center=Point2d(1.0, 2.0))
        later = _pad("R1", "1", center=Point2d(5.0, 0.0))

        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            point=None,
            routed_pads=[first, later],
            outline=_outline(),
        )

        assert error is None
        assert isinstance(resolved, ResolvedInitialPurge)
        assert resolved.point == first.center
        assert resolved.source == "default"
        assert resolved.amount_ul == pytest.approx(0.1)

    def test_explicit_point_overrides_the_route_head(self):
        routed = _pad("R1", "1", center=Point2d(0.0, 0.0))

        resolved, error = resolve_initial_purge(
            amount_ul=0.25,
            point=Point2d(-6.0, 7.5),
            routed_pads=[routed],
            outline=_outline(),
        )

        assert error is None
        assert resolved is not None
        assert resolved.point == Point2d(-6.0, 7.5)
        assert resolved.source == "explicit"

    def test_label_shows_the_coordinates(self):
        resolved, _ = resolve_initial_purge(
            amount_ul=0.1,
            point=Point2d(1.5, -2.25),
            routed_pads=[],
            outline=_outline(),
        )

        assert resolved is not None
        assert resolved.label == purge_point_label(Point2d(1.5, -2.25))
        assert "1.50" in resolved.label
        assert "-2.25" in resolved.label

    def test_empty_route_without_a_point_resolves_to_nothing(self):
        resolved, error = resolve_initial_purge(
            amount_ul=0.1, point=None, routed_pads=[], outline=_outline()
        )

        assert error is None
        assert resolved is None

    def test_amount_zero_disables_initial_purge(self):
        resolved, error = resolve_initial_purge(
            amount_ul=0.0,
            point=Point2d(1.0, 1.0),
            routed_pads=[_pad("R1", "1", center=Point2d(0.0, 0.0))],
            outline=_outline(),
        )

        assert error is None
        assert resolved is None

    def test_point_outside_the_board_outline_returns_error_text(self):
        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            point=Point2d(50.0, 0.0),
            routed_pads=[],
            outline=_outline(),
        )

        assert resolved is None
        assert error is not None
        assert "基板外形" in error

    @pytest.mark.parametrize("value", [float("nan"), float("inf")])
    def test_non_finite_point_returns_error_text(self, value: float):
        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            point=Point2d(value, 0.0),
            routed_pads=[],
            outline=_outline(),
        )

        assert resolved is None
        assert error is not None

    @pytest.mark.parametrize("amount", [-0.1, float("nan")])
    def test_invalid_amount_returns_error_text(self, amount: float):
        resolved, error = resolve_initial_purge(
            amount_ul=amount, point=None, routed_pads=[], outline=_outline()
        )

        assert resolved is None
        assert error is not None


class TestValidateInitialPurge:
    """Resolve と同一規則の None 返却バリデーション（無効化中も座標は検証する）."""

    @pytest.mark.parametrize("point", [Point2d(1.0, 1.0), None])
    def test_valid_settings_return_none(self, point: Point2d | None):
        assert (
            validate_initial_purge(amount_ul=0.1, point=point, outline=_outline())
            is None
        )

    def test_amount_zero_still_validates_an_off_board_point(self):
        error = validate_initial_purge(
            amount_ul=0.0, point=Point2d(50.0, 0.0), outline=_outline()
        )

        assert error is not None
        assert "基板外形" in error

    def test_negative_amount_returns_error(self):
        error = validate_initial_purge(amount_ul=-0.1, point=None, outline=_outline())

        assert error is not None
        assert "0以上" in error

    def test_error_text_matches_resolve_initial_purge(self):
        # エラー文言は resolve_initial_purge と同一ソース（文言の乖離防止）
        point = Point2d(50.0, 0.0)
        _, resolve_error = resolve_initial_purge(
            amount_ul=0.1, point=point, routed_pads=[], outline=_outline()
        )
        validate_error = validate_initial_purge(
            amount_ul=0.1, point=point, outline=_outline()
        )

        assert validate_error == resolve_error


class TestResolveInitialPurgeFor:
    """UI 表示用に解決結果と既定座標をまとめて返す."""

    def test_reports_the_route_head_center_as_the_default_point(self):
        first = _pad("U1", "2", center=Point2d(1.0, 2.0))

        resolution = resolve_initial_purge_for(
            amount_ul=0.1,
            point=Point2d(3.0, 4.0),
            routed_pads=[first],
            outline=_outline(),
        )

        assert resolution.error is None
        assert resolution.default_point == first.center
        assert resolution.resolved is not None
        assert resolution.resolved.point == Point2d(3.0, 4.0)

    def test_default_point_is_none_without_a_route(self):
        resolution = resolve_initial_purge_for(
            amount_ul=0.1, point=None, routed_pads=[], outline=_outline()
        )

        assert resolution.default_point is None
        assert resolution.resolved is None

    def test_error_is_reported_without_a_resolution(self):
        resolution = resolve_initial_purge_for(
            amount_ul=0.1,
            point=Point2d(50.0, 0.0),
            routed_pads=[],
            outline=_outline(),
        )

        assert resolution.resolved is None
        assert resolution.error is not None
