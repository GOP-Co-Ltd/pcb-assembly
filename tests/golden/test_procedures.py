"""手続きごとに、実機へ送る G-code の列を golden で固定する.

各テストは 1 つの手続きを FakeKlipper に対して実行し、送信の列を ``procedures/<名前>.gcode`` と比べる。
入力は ``machine.toml``（kurousagi 相当）、追跡している fixture 基板、固定の Board 変換と位置合わせ補正だけにする。
golden は現状の送信列そのものなので、手続きを意図して変えたときだけ ``PCBASM_UPDATE_GOLDEN=1`` で書き換え、差分を PR の主題にする。
"""

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import attrs
import numpy as np
import pytest
import shapely
from shapely import Polygon

from pcbasm import geometry, hal
from pcbasm.config import Machine
from pcbasm.gcode import GCode
from pcbasm.geometry import (
    Compose,
    HeightPlane,
    Identity,
    Point2d,
    Point3d,
    Rotation,
    Shift,
)
from pcbasm.hal import XYZStage
from pcbasm.hal.fake import (
    FAKE_PRINTER_CONFIG,
    FakeCamera,
    FakeKlipper,
)
from pcbasm.parking import park_or_present
from pcbasm.pasting.alignment import PasteCorrection
from pcbasm.pasting.applicator import build_applicator
from pcbasm.pasting.dataset.plan import (
    DEFAULT_PASTE_HEIGHT_MM,
    DotGridSpec,
    plan_dot_grid,
)
from pcbasm.pasting.fill_sequence import FillSequence, MicroLineSequence
from pcbasm.pasting.flowcalib.flow import RotationsPerUlRound
from pcbasm.pasting.flowcalib.lines import plan_rate_sweep, plan_speed_sweep
from pcbasm.pasting.flowcalib.params import CalibrationParams
from pcbasm.pasting.flowcalib.procedure import FlowCalibrationProcedure
from pcbasm.pasting.nozzle_clean import clean_nozzle, resolve_nozzle_clean
from pcbasm.pasting.params import DispenseSettings, PasteParams, PasteParamsPatch
from pcbasm.pasting.session import PasteSession
from pcbasm.pasting.settings import PasteSettingsModel
from pcbasm.pasting.toolhead_offset import (
    ToolheadOffsetProcedure,
    plan_toolhead_offset_points,
)
from pcbasm.pasting.workflow import PasteTargets, plan_paste_targets
from pcbasm.pcb import Copper, Layer, PadHierarchy, PcbFile
from pcbasm.pcb.generate import generate_rect_pcb, save_board
from pcbasm.posctrl import (
    AlignmentRegion,
    BoardAlignment,
    BoardCalibrationResult,
    EdgeMatch,
    RegionAlignment,
)
from pcbasm.vision import CalibrationResult, Image, Offset
from tests.support import polling
from tests.support.golden import assert_transcript
from tests.support.paths import TESTING_DATA_DIR
from web.api.jobs.catalog import JobCatalog, JobDefinition
from web.api.jobs.context import JobContext
from web.api.jobs.machine_commands import (
    create_command_klipper,
    handle_machine_command,
)
from web.api.jobs.manager import JobManager, JobStatus
from web.api.jobs.pasting.common import (
    LOADING_DEFAULT_AMOUNT,
    LOADING_DEFAULT_ROTATION_ACCEL,
    LOADING_DEFAULT_ROTATION_RATE,
    LOADING_DEFAULT_ROTATIONS,
)

GOLDEN_DIR = Path(__file__).parent
PROCEDURES = GOLDEN_DIR / "procedures"
MACHINE_TOML = GOLDEN_DIR / "machine.toml"

LED_BLINKER = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"
FILL_COVERAGE = TESTING_DATA_DIR / "fill_coverage" / "fill_coverage.kicad_pcb"

