"""位置合わせの本筋（領域照合 → pad 中心の精密照合 → 補正の選択）の仕様テスト."""

import logging

import attrs
import numpy as np
import pytest
import shapely
from shapely.affinity import rotate

from pcbasm.align import (
    AlignEvent,
    AlignmentRegion,
    AlignStep,
    BoardCorrection,
    BoardFrame,
    EdgeMatch,
    PadMeasured,
    PadRefinement,
    RefinementPlanned,
    RegionAlignmentSession,
    RegionFit,
    RegionMeasured,
    RegionsPlanned,
    align_board,
    align_regions,
    refinement_targets,
)
from pcbasm.board import Board, Layer, Pad
from pcbasm.config import MachineConfig, load_machine
from pcbasm.geometry import Identity, Point2d, Shift, Transform, transform_polygon
from pcbasm.hal import Camera, KlipperError, XYZStage
from pcbasm.hal.fake import FakeCamera, FakeKlipper, SceneCamera
from pcbasm.kicad import load_board
from pcbasm.vision import Image, Offset
from tests.support.camera_model import make_camera_model
from tests.support.paths import LED_BLINKER_PCB, TEST_CONFIG_DIR

PPM = 10.0


def _fit(
    index: int,
    board_area: shapely.Polygon,
    displacement: Point2d,
    *,
    rms_distance_px: float = 0.0,
) -> RegionFit:
    center = board_area.centroid
    return RegionFit(
        region=AlignmentRegion(
            index=index,
            board_center=Point2d(center.x, center.y),
            anchor=Point2d(center.x, center.y),
            roi=(0, 0, 100, 100),
            board_area=board_area,
        ),
        match=EdgeMatch(
            offset=Offset(px=Point2d(0.0, 0.0)),
            rms_distance_px=rms_distance_px,
        ),
        displacement=displacement,
        increment=Point2d(0.0, 0.0),
        passes=1,
    )


def _correction(*fits: RegionFit) -> BoardCorrection:
    return BoardCorrection(fits=fits, region_count=len(fits))


def _shift_at(correction: BoardCorrection, point: Point2d) -> Point2d:
    return correction.correction_for(point).apply(point) - point


def _pad(polygon: shapely.Polygon, designator: str = "U1") -> Pad:
    return Pad(
        designator=designator,
        pad_number="1",
        net_name="",
        layer=Layer.TOP,
        polygon=polygon,
    )


class TestRefinementTargets:
    """Paste 開口の最小回転外接矩形の短辺で、pad 中心の精密照合の対象を選ぶ."""

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

        assert (refinement_targets([pad], max_short_side_mm=0.4) == (pad,)) is expected

    def test_keeps_input_order_regardless_of_pad_orientation(self):
        narrow = _pad(shapely.box(0.0, 0.0, 0.3, 1.2), "R1")
        rotated = _pad(rotate(shapely.box(-1.5, -0.2, 1.5, 0.2), 37.0), "R2")
        wide = _pad(shapely.box(0.0, 0.0, 2.0, 2.0), "U1")

        targets = refinement_targets([wide, narrow, rotated], max_short_side_mm=0.4)

        assert targets == (narrow, rotated)

    def test_zero_threshold_disables_refinement_for_every_pad_size(self):
        pad = _pad(shapely.box(-0.05, -0.05, 0.05, 0.05))

        assert refinement_targets([pad], max_short_side_mm=0.0) == ()

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
        assert refinement_targets([_pad(polygon)], max_short_side_mm=0.4) == ()


