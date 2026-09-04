"""PasteSession の pad 別座標変換契約."""

from typing import Any

import pytest
import shapely

from pcbasm.geometry import HeightPlane, Point2d, Point3d, Rotation, Shift
from pcbasm.pasting.alignment import PasteCorrection
from pcbasm.pasting.session import PasteSession
from pcbasm.pcb import Layer, Pad
from pcbasm.posctrl import AlignmentRegion, BoardAlignment, EdgeMatch, RegionAlignment
from pcbasm.vision import Offset

PPM = 10.0


def _region_result(
    index: int,
    area: shapely.Polygon,
    displacement: Point2d,
) -> RegionAlignment:
    center = area.centroid
    return RegionAlignment(
        region=AlignmentRegion(
            index=index,
            board_center=Point2d(center.x, center.y),
            anchor=Point2d(center.x, center.y),
            roi=(0, 0, 100, 100),
            board_area=area,
        ),
        match=EdgeMatch(
            offset=Offset(px=Point2d(0.0, 0.0), pixel_per_mm=PPM),
            rms_distance_px=0.0,
        ),
        displacement=displacement,
        increment=Point2d(0.0, 0.0),
        passes=1,
    )


def _height_plane() -> HeightPlane:
    points = [
        (0.0, 0.0),
        (3.0, 0.0),
        (0.0, 4.0),
        (3.0, 4.0),
        (1.0, 2.0),
        (2.0, 1.0),
    ]
    return HeightPlane(tuple(Point3d(x, y, x + 2.0 * y) for x, y in points))


def _session() -> PasteSession:
    unused: Any = object()
    return PasteSession(
        machine=unused,
        klipper=unused,
        stage=unused,
        camera=unused,
        calibration=unused,
        calibration_result=unused,
        board_transform=Rotation(90.0),
        offset_transform=unused,
        toolhead_offset=Shift(10.0, 20.0),
        pcb=unused,
        probe_executor=unused,
        height_measurer=unused,
    )


def _pad(designator: str, center: Point2d) -> Pad:
    return Pad(
        designator=designator,
        pad_number="1",
        net_name="NET",
        layer=Layer.TOP,
        polygon=shapely.box(
            center.x - 0.2,
            center.y - 0.2,
            center.x + 0.2,
            center.y + 0.2,
        ),
    )


class TestPasteSessionPadTransform:
    """Board→局所補正→toolhead→height の合成順を検証する."""

    def test_applies_transforms_in_agreed_order(self):
        pad = _pad("U1", Point2d(1.0, 2.0))
        area = shapely.box(-5.0, -5.0, 5.0, 5.0)
        alignment = BoardAlignment(
            results=(_region_result(0, area, Point2d(0.3, 0.1)),)
        )

        moved = (
            _session()
            .pad_transform(pad, PasteCorrection(alignment, _height_plane()))
            .apply(pad.center.to3d(0.4))
        )

        # Rotation90(1,2)=(-2,1), 局所補正=(0.3,0.1),
        # toolhead=(10,20) より最終 XY=(8.3,21.1)。
        assert moved.x == pytest.approx(8.3, abs=1e-9)
        assert moved.y == pytest.approx(21.1, abs=1e-9)
        # HeightPlane は最終ノズル XY で z=x+2y を評価する。
        assert moved.z == pytest.approx(0.4 + moved.x + 2.0 * moved.y, abs=1e-9)


class TestPasteSessionPadTransforms:
    """Paste と purge が同じ pad 別変換を利用できる契約."""

    def test_preserves_input_order_and_uses_each_pads_correction(self):
        left_area = shapely.box(-2.0, -2.0, 0.0, 2.0)
        right_area = shapely.box(0.0, -2.0, 2.0, 2.0)
        left_shift = Point2d(0.1, -0.2)
        right_shift = Point2d(0.4, 0.3)
        alignment = BoardAlignment(
            results=(
                _region_result(0, left_area, left_shift),
                _region_result(1, right_area, right_shift),
            )
        )
        pads = [
            _pad("PURGE", Point2d(1.0, 0.0)),
            _pad("R1", Point2d(-1.0, 0.0)),
            _pad("R2", Point2d(1.0, 0.0)),
        ]
        session = _session()

        correction = PasteCorrection(alignment, _height_plane())
        entries = [(pad, session.pad_transform(pad, correction)) for pad in pads]

        expected_shifts = [right_shift, left_shift, right_shift]
        for (pad, transform), expected in zip(entries, expected_shifts, strict=True):
            moved = transform.apply(pad.center.to3d(0.0))
            base = session.toolhead_offset.apply(
                session.board_transform.apply(pad.center).to3d(0.0)
            )
            assert moved.x - base.x == pytest.approx(expected.x, abs=1e-9)
            assert moved.y - base.y == pytest.approx(expected.y, abs=1e-9)

    def test_uncovered_pad_uses_nearest_success_region(self):
        expected = Point2d(0.1, -0.2)
        alignment = BoardAlignment(
            results=(
                _region_result(
                    0,
                    shapely.box(-2.0, -2.0, 0.0, 2.0),
                    expected,
                ),
            )
        )
        pads = [
            _pad("R1", Point2d(-1.0, 0.0)),
            _pad("C17", Point2d(5.0, 0.0)),
        ]
        session = _session()

        correction = PasteCorrection(alignment, _height_plane())
        entries = [(pad, session.pad_transform(pad, correction)) for pad in pads]

        for pad, transform in entries:
            moved = transform.apply(pad.center.to3d(0.0))
            base = session.toolhead_offset.apply(
                session.board_transform.apply(pad.center).to3d(0.0)
            )
            assert moved.x - base.x == pytest.approx(expected.x, abs=1e-9)
            assert moved.y - base.y == pytest.approx(expected.y, abs=1e-9)

    def test_no_success_region_aborts_with_designator(self):
        pad = _pad("C17", Point2d(5.0, 0.0))

        with pytest.raises(ValueError) as exc_info:
            _session().pad_transform(
                pad, PasteCorrection(BoardAlignment(results=()), _height_plane())
            )

        assert "C17" in str(exc_info.value)


class TestPasteSessionCameraTarget:
    """補正済み pad 中心をカメラ中心へ置くステージ XY（toolhead / height を含まない）."""

    def test_applies_board_transform_and_region_correction_only(self):
        pad = _pad("U1", Point2d(1.0, 2.0))
        alignment = BoardAlignment(
            results=(
                _region_result(0, shapely.box(-5.0, -5.0, 5.0, 5.0), Point2d(0.3, 0.1)),
            )
        )
        correction = PasteCorrection(alignment, _height_plane())

        target = _session().camera_target(pad, correction)

        # Rotation90(1,2)=(-2,1) + 局所補正 (0.3,0.1)。toolhead offset は加えない。
        assert target.x == pytest.approx(-1.7, abs=1e-9)
        assert target.y == pytest.approx(1.1, abs=1e-9)

    def test_offset_is_added_in_machine_xy(self):
        pad = _pad("U1", Point2d(1.0, 2.0))
        correction = PasteCorrection(
            BoardAlignment(
                results=(
                    _region_result(
                        0, shapely.box(-5.0, -5.0, 5.0, 5.0), Point2d(0.0, 0.0)
                    ),
                )
            ),
            _height_plane(),
        )

        plain = _session().camera_target(pad, correction)
        shifted = _session().camera_target(pad, correction, offset=Point2d(0.5, -1.0))

        assert shifted.x - plain.x == pytest.approx(0.5, abs=1e-9)
        assert shifted.y - plain.y == pytest.approx(-1.0, abs=1e-9)