# 実機の Board 計測が返す形（回転 + 並進）。回転は 001 の校正値相当の小角
BOARD_TRANSFORM = Compose([Rotation(0.3), Shift(40.0, 60.0)])
FOCUS_Z = 12.5
# プローブが返す基板表面の Z
SURFACE_Z = -1.25


def _surface_z(x: float, y: float) -> float:
    return 0.2 + 0.004 * x - 0.003 * y + 0.0002 * x * x - 0.0001 * x * y


def _region(index: int, area: Polygon, displacement: Point2d) -> RegionAlignment:
    center = Point2d(area.centroid.x, area.centroid.y)
    return RegionAlignment(
        region=AlignmentRegion(
            index=index,
            board_center=center,
            anchor=center,
            roi=(0, 0, 100, 100),
            board_area=area,
        ),
        match=EdgeMatch(
            offset=Offset(px=Point2d(0.0, 0.0), pixel_per_mm=20.0),
            rms_distance_px=0.0,
        ),
        displacement=displacement,
        increment=Point2d(0.0, 0.0),
        passes=1,
    )


# led_blinker の左右で変位の違う 2 領域（U1 は境界をまたぐ）と、その塗る機械座標を囲む傾いた高さ面。
# TestFillPathFallback が使う fill_coverage の pad はどちらの範囲にも入らない。
# 位置合わせは最寄り領域の変位を使い、高さ面は外挿になる。
CORRECTION = PasteCorrection(
    alignment=BoardAlignment(
        results=(
            _region(0, shapely.box(0.0, 0.0, 10.0, 25.0), Point2d(0.03, -0.02)),
            _region(1, shapely.box(10.0, 0.0, 20.0, 25.0), Point2d(-0.01, 0.04)),
        )
    ),
    height_plane=HeightPlane(
        tuple(
            Point3d(x, y, _surface_z(x, y))
            for x, y in ((36, 80), (62, 80), (36, 112), (62, 112), (48, 92), (55, 101))
        )
    ),
)


@pytest.fixture
def machine() -> Machine:
    return Machine(MACHINE_TOML)


@pytest.fixture
def klipper() -> FakeKlipper:
    klipper = FakeKlipper()
    klipper.set_status("probe", "last_z_result", SURFACE_Z)
    return klipper


def _result(
    klipper: FakeKlipper, machine: Machine, pcb: Path
) -> BoardCalibrationResult:
    blank = Image(np.zeros((480, 640, 3), dtype=np.uint8))
    return BoardCalibrationResult(
        machine=machine,
        klipper=klipper,
        stage=XYZStage(klipper.readonly),
        camera=FakeCamera([blank]),
        calibration=CalibrationResult(
            pixel_per_mm=20.0,
            square_size_mm=1.0,
            mean_distance_px=20.0,
            std_distance_px=0.0,
            resolution=(640, 480),
            crop_size=(400, 400),
            calibrated_at=datetime(2026, 10, 23, tzinfo=UTC),
            z_position=FOCUS_Z,
        ),
        offset_transform=Identity(),
        board_transform=BOARD_TRANSFORM,
        pcb=PcbFile(pcb),
    )


def _session(klipper: FakeKlipper, machine: Machine, pcb: Path) -> PasteSession:
    return PasteSession.from_calibration(_result(klipper, machine, pcb))


def _targets(session: PasteSession) -> PasteTargets:
    """塗布ジョブと同じく、基板設定の無い基板の対象 pad・順路・初回パージを解決する."""
    dispenser = session.machine.paste_dispenser
    targets, error = plan_paste_targets(
        session.pcb,
        PadHierarchy.build(session.pcb.components, session.pcb.pads),
        PasteSettingsModel.from_config(dispenser),
        initial_purge_ul=dispenser.initial_purge_ul,
        flow_calibration=dispenser.flow_calibration,
    )
    assert targets is not None, error
    return targets


def _plate(tmp_path: Path, width: float, height: float) -> Path:
    """校正ジョブと同じく、外形だけの矩形銅板をその場で生成する."""
    path = tmp_path / "plate.kicad_pcb"
    save_board(generate_rect_pcb(width, height), path)
    return path


