"""重複する成功領域から pad 別補正を導く仕様テストと、照合の失敗を None で返す契約."""

from datetime import UTC, datetime

import numpy as np
import pytest
import shapely
from shapely.affinity import rotate

from pcbasm.align import (
    AlignmentRegion,
    BoardAlignment,
    BoardFrame,
    EdgeMatch,
    RegionAlignment,
    RegionAlignmentSession,
    is_pad_refinement_target,
)
from pcbasm.board import Layer, Pad
from pcbasm.config import load_machine
from pcbasm.geometry import Identity, Point2d
from pcbasm.hal import XYZStage
from pcbasm.hal.fake import FakeCamera, FakeKlipper
from pcbasm.kicad import load_board
from pcbasm.vision import Image, Offset
from pcbasm.vision.calibration import CalibrationResult
from tests.support.paths import TESTING_CONFIG_DIR, TESTING_DATA_DIR

PPM = 10.0


def _alignment(
    index: int,
    board_area: shapely.Polygon,
    displacement: Point2d,
    *,
    rms_distance_px: float = 0.0,
) -> RegionAlignment:
    center = board_area.centroid
    return RegionAlignment(
        region=AlignmentRegion(
            index=index,
            board_center=Point2d(center.x, center.y),
            anchor=Point2d(center.x, center.y),
            roi=(0, 0, 100, 100),
            board_area=board_area,
        ),
        match=EdgeMatch(
            offset=Offset(px=Point2d(0.0, 0.0), pixel_per_mm=PPM),
            rms_distance_px=rms_distance_px,
        ),
        displacement=displacement,
        increment=Point2d(0.0, 0.0),
        passes=1,
    )


def _shift_at(alignment: BoardAlignment, point: Point2d) -> Point2d:
    correction = alignment.correction_for(point, designator="U1")
    return correction.apply(point) - point


def _pad(polygon: shapely.Polygon) -> Pad:
    return Pad(
        designator="U1",
        pad_number="1",
        net_name="",
        layer=Layer.TOP,
        polygon=polygon,
    )


class TestIsPadRefinementTarget:
    """Paste開口の最小回転外接矩形短辺による逐次位置合わせ対象判定."""

    @pytest.mark.parametrize(
        ("short_side", "expected"),
        [
            (0.2, True),
            (0.4, True),
            (0.4000000005, True),
            (0.400000002, False),
            (0.5, False),
        ],
        ids=["below", "equal", "within-tolerance", "outside-tolerance", "above"],
    )
    def test_compares_short_side_with_inclusive_tolerant_boundary(
        self, short_side: float, expected: bool
    ):
        pad = _pad(shapely.box(-2.0, -short_side / 2, 2.0, short_side / 2))

        assert is_pad_refinement_target(pad, max_short_side_mm=0.4) is expected

    def test_is_invariant_to_arbitrary_pad_rotation(self):
        polygon = rotate(
            shapely.box(-1.5, -0.2, 1.5, 0.2),
            37.0,
            origin="centroid",
        )

        assert is_pad_refinement_target(_pad(polygon), max_short_side_mm=0.4)

    def test_zero_threshold_disables_refinement_for_every_pad_size(self):
        pad = _pad(shapely.box(-0.05, -0.05, 0.05, 0.05))

        assert not is_pad_refinement_target(pad, max_short_side_mm=0.0)

    @pytest.mark.parametrize(
        "polygon",
        [
            shapely.Polygon(),
            shapely.Polygon([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]),
        ],
        ids=["empty", "degenerate"],
    )
    def test_empty_and_degenerate_polygons_are_not_targets(
        self, polygon: shapely.Polygon
    ):
        assert not is_pad_refinement_target(_pad(polygon), max_short_side_mm=0.4)