class TestBoardCorrection:
    """重複する成功領域から整合性の高い machine displacement を採用する."""

    def test_uses_nearest_center_when_corrections_and_rms_are_tied(self):
        correction = _correction(
            _fit(0, shapely.box(-4.0, -2.0, 2.0, 2.0), Point2d(0.50, 0.30)),
            _fit(1, shapely.box(-1.0, -1.0, 1.0, 1.0), Point2d(0.10, -0.20)),
        )

        shift = _shift_at(correction, Point2d(0.5, 0.0))

        assert shift.x == pytest.approx(0.10, abs=1e-12)
        assert shift.y == pytest.approx(-0.20, abs=1e-12)

    def test_uses_correction_most_consistent_with_overlapping_regions(self):
        correction = _correction(
            _fit(0, shapely.box(-1.0, -1.0, 1.0, 1.0), Point2d(0.80, 0.40)),
            _fit(1, shapely.box(-2.0, -1.0, 0.5, 1.0), Point2d(0.10, -0.10)),
            _fit(2, shapely.box(-0.5, -1.0, 2.0, 1.0), Point2d(0.12, -0.08)),
        )

        shift = _shift_at(correction, Point2d(0.0, 0.0))

        assert shift.x == pytest.approx(0.12, abs=1e-12)
        assert shift.y == pytest.approx(-0.08, abs=1e-12)

    def test_uses_lower_rms_when_consistency_is_tied(self):
        correction = _correction(
            _fit(
                0,
                shapely.box(-1.0, -1.0, 1.0, 1.0),
                Point2d(0.40, 0.20),
                rms_distance_px=1.5,
            ),
            _fit(
                1,
                shapely.box(-0.5, -1.0, 2.0, 1.0),
                Point2d(0.10, -0.20),
                rms_distance_px=0.5,
            ),
        )

        shift = _shift_at(correction, Point2d(0.0, 0.0))

        assert shift.x == pytest.approx(0.10, abs=1e-12)
        assert shift.y == pytest.approx(-0.20, abs=1e-12)

    def test_different_pads_select_different_covering_region_sets(self):
        correction = _correction(
            _fit(0, shapely.box(-2.0, -2.0, 0.0, 2.0), Point2d(0.10, -0.10)),
            _fit(1, shapely.box(0.0, -2.0, 2.0, 2.0), Point2d(0.40, 0.20)),
        )

        left = _shift_at(correction, Point2d(-1.0, 0.0))
        right = _shift_at(correction, Point2d(1.0, 0.0))

        assert left.x == pytest.approx(0.10, abs=1e-12)
        assert left.y == pytest.approx(-0.10, abs=1e-12)
        assert right.x == pytest.approx(0.40, abs=1e-12)
        assert right.y == pytest.approx(0.20, abs=1e-12)

    def test_returns_a_pure_translation(self):
        correction = _correction(
            _fit(0, shapely.box(-2.0, -2.0, 2.0, 2.0), Point2d(0.25, -0.15))
        ).correction_for(Point2d(0.0, 0.0))

        for point in (Point2d(0.0, 0.0), Point2d(100.0, -50.0)):
            shift = correction.apply(point) - point
            assert shift.x == pytest.approx(0.25, abs=1e-12)
            assert shift.y == pytest.approx(-0.15, abs=1e-12)

    def test_uses_nearest_success_region_when_no_region_covers_point(self):
        correction = _correction(
            _fit(0, shapely.box(-1.0, -1.0, 1.0, 1.0), Point2d(0.25, -0.15)),
            _fit(1, shapely.box(9.0, 9.0, 11.0, 11.0), Point2d(-0.10, 0.30)),
        )

        shift = _shift_at(correction, Point2d(20.0, 20.0))

        assert shift.x == pytest.approx(-0.10, abs=1e-12)
        assert shift.y == pytest.approx(0.30, abs=1e-12)

    def test_prefers_refined_pad_over_region_that_covers_it(self):
        area = shapely.box(-1.0, -1.0, 1.0, 1.0)
        refined = _fit(-1, area, Point2d(0.05, 0.02))
        correction = attrs.evolve(
            _correction(_fit(0, area, Point2d(0.25, -0.15))),
            refinements=(PadRefinement(_pad(area), refined, Point2d(-0.2, 0.17)),),
        )

        shift = _shift_at(correction, Point2d(0.0, 0.0))

        assert shift.x == pytest.approx(0.05, abs=1e-12)
        assert shift.y == pytest.approx(0.02, abs=1e-12)

    def test_uses_region_when_no_refined_pad_covers_point(self):
        refined_area = shapely.box(-1.0, -1.0, 1.0, 1.0)
        correction = attrs.evolve(
            _correction(
                _fit(1, shapely.box(9.0, 9.0, 11.0, 11.0), Point2d(-0.10, 0.30))
            ),
            refinements=(
                PadRefinement(
                    _pad(refined_area),
                    _fit(-1, refined_area, Point2d(0.25, -0.15)),
                    Point2d(0.0, 0.0),
                ),
            ),
        )

        shift = _shift_at(correction, Point2d(10.0, 10.0))

        assert shift.x == pytest.approx(-0.10, abs=1e-12)
        assert shift.y == pytest.approx(0.30, abs=1e-12)

    def test_cannot_be_made_without_a_successful_region(self):
        with pytest.raises(ValueError, match="照合 3 領域"):
            BoardCorrection(fits=(), region_count=3)

    def test_summary_counts_regions_and_refined_pads(self):
        area = shapely.box(-1.0, -1.0, 1.0, 1.0)
        correction = BoardCorrection(
            fits=(_fit(0, area, Point2d(0.0, 0.0)),),
            region_count=2,
            refinements=(
                PadRefinement(
                    _pad(area, "R1"),
                    _fit(-1, area, Point2d(0.0, 0.0)),
                    Point2d(0.0, 0.0),
                ),
                PadRefinement(_pad(area, "R2"), None, None),
            ),
        )

        assert correction.summary() == "照合成功 1/2 領域 / pad中心照合 1/2 pads"