class TestApplicator:
    """塗布ジョブの pad ループ（リトラクト → 順路どおりに apply）を方式ごとに固定する."""

    @pytest.mark.parametrize("mode", ["dot", "line", "area", "micro_line", "auto"])
    def test_routed_pads(self, klipper: FakeKlipper, machine: Machine, mode: str):
        session = _session(klipper, machine, LED_BLINKER)
        targets = _targets(session)

        with session.make_applicator() as applicator:
            applicator.retract()
            for pad in targets.routed_pads:
                params = targets.params_for(pad) or applicator.default_params
                applicator.apply(
                    pad.polygon,
                    params=params.patched(
                        PasteParamsPatch.from_dict({"dispense_mode": mode})
                    ),
                    transform=session.pad_transform(pad, CORRECTION),
                    line_reference=session.component_positions.get(pad.designator),
                )

        assert_transcript(klipper.sent, PROCEDURES / f"applicator_{mode}.gcode")


class TestFillPathFallback:
    """指定した方式が成り立たない pad で、実際に使う方式の送信列を固定する."""

    @pytest.mark.parametrize(
        ("name", "designator", "values", "applied"),
        [
            ("area_to_line", "LINE_THIN", {"dispense_mode": "area"}, "line"),
            ("area_to_dot", "DOT_TINY", {"dispense_mode": "area"}, "dot"),
            ("line_to_dot", "DOT_TINY", {"dispense_mode": "line"}, "dot"),
            ("micro_line_to_dot", "DOT_TINY", {"dispense_mode": "micro_line"}, "dot"),
            (
                "negative_boundary_margin",
                "DOT_TINY",
                {"dispense_mode": "area", "boundary_margin": -0.1},
                "area",
            ),
            ("area_components", "SPLIT_DB", {"dispense_mode": "area"}, "area"),
        ],
    )
    def test_pad(
        self,
        klipper: FakeKlipper,
        machine: Machine,
        name: str,
        designator: str,
        values: dict[str, Any],
        applied: str,
    ):
        session = _session(klipper, machine, FILL_COVERAGE)
        (pad,) = (pad for pad in session.pcb.pads if pad.designator == designator)

        with session.make_applicator() as applicator:
            applicator.retract()
            result = applicator.apply(
                pad.polygon,
                params=applicator.default_params.patched(
                    PasteParamsPatch.from_dict(values)
                ),
                transform=session.pad_transform(pad, CORRECTION),
            )

        assert result.summary.applied_mode == applied
        assert_transcript(klipper.sent, PROCEDURES / f"fill_path_{name}.gcode")


class TestFillSequence:
    def test_variants(self, klipper: FakeKlipper, machine: Machine):
        """移動律速・吐出レート律速・点とプライム後の追加遅延・微小線を 1 本ずつ送る."""
        stage = XYZStage(klipper.readonly)
        dispenser = hal.PasteDispenser(
            klipper=klipper.readonly,
            rotations_per_ul=machine.paste_dispenser.rotations_per_ul,
        )
        settings = DispenseSettings.from_config(machine.paste_dispenser)
        line = geometry.Path([Point3d(50.0, 90.0, 0.3), Point3d(53.0, 90.5, 0.32)])
        point = geometry.Path([Point3d(51.0, 92.0, 0.3)])
        sequences = [
            FillSequence(
                path=line, total_amount_ul=0.05, settings=settings, rate_cap=math.inf
            ),
            FillSequence(path=line, total_amount_ul=0.5, settings=settings),
            FillSequence(
                path=point,
                total_amount_ul=0.02,
                settings=settings,
                prime_extra_delay=0.4,
            ),
            MicroLineSequence(path=line, total_amount_ul=0.01, settings=settings),
        ]

        for sequence in sequences:
            klipper.send_gcode(
                sequence.to_gcode(stage, dispenser) + GCode.wait_for_done()
            )

        assert_transcript(klipper.sent, PROCEDURES / "fill_sequence.gcode")