class TestBoardAlignmentCorrectionFor:
    """重複する成功領域から整合性の高い machine displacement を採用する."""

    def test_uses_nearest_center_when_corrections_and_rms_are_tied(self):
        alignment = BoardAlignment(
            results=(
                _alignment(
                    0,
                    shapely.box(-4.0, -2.0, 2.0, 2.0),
                    Point2d(0.50, 0.30),
                ),
                _alignment(
                    1,
                    shapely.box(-1.0, -1.0, 1.0, 1.0),
                    Point2d(0.10, -0.20),
                ),
            )
        )

        shift = _shift_at(alignment, Point2d(0.5, 0.0))

        assert shift.x == pytest.approx(0.10, abs=1e-12)
        assert shift.y == pytest.approx(-0.20, abs=1e-12)

    def test_uses_correction_most_consistent_with_overlapping_regions(self):
        alignment = BoardAlignment(
            results=(
                _alignment(
                    0,
                    shapely.box(-1.0, -1.0, 1.0, 1.0),
                    Point2d(0.80, 0.40),
                ),
                _alignment(
                    1,
                    shapely.box(-2.0, -1.0, 0.5, 1.0),
                    Point2d(0.10, -0.10),
                ),
                _alignment(
                    2,
                    shapely.box(-0.5, -1.0, 2.0, 1.0),
                    Point2d(0.12, -0.08),
                ),
            )
        )

        shift = _shift_at(alignment, Point2d(0.0, 0.0))

        assert shift.x == pytest.approx(0.12, abs=1e-12)
        assert shift.y == pytest.approx(-0.08, abs=1e-12)

    def test_uses_lower_rms_when_consistency_is_tied(self):
        alignment = BoardAlignment(
            results=(
                _alignment(
                    0,
                    shapely.box(-1.0, -1.0, 1.0, 1.0),
                    Point2d(0.40, 0.20),
                    rms_distance_px=1.5,
                ),
                _alignment(
                    1,
                    shapely.box(-0.5, -1.0, 2.0, 1.0),
                    Point2d(0.10, -0.20),
                    rms_distance_px=0.5,
                ),
            )
        )

        shift = _shift_at(alignment, Point2d(0.0, 0.0))

        assert shift.x == pytest.approx(0.10, abs=1e-12)
        assert shift.y == pytest.approx(-0.20, abs=1e-12)

    def test_different_pads_select_different_covering_region_sets(self):
        alignment = BoardAlignment(
            results=(
                _alignment(
                    0,
                    shapely.box(-2.0, -2.0, 0.0, 2.0),
                    Point2d(0.10, -0.10),
                ),
                _alignment(
                    1,
                    shapely.box(0.0, -2.0, 2.0, 2.0),
                    Point2d(0.40, 0.20),
                ),
            )
        )

        left = _shift_at(alignment, Point2d(-1.0, 0.0))
        right = _shift_at(alignment, Point2d(1.0, 0.0))

        assert left.x == pytest.approx(0.10, abs=1e-12)
        assert left.y == pytest.approx(-0.10, abs=1e-12)
        assert right.x == pytest.approx(0.40, abs=1e-12)
        assert right.y == pytest.approx(0.20, abs=1e-12)

    def test_returns_a_pure_translation(self):
        alignment = BoardAlignment(
            results=(
                _alignment(
                    0,
                    shapely.box(-2.0, -2.0, 2.0, 2.0),
                    Point2d(0.25, -0.15),
                ),
            )
        )
        correction = alignment.correction_for(Point2d(0.0, 0.0), designator="R1")

        for point in (Point2d(0.0, 0.0), Point2d(100.0, -50.0)):
            shift = correction.apply(point) - point
            assert shift.x == pytest.approx(0.25, abs=1e-12)
            assert shift.y == pytest.approx(-0.15, abs=1e-12)

    def test_uses_nearest_success_region_when_no_region_covers_point(self):
        alignment = BoardAlignment(
            results=(
                _alignment(
                    0,
                    shapely.box(-1.0, -1.0, 1.0, 1.0),
                    Point2d(0.25, -0.15),
                ),
                _alignment(
                    1,
                    shapely.box(9.0, 9.0, 11.0, 11.0),
                    Point2d(-0.10, 0.30),
                ),
            )
        )

        shift = _shift_at(alignment, Point2d(20.0, 20.0))

        assert shift.x == pytest.approx(-0.10, abs=1e-12)
        assert shift.y == pytest.approx(0.30, abs=1e-12)

    def test_uses_fallback_region_when_no_refined_pad_covers_point(self):
        refined = _alignment(
            0,
            shapely.box(-1.0, -1.0, 1.0, 1.0),
            Point2d(0.25, -0.15),
        )
        fallback = _alignment(
            1,
            shapely.box(9.0, 9.0, 11.0, 11.0),
            Point2d(-0.10, 0.30),
        )
        alignment = BoardAlignment(results=(refined,), fallback_results=(fallback,))

        shift = _shift_at(alignment, Point2d(10.0, 10.0))

        assert shift.x == pytest.approx(-0.10, abs=1e-12)
        assert shift.y == pytest.approx(0.30, abs=1e-12)

    def test_no_success_region_raises(self):
        alignment = BoardAlignment(results=())

        with pytest.raises(ValueError) as exc_info:
            alignment.correction_for(Point2d(20.0, 20.0), designator="C17")

        assert "C17" in str(exc_info.value)


class TestRegionAlignmentSessionFailure:
    """照合の失敗は None で返し、呼び出し側は残りの領域で続ける.

    Klipper が送信を断った KlipperError も、RuntimeError の派生なので領域の失敗になる。
    """

    @pytest.fixture
    def klipper(self) -> FakeKlipper:
        klipper = FakeKlipper()
        klipper.fail_on(lambda gcode: True)
        return klipper

    @pytest.fixture
    def session(self, klipper: FakeKlipper) -> RegionAlignmentSession:
        resolution = (640, 480)
        frame = Image(np.zeros((resolution[1], resolution[0], 3), dtype=np.uint8))
        return RegionAlignmentSession(
            BoardFrame(
                board=load_board(
                    TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"
                ),
                calibration=CalibrationResult(
                    pixel_per_mm=PPM,
                    square_size_mm=1.0,
                    mean_distance_px=PPM,
                    std_distance_px=0.0,
                    resolution=resolution,
                    crop_size=(400, 400),
                    calibrated_at=datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC),
                    z_position=12.0,
                ),
                offset_transform=Identity(),
                board_transform=Identity(),
            ),
            load_machine(TESTING_CONFIG_DIR / "machine.toml"),
            klipper=klipper,
            stage=XYZStage(klipper),
            camera=FakeCamera([frame]),
        )

    def test_klipper_refusal_fails_the_region(
        self, klipper: FakeKlipper, session: RegionAlignmentSession
    ):
        region = AlignmentRegion(
            index=0,
            board_center=Point2d(10.0, 10.0),
            anchor=Point2d(10.0, 10.0),
            roi=session.region_roi,
            board_area=shapely.box(9.0, 9.0, 11.0, 11.0),
        )

        assert session.align(region) is None
        # 最初の移動で断られ、撮像へ進んでいない
        assert len(klipper.sent) == 1

    def test_klipper_refusal_fails_the_pad_refinement(
        self, klipper: FakeKlipper, session: RegionAlignmentSession
    ):
        refined = session.refine(
            Point2d(10.0, 10.0), Identity(), shapely.box(9.5, 9.5, 10.5, 10.5)
        )

        assert refined is None
        assert len(klipper.sent) == 1
