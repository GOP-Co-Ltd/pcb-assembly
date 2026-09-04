"""pcbasm.geometry.packing のテスト."""

import pytest

from pcbasm.geometry.packing import Rect, pack_rects


def _separated(first: Rect, second: Rect, gap: float) -> bool:
    return (
        first.right + gap <= second.x + 1e-9
        or second.right + gap <= first.x + 1e-9
        or first.bottom + gap <= second.y + 1e-9
        or second.bottom + gap <= first.y + 1e-9
    )


class TestRect:
    def test_edges_and_containment(self):
        outer = Rect(0.0, 0.0, 10.0, 5.0)
        inner = Rect(2.0, 1.0, 3.0, 2.0)
        assert (outer.right, outer.bottom) == (10.0, 5.0)
        assert outer.contains(inner) and not inner.contains(outer)
        assert outer.intersects(inner)
        assert not inner.intersects(Rect(5.0, 1.0, 1.0, 1.0))  # 辺で接するだけ


class TestPackRects:
    def test_placements_keep_input_order_and_do_not_overlap(self):
        sizes = [(3.0, 2.0), (1.0, 1.0), (2.0, 2.0), (1.0, 3.0)]
        area = Rect(1.0, 1.0, 8.0, 6.0)
        placed = pack_rects(sizes, area, gap=0.5)
        assert placed is not None
        assert len(placed) == len(sizes)
        for rect, (width, height) in zip(placed, sizes, strict=True):
            assert (rect.width, rect.height) == (width, height)
            assert area.contains(rect)
        for i, first in enumerate(placed):
            assert all(_separated(first, second, 0.5) for second in placed[i + 1 :])

    def test_keepout_is_never_overlapped(self):
        keepout = Rect(0.0, 0.0, 3.0, 3.0)
        placed = pack_rects(
            [(2.0, 2.0)] * 3, Rect(0.0, 0.0, 6.0, 6.0), keepouts=(keepout,)
        )
        assert placed is not None
        assert all(not rect.intersects(keepout) for rect in placed)

    def test_returns_none_when_it_cannot_fit(self):
        assert pack_rects([(5.0, 5.0), (5.0, 5.0)], Rect(0.0, 0.0, 6.0, 6.0)) is None

    def test_prefers_compact_layout(self):
        placed = pack_rects([(1.0, 1.0)] * 4, Rect(0.0, 0.0, 10.0, 10.0))
        assert placed is not None
        used_right = max(rect.right for rect in placed)
        used_bottom = max(rect.bottom for rect in placed)
        assert used_right * used_bottom == pytest.approx(4.0)

    def test_empty_input_returns_empty(self):
        assert pack_rects([], Rect(0.0, 0.0, 1.0, 1.0)) == ()