class TestInitialPurge:
    def test_retract_then_purge(self, klipper: FakeKlipper, machine: Machine):
        """塗布ジョブの先頭と同じく、リトラクトしてから面積最大の pad の中心へパージする."""
        session = _session(klipper, machine, LED_BLINKER)
        purge = _targets(session).initial_purge
        assert purge is not None

        with session.make_applicator() as applicator:
            applicator.retract()
            applicator.deposit_at(
                purge.point,
                amount_ul=purge.amount_ul,
                transform=session.point_transform(purge.point, CORRECTION),
            )

        assert_transcript(klipper.sent, PROCEDURES / "initial_purge.gcode")


class TestNozzleClean:
    def test_prime_clean_retract(self, klipper: FakeKlipper, machine: Machine):
        """パージが写らなかったときのクリーニング（prime → パージとこすり → retract）."""
        stage = XYZStage(klipper.readonly)
        clean = resolve_nozzle_clean(machine, stage)
        assert clean is not None

        with build_applicator(klipper, stage, machine.paste_dispenser) as applicator:
            applicator.prime()
            clean_nozzle(klipper, stage, applicator, clean)
            applicator.retract()

        assert_transcript(klipper.sent, PROCEDURES / "nozzle_clean.gcode")

    def test_out_of_reach_fails_before_moving(
        self, tmp_path: Path, klipper: FakeKlipper
    ):
        """塗布ジョブが位置合わせの直後に解決する段で、可動域外は何も送らずに失敗する."""
        path = tmp_path / "machine.toml"
        text = MACHINE_TOML.read_text(encoding="utf-8")
        path.write_text(text.replace("x = 120.0", "x = 299.0"), encoding="utf-8")

        with pytest.raises(ValueError, match="可動域外"):
            resolve_nozzle_clean(Machine(path), XYZStage(klipper.readonly))

        assert klipper.sent == ()


class TestParking:
    def test_nozzle_cap(self, klipper: FakeKlipper, machine: Machine):
        park_or_present(klipper, machine)

        assert_transcript(klipper.sent, PROCEDURES / "park_nozzle_cap.gcode")

    @pytest.mark.parametrize(
        ("name", "printer_config"),
        [
            (
                "present",
                {**FAKE_PRINTER_CONFIG, "gcode_macro PRESENT": {"gcode": "G1 Y300"}},
            ),
            ("relax", FAKE_PRINTER_CONFIG),
        ],
    )
    def test_without_nozzle_cap(
        self, tmp_path: Path, name: str, printer_config: dict[str, Any]
    ):
        """キャップ未記録なら PRESENT マクロ、無ければ M84 に退避する."""
        path = tmp_path / "machine.toml"
        path.write_text('machine_type = "paste"\n', encoding="utf-8")
        klipper = FakeKlipper(printer_config)

        park_or_present(klipper, Machine(path), warn=lambda message: None)

        assert_transcript(klipper.sent, PROCEDURES / f"park_{name}.gcode")


class TestLoading:
    def test_extrude_suck_and_rotate(self, klipper: FakeKlipper, machine: Machine):
        """ローディング画面の押出・吸引・回転（引き戻し付き）."""
        stage = XYZStage(klipper.readonly)

        with build_applicator(klipper, stage, machine.paste_dispenser) as applicator:
            applicator.load(LOADING_DEFAULT_AMOUNT)
            applicator.load(-LOADING_DEFAULT_AMOUNT)
            applicator.load_rotations(
                LOADING_DEFAULT_ROTATIONS,
                LOADING_DEFAULT_ROTATION_RATE,
                LOADING_DEFAULT_ROTATION_ACCEL,
                retract_rotations=1.0,
            )

        assert_transcript(klipper.sent, PROCEDURES / "loading.gcode")