def _session(
    klipper: FakeKlipper,
    camera: Camera,
    *,
    board: Board,
    placement: Transform = Identity(),
    machine: MachineConfig | None = None,
    pixel_per_mm: float = PPM,
) -> RegionAlignmentSession:
    camera_model = make_camera_model(
        pixel_per_mm, (camera.resolution.width, camera.resolution.height), focus_z=12.0
    )
    return RegionAlignmentSession(
        BoardFrame(board=board, camera_model=camera_model, board_transform=placement),
        machine or load_machine(TEST_CONFIG_DIR / "machine.toml"),
        klipper=klipper,
        stage=XYZStage(klipper),
        camera=camera,
    )


class TestRegionAlignmentSessionKlipperRefusal:
    """Klipper が移動を断った KlipperError は、領域の失敗（None）にせず呼び出し側へ伝える."""

    @pytest.fixture
    def klipper(self) -> FakeKlipper:
        klipper = FakeKlipper()
        klipper.fail_on(lambda gcode: True)
        return klipper

    @pytest.fixture
    def session(self, klipper: FakeKlipper) -> RegionAlignmentSession:
        frame = Image(np.zeros((480, 640, 3), dtype=np.uint8))
        return _session(klipper, FakeCamera([frame]), board=load_board(LED_BLINKER_PCB))

    def test_align_raises_klipper_error(
        self, klipper: FakeKlipper, session: RegionAlignmentSession
    ):
        region = AlignmentRegion(
            index=0,
            board_center=Point2d(10.0, 10.0),
            anchor=Point2d(10.0, 10.0),
            roi=session.region_roi,
            board_area=shapely.box(9.0, 9.0, 11.0, 11.0),
        )

        with pytest.raises(KlipperError):
            session.align(region)
        # 最初の移動で断られ、撮像へ進んでいない
        assert len(klipper.sent) == 1

    def test_refine_raises_klipper_error(
        self, klipper: FakeKlipper, session: RegionAlignmentSession
    ):
        with pytest.raises(KlipperError):
            session.refine(
                Point2d(10.0, 10.0), Identity(), shapely.box(9.5, 9.5, 10.5, 10.5)
            )
        assert len(klipper.sent) == 1


# SceneCamera が描く基板の置き方（board → 機械座標）と、照合する 2 pad（上下に離れていて別の領域になる）
PLACEMENT = Shift(50.0, 40.0)
SCENE_PPM = 30.0
SCENE_SIZE = (640, 480)
PAD_REFS = ("R1.1", "R2.1")


def _align_on_scene(
    *,
    copper: Transform | None = PLACEMENT,
    fail_klipper: bool = False,
    max_passes: int | None = None,
) -> tuple[list[AlignEvent], BoardCorrection | ValueError | KlipperError]:
    """TOP 銅箔を copper で置いた led_blinker を SceneCamera で撮り、2 pad を位置合わせする.

    Args:
        copper: 銅箔の置き方（``None`` なら銅箔を描かない）
        fail_klipper: Klipper がすべての送信を Moonraker の 400 で断る
        max_passes: 1 領域の再計測の上限回数の上書き
    """
    board = load_board(LED_BLINKER_PCB)
    klipper = FakeKlipper()
    camera = SceneCamera(
        klipper,
        pixel_per_mm=SCENE_PPM,
        toolhead=Point2d(0.0, 0.0),
        rotations_per_ul=1.0,
        size=SCENE_SIZE,
        shapes=[
            transform_polygon(island.polygon, copper)
            for island in board.copper
            if copper is not None and island.layer == Layer.TOP
        ],
    )
    if fail_klipper:
        klipper.fail_on(lambda gcode: True)
    machine = load_machine(TEST_CONFIG_DIR / "machine.toml")
    if max_passes is not None:
        machine = attrs.evolve(
            machine, alignment=attrs.evolve(machine.alignment, max_passes=max_passes)
        )
    session = _session(
        klipper,
        camera,
        board=board,
        placement=PLACEMENT,
        machine=machine,
        pixel_per_mm=SCENE_PPM,
    )
    pads = [pad for pad in board.pads if pad.ref in PAD_REFS]
    events: list[AlignEvent] = []
    try:
        correction = align_board(
            session, pads, max_short_side_mm=0.6, on_event=events.append
        )
    except (ValueError, KlipperError) as exc:
        return events, exc
    return events, correction


