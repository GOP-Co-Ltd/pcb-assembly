"""シミュレーションのカメラに映す基板（pcbasm.align.scene）の仕様テスト."""

import math

import pytest

from pcbasm.align.board_frame import Corner, marker_position
from pcbasm.align.scene import board_scene, placed_on_reference
from pcbasm.board import Board, Layer
from pcbasm.config.reference_point import CornerOffsetsConfig, ReferencePointConfig
from pcbasm.geometry import Compose, Point2d, Shift
from pcbasm.kicad import load_board
from tests.support.paths import TESTING_DATA_DIR

REFERENCE = ReferencePointConfig(
    x=30.0,
    y=40.0,
    target_diameter=1.0,
    offsets=CornerOffsetsConfig(
        top_left=(2.0, 3.0),
        top_right=(-2.0, 3.0),
        bottom_left=(2.0, -3.0),
        bottom_right=(-2.0, -3.0),
    ),
)
PLACEMENT = Shift(10.0, 20.0)


@pytest.fixture(scope="module")
def board() -> Board:
    return load_board(TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb")


class TestPlacedOnReference:
    def test_puts_the_top_left_marker_on_the_reference_point(self, board: Board):
        marker = marker_position(board.outline, REFERENCE.offsets, Corner.TOP_LEFT)

        assert placed_on_reference(REFERENCE).apply(marker) == Point2d(30.0, 40.0)


class TestBoardScene:
    def test_draws_a_marker_circle_on_each_corner_then_the_top_copper(
        self, board: Board
    ):
        shapes = board_scene(board, REFERENCE, PLACEMENT, PLACEMENT)

        corners = (
            Corner.TOP_LEFT,
            Corner.TOP_RIGHT,
            Corner.BOTTOM_RIGHT,
            Corner.BOTTOM_LEFT,
        )
        for circle, corner in zip(shapes[:4], corners, strict=True):
            center = PLACEMENT.apply(
                marker_position(board.outline, REFERENCE.offsets, corner)
            )
            assert circle.centroid.x == pytest.approx(center.x)
            assert circle.centroid.y == pytest.approx(center.y)
            assert circle.area == pytest.approx(math.pi * 0.5**2, rel=1e-3)
        top = [island for island in board.copper if island.layer == Layer.TOP]
        assert len(shapes) == len(corners) + len(top)

    def test_moves_the_copper_without_the_markers(self, board: Board):
        aligned = board_scene(board, REFERENCE, PLACEMENT, PLACEMENT)
        shifted = board_scene(
            board, REFERENCE, PLACEMENT, Compose([PLACEMENT, Shift(0.1, 0.0)])
        )

        assert [shape.bounds for shape in shifted[:4]] == [
            shape.bounds for shape in aligned[:4]
        ]
        for moved, original in zip(shifted[4:], aligned[4:], strict=True):
            assert moved.bounds[0] == pytest.approx(original.bounds[0] + 0.1)
            assert moved.bounds[1] == pytest.approx(original.bounds[1])