class TestToolheadOffset:
    def test_probe_then_deposit(self, klipper: FakeKlipper, machine: Machine):
        """全点をプローブしてから、表面 Z + 塗布高さで全点に点塗布する."""
        result = _result(klipper, machine, LED_BLINKER)
        points, error = plan_toolhead_offset_points(
            result.pcb.outline.polygon,
            point_count=4,
            point_spacing=5.0,
            edge_margin=5.0,
            paste_diameter_max=2.0,
        )
        assert points is not None, error
        procedure = ToolheadOffsetProcedure(
            result,
            tolerance=0.1,
            lift_height=5.0,
            diameter_min=0.4,
            diameter_max=2.0,
            point_spacing=5.0,
        )

        probed = [procedure.probe(point) for point in points]
        with procedure.applicator() as applicator:
            applicator.retract()
            for point in probed:
                procedure.deposit(applicator, point, amount_ul=LOADING_DEFAULT_AMOUNT)

        assert_transcript(klipper.sent, PROCEDURES / "toolhead_offset.gcode")


class TestHeightPlane:
    def test_probes_top_copper(self, klipper: FakeKlipper, machine: Machine):
        _session(klipper, machine, LED_BLINKER).measure_height_plane()

        assert_transcript(klipper.sent, PROCEDURES / "height_plane.gcode")


class TestFlowCalibration:
    """吐出量キャリブレーション ①②③ の線引き（銅板の高さ計測から無効化まで）."""

    @pytest.fixture
    def params(self) -> CalibrationParams:
        return CalibrationParams()

    @pytest.fixture
    def result(
        self,
        tmp_path: Path,
        klipper: FakeKlipper,
        machine: Machine,
        params: CalibrationParams,
    ) -> BoardCalibrationResult:
        plate = _plate(tmp_path, params.board_width, params.board_height)
        return _result(klipper, machine, plate)

    def test_rotations_per_ul(
        self,
        klipper: FakeKlipper,
        machine: Machine,
        params: CalibrationParams,
        result: BoardCalibrationResult,
    ):
        """① 線を引いて計量し、算出値を採用して applicator を作り直す."""
        density = machine.paste_dispenser.density_mg_per_ul
        with FlowCalibrationProcedure.setup(result) as procedure:
            procedure.move_to_loading_z()
            procedure.draw_lines(
                params.line_layout().lines, amount_ul=params.line_amount_ul
            )
            procedure.move_to_removal_z(params.removal_z_offset)
            procedure.adopt(
                RotationsPerUlRound.evaluate(
                    mass_mg=0.9 * params.line_count * params.line_amount_ul * density,
                    line_count=params.line_count,
                    amount_ul=params.line_amount_ul,
                    previous_rotations_per_ul=procedure.rotations_per_ul,
                    density_mg_per_ul=density,
                )
            )

        assert_transcript(klipper.sent, PROCEDURES / "flowcalib_rotations_per_ul.gcode")

    def test_max_dispense_rate(
        self,
        klipper: FakeKlipper,
        machine: Machine,
        params: CalibrationParams,
        result: BoardCalibrationResult,
    ):
        """② レートごとに 1 本引いては退避する."""
        fill_speed = machine.paste_dispenser.max_fill_speed
        points, error = plan_rate_sweep(params, fill_speed=fill_speed)
        assert points is not None, error

        with FlowCalibrationProcedure.setup(result) as procedure:
            procedure.applicator.retract()
            for point in points:
                procedure.draw_lines(
                    [(point.start, point.end)],
                    amount_ul=point.amount_ul,
                    fill_speed=fill_speed,
                    rate_cap=point.rate,
                    retract=False,
                )
                procedure.move_to_removal_z(params.removal_z_offset)

        assert_transcript(
            klipper.sent, PROCEDURES / "flowcalib_max_dispense_rate.gcode"
        )

    def test_max_fill_speed(
        self,
        klipper: FakeKlipper,
        machine: Machine,
        params: CalibrationParams,
        result: BoardCalibrationResult,
    ):
        """③ 速度ごとに同じ量の線を、吐出レートの頭打ち無しで引く."""
        dispenser = machine.paste_dispenser
        sweep, error = plan_speed_sweep(
            params,
            ul_per_mm2=dispenser.ul_per_mm2,
            bead_width=dispenser.nozzle_diameter * dispenser.bead_width_factor,
        )
        assert sweep is not None, error

        with FlowCalibrationProcedure.setup(result) as procedure:
            procedure.draw_lines(
                [(point.start, point.end) for point in sweep.points],
                amount_ul=sweep.total_amount_ul,
                fill_speed=[point.fill_speed for point in sweep.points],
                rate_cap=math.inf,
            )

        assert_transcript(klipper.sent, PROCEDURES / "flowcalib_max_fill_speed.gcode")