class TestAlignBoard:
    """SceneCamera が描く led_blinker の銅箔で、領域照合と pad 中心の精密照合を通す."""

    def test_reports_each_step_before_and_after(self):
        """照合の前に件数を、1 件ごとに取りかかる前（進捗と中断の確認）と終えた後（照合ログ）を知らせる."""
        events, correction = _align_on_scene()

        assert isinstance(correction, BoardCorrection)
        regions = [event for event in events if isinstance(event, RegionMeasured)]
        assert [type(event) for event in events] == [
            RegionsPlanned,
            *(t for _ in regions for t in (AlignStep, RegionMeasured)),
            RefinementPlanned,
            *(t for _ in PAD_REFS for t in (AlignStep, PadMeasured)),
        ]
        steps = [event for event in events if isinstance(event, AlignStep)]
        assert [(step.stage, step.index) for step in steps] == [
            *(("銅箔照合", index) for index in range(len(regions))),
            ("pad照合", 0),
            ("pad照合", 1),
        ]
        assert [
            event.message
            for event in events
            if isinstance(event, RegionsPlanned | RefinementPlanned)
        ] == [
            f"照合対象の領域数: {len(regions)}",
            "pad中心照合対象: 2/2 pads (最大短辺 0.6 mm)",
        ]
        assert correction.summary() == (
            f"照合成功 {len(regions)}/{len(regions)} 領域 / pad中心照合 2/2 pads"
        )

    def test_refined_pads_use_their_own_displacement(self):
        """精密照合した pad は自身の変位で補正し、残差は領域だけの補正からの差になる.

        銅箔は board の置き方どおりに描くので、補正はほぼ 0 になる。
        """
        events, correction = _align_on_scene()

        assert isinstance(correction, BoardCorrection)
        regions_only = attrs.evolve(correction, refinements=())
        refinements = [
            event.refinement for event in events if isinstance(event, PadMeasured)
        ]
        assert len(refinements) == len(PAD_REFS)
        for refinement in refinements:
            center = refinement.pad.center
            assert refinement.result is not None and refinement.residual is not None
            displacement = refinement.result.displacement
            shift = _shift_at(correction, center)
            residual = displacement - _shift_at(regions_only, center)
            assert (shift.x, shift.y) == pytest.approx(
                (displacement.x, displacement.y), abs=1e-12
            )
            assert (refinement.residual.x, refinement.residual.y) == pytest.approx(
                (residual.x, residual.y), abs=1e-12
            )
            assert displacement.norm < 0.01

    @pytest.mark.parametrize(
        ("failure", "cause"),
        [
            ({"copper": None}, "領域 {index} の銅箔エッジ照合に失敗しました"),
            # 銅箔が 0.2 mm ずれていて、1 パスでは増分が収束判定の 0.03 mm に収まらない
            (
                {"copper": Shift(50.2, 40.0), "max_passes": 1},
                "領域 {index} は 1 パスで収束しませんでした",
            ),
        ],
        ids=["no_copper_edges", "not_converging"],
    )
    def test_each_failure_fails_the_region_and_moves_on(
        self, caplog: pytest.LogCaptureFixture, failure: dict, cause: str
    ):
        """銅箔エッジの検出の失敗と非収束は、どちらも領域の失敗として次の領域へ進む.

        どの領域も成功しないので、最後に補正が作れず ValueError で止まる（元の例外は伝わらない）。
        失敗の原因は警告ログに残る。
        """
        with caplog.at_level(logging.WARNING, logger="pcbasm.align.board"):
            events, result = _align_on_scene(**failure)

        assert isinstance(result, ValueError)
        regions = [event for event in events if isinstance(event, RegionMeasured)]
        assert len(regions) >= 2
        assert [event.region.index for event in regions] == list(range(len(regions)))
        assert all(event.fit is None for event in regions)
        assert "照合に失敗" in regions[0].message
        assert [record.getMessage() for record in caplog.records] == [
            f"領域 {index} の照合に失敗: " + cause.format(index=index)
            for index in range(len(regions))
        ]

    def test_moonraker_400_stops_at_the_first_region(
        self, caplog: pytest.LogCaptureFixture
    ):
        """Moonraker の 400 は領域の失敗にせず、残りの領域へ進まずに KlipperError で止まる."""
        with caplog.at_level(logging.WARNING, logger="pcbasm.align.board"):
            events, result = _align_on_scene(fail_klipper=True)

        assert isinstance(result, KlipperError)
        assert [type(event) for event in events] == [RegionsPlanned, AlignStep]
        assert caplog.records == []