class TestPasteDataset:
    def test_dot_grid(self, tmp_path: Path, klipper: FakeKlipper, machine: Machine):
        """塗布量データセットの収集で、銅板の高さを測ってからセルへ点塗布する."""
        spec = DotGridSpec()
        plan, error = plan_dot_grid(spec)
        assert plan is not None, error
        plate = _plate(tmp_path, spec.plate_width_mm, spec.plate_height_mm)
        session = _session(klipper, machine, plate)
        height_plane = session.measure_height_plane(
            [Copper(layer=Layer.TOP, polygon=session.pcb.outline.polygon)]
        )
        transform = session.plate_transform(height_plane=height_plane)
        params = attrs.evolve(
            PasteParams.from_config(machine.paste_dispenser),
            paste_height=DEFAULT_PASTE_HEIGHT_MM,
            prime_extra_delay=0.0,
        )

        with session.make_applicator() as applicator:
            for cell in plan.cells:
                applicator.deposit_at(
                    cell.center,
                    amount_ul=cell.commanded_volume_ul,
                    transform=transform,
                    params=params,
                )

        assert_transcript(klipper.sent, PROCEDURES / "paste_dataset.gcode")


class TestMachineCommands:
    """ジョブモードのマシン操作パネル（jog / home / move / relax / focus_z / move_to_cap）."""

    @pytest.fixture
    def catalog(self) -> JobCatalog:
        return JobCatalog()

    @pytest.mark.parametrize(
        ("name", "command"),
        [
            ("jog_x", {"type": "jog", "axis": "x", "dist": 1.5}),
            ("jog_y", {"type": "jog", "axis": "y", "dist": -0.1}),
            ("jog_z", {"type": "jog", "axis": "z", "dist": 10.0}),
            ("home_all", {"type": "home", "axes": []}),
            ("home_xy", {"type": "home", "axes": ["x", "y"]}),
            ("move", {"type": "move", "x": 100.0, "y": 50.0, "z": 5.0}),
            ("relax", {"type": "relax"}),
            ("focus_z", {"type": "focus_z"}),
            ("move_to_cap", {"type": "move_to_cap"}),
        ],
    )
    def test_command(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        machine_klipper: FakeKlipper,
        name: str,
        command: dict[str, Any],
    ):
        # jog は現在位置からの相対量なので、原点以外から始める
        machine_klipper.set_status(
            "gcode_move", "gcode_position", [100.0, 80.0, 5.0, 0.0]
        )

        def run(ctx: JobContext) -> None:
            klipper = create_command_klipper(ctx)
            stage = XYZStage(klipper.readonly)
            assert handle_machine_command(ctx, klipper, stage, command, focus_z=FOCUS_Z)

        catalog.register(
            JobDefinition(
                name="command",
                label="マシン操作",
                tab="dev",
                run=run,
                uses_machine=False,
            )
        )
        record = manager.start("command", {})
        polling.wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert_transcript(
            machine_klipper.sent, PROCEDURES / f"machine_command_{name}.gcode"
        )
