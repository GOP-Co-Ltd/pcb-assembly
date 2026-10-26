"""手続きごとに、実機へ送る G-code の列を golden で固定する.

各テストは 1 つの手続きを FakeKlipper に対して実行し、送信の列を ``procedures/<名前>.gcode`` と比べる。
入力は ``machine.toml``（kurousagi 相当）、追跡している fixture 基板、固定の Board 変換と位置合わせ補正だけにする。
golden は現状の送信列そのものなので、手続きを意図して変えたときだけ ``PCBASM_UPDATE_GOLDEN=1`` で書き換え、差分を PR の主題にする。
"""

import math
from pathlib import Path
from typing import Any

import attrs
import numpy as np
import pytest
import shapely
from shapely import Polygon

from pcbasm import geometry, hal
from pcbasm.align import (
    AlignmentRegion,
    BoardCorrection,
    BoardFrame,
    EdgeMatch,
    RegionFit,
)
from pcbasm.config import MachineConfig, load_machine
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
    FAKE_MACHINE_PRINTER_CONFIG,
    FAKE_PRINTER_CONFIG,
    FakeCamera,
    FakeKlipper,
)
from pcbasm.kicad import load_board
from pcbasm.machine.park import PRESENT_TIMEOUT, park
from pcbasm.pasting.applicator import build_applicator
from pcbasm.pasting.dispense_calibration.lines import plan_rate_sweep, plan_speed_sweep
from pcbasm.pasting.dispense_calibration.mass import RotationsPerUlRound
from pcbasm.pasting.dispense_calibration.params import CalibrationParams
from pcbasm.pasting.dispense_calibration.procedure import FlowCalibrationProcedure
from pcbasm.pasting.dot_capture import PRIME_EXTRA_DELAY
from pcbasm.pasting.fill_sequence import FillSequence, MicroLineSequence
from pcbasm.pasting.hierarchy import PadHierarchy
from pcbasm.pasting.nozzle_clean import clean_nozzle, resolve_nozzle_clean
from pcbasm.pasting.overrides import BoardSession, PasteSettingsModel
from pcbasm.pasting.params import PasteParamsPatch
from pcbasm.pasting.paste_volume.dataset.plan import (
    DEFAULT_PASTE_HEIGHT_MM,
    DotGridSpec,
    plan_dot_grid,
)
from pcbasm.pasting.plan import PastePlan, plan_paste
from pcbasm.pasting.plate import CopperPlate, write_rect_board
from pcbasm.pasting.prepare import PasteCorrection
from pcbasm.pasting.session import PasteSession
from pcbasm.pasting.toolhead_offset import (
    ToolheadOffsetProcedure,
    plan_toolhead_offset_points,
)
from pcbasm.vision import Image, Offset
from tests.support.camera_model import make_camera_model
from tests.support.golden import assert_transcript
from tests.support.paths import FILL_COVERAGE_PCB, LED_BLINKER_PCB
from web.api.jobs.steps import (
    LOADING_DEFAULT_AMOUNT,
    LOADING_DEFAULT_ROTATION_ACCEL,
    LOADING_DEFAULT_ROTATION_RATE,
    LOADING_DEFAULT_ROTATIONS,
)

GOLDEN_DIR = Path(__file__).parent
PROCEDURES = GOLDEN_DIR / "procedures"
MACHINE_TOML = GOLDEN_DIR / "machine.toml"


# 実機の Board 計測が返す形（回転 + 並進）。回転は 001 の校正値相当の小角
BOARD_TRANSFORM = Compose([Rotation(0.3), Shift(40.0, 60.0)])
FOCUS_Z = 12.5
# プローブが返す基板表面の Z
SURFACE_Z = -1.25


def _surface_z(x: float, y: float) -> float:
    return 0.2 + 0.004 * x - 0.003 * y + 0.0002 * x * x - 0.0001 * x * y


def _region(index: int, area: Polygon, displacement: Point2d) -> RegionFit:
    center = Point2d(area.centroid.x, area.centroid.y)
    return RegionFit(
        region=AlignmentRegion(
            index=index,
            board_center=center,
            anchor=center,
            roi=(0, 0, 100, 100),
            board_area=area,
        ),
        match=EdgeMatch(
            offset=Offset(px=Point2d(0.0, 0.0)),
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
    alignment=BoardCorrection(
        fits=(
            _region(0, shapely.box(0.0, 0.0, 10.0, 25.0), Point2d(0.03, -0.02)),
            _region(1, shapely.box(10.0, 0.0, 20.0, 25.0), Point2d(-0.01, 0.04)),
        ),
        region_count=2,
    ),
    height_plane=HeightPlane(
        tuple(
            Point3d(x, y, _surface_z(x, y))
            for x, y in ((36, 80), (62, 80), (36, 112), (62, 112), (48, 92), (55, 101))
        )
    ),
)


@pytest.fixture
def machine() -> MachineConfig:
    return load_machine(MACHINE_TOML)


@pytest.fixture
def klipper() -> FakeKlipper:
    klipper = FakeKlipper()
    klipper.set_status("probe", "last_z_result", SURFACE_Z)
    return klipper


def _blank_camera() -> FakeCamera:
    return FakeCamera([Image(np.zeros((480, 640, 3), dtype=np.uint8))])


def _board_frame(pcb: Path) -> BoardFrame:
    return BoardFrame(
        board=load_board(pcb),
        camera_model=make_camera_model(20.0, (640, 480), focus_z=FOCUS_Z),
        board_transform=BOARD_TRANSFORM,
    )


def _session(klipper: FakeKlipper, machine: MachineConfig, pcb: Path) -> PasteSession:
    return PasteSession.from_board_frame(
        _board_frame(pcb),
        machine,
        klipper=klipper,
        stage=XYZStage(klipper),
        camera=_blank_camera(),
    )


def _plan(session: PasteSession) -> PastePlan:
    """塗布ジョブと同じく、基板設定の無い基板の対象 pad・順路・初回パージを解決する."""
    dispenser = session.machine.require_solder()
    board = session.board_frame.board
    hierarchy = PadHierarchy.build(board.components, board.pads)
    plan = plan_paste(
        BoardSession(
            source_pcb="",
            base_config=dispenser,
            board=board,
            hierarchy=hierarchy,
            board_signature=hierarchy.signature(),
            model=PasteSettingsModel.from_config(dispenser),
        )
    )
    assert plan.error is None, plan.error
    return plan


def _plate(tmp_path: Path, width: float, height: float) -> Path:
    """校正ジョブと同じく、外形だけの矩形銅板をその場で生成する."""
    path = tmp_path / "plate.kicad_pcb"
    write_rect_board(path, width, height)
    return path


class TestApplicator:
    """塗布ジョブの pad ループ（リトラクト → 順路どおりに apply）を方式ごとに固定する."""

    @pytest.mark.parametrize("mode", ["dot", "line", "area", "micro_line", "auto"])
    def test_routed_pads(self, klipper: FakeKlipper, machine: MachineConfig, mode: str):
        session = _session(klipper, machine, LED_BLINKER_PCB)
        plan = _plan(session)

        with session.make_applicator() as applicator:
            applicator.retract()
            for pad in plan.routed_pads:
                params = plan.params_for(pad)
                applicator.apply(
                    pad.polygon,
                    params=params.patched(PasteParamsPatch(dispense_mode=mode)),
                    transform=session.pad_transform(pad, CORRECTION),
                    line_reference=plan.line_reference(pad),
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
        machine: MachineConfig,
        name: str,
        designator: str,
        values: dict[str, Any],
        applied: str,
    ):
        session = _session(klipper, machine, FILL_COVERAGE_PCB)
        (pad,) = (
            pad
            for pad in session.board_frame.board.pads
            if pad.designator == designator
        )

        with session.make_applicator() as applicator:
            applicator.retract()
            result = applicator.apply(
                pad.polygon,
                params=applicator.default_params.patched(PasteParamsPatch(**values)),
                transform=session.pad_transform(pad, CORRECTION),
            )

        assert result.summary.applied_mode == applied
        assert_transcript(klipper.sent, PROCEDURES / f"fill_path_{name}.gcode")


class TestFillSequence:
    def test_variants(self, klipper: FakeKlipper, machine: MachineConfig):
        """移動律速・吐出レート律速・点とプライム後の追加遅延・微小線を 1 本ずつ送る."""
        stage = XYZStage(klipper)
        dispenser = hal.PasteDispenser(
            klipper=klipper,
            rotations_per_ul=machine.require_solder().dispenser.rotations_per_ul,
        )
        motion = machine.require_solder().dispense_motion
        line = geometry.Path([Point3d(50.0, 90.0, 0.3), Point3d(53.0, 90.5, 0.32)])
        point = geometry.Path([Point3d(51.0, 92.0, 0.3)])
        sequences = [
            FillSequence(
                path=line, total_amount_ul=0.05, motion=motion, rate_cap=math.inf
            ),
            FillSequence(path=line, total_amount_ul=0.5, motion=motion),
            FillSequence(
                path=point,
                total_amount_ul=0.02,
                motion=motion,
                prime_extra_delay=0.4,
            ),
            MicroLineSequence(path=line, total_amount_ul=0.01, motion=motion),
        ]

        for sequence in sequences:
            klipper.send_gcode(
                sequence.to_gcode(stage, dispenser) + GCode.wait_for_done()
            )

        assert_transcript(klipper.sent, PROCEDURES / "fill_sequence.gcode")


class TestInitialPurge:
    def test_retract_then_purge(self, klipper: FakeKlipper, machine: MachineConfig):
        """塗布ジョブの先頭と同じく、リトラクトしてから面積最大の pad の中心へパージする."""
        session = _session(klipper, machine, LED_BLINKER_PCB)
        purge = _plan(session).initial_purge.resolved
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
    def test_prime_clean_retract(self, klipper: FakeKlipper, machine: MachineConfig):
        """パージが写らなかったときのクリーニング（prime → パージとこすり → retract）."""
        stage = XYZStage(klipper)
        plan = resolve_nozzle_clean(machine, stage)
        assert plan is not None

        with build_applicator(klipper, stage, machine.require_solder()) as applicator:
            applicator.prime()
            clean_nozzle(klipper, applicator, plan)
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
            resolve_nozzle_clean(load_machine(path), XYZStage(klipper))

        assert klipper.sent == ()


def _park(klipper: FakeKlipper, machine: MachineConfig) -> None:
    """ジョブの終了時と同じく、駐機の timeout の接続で脱力まで駐機する."""
    connection = klipper.connection(timeout=PRESENT_TIMEOUT)
    cap = machine.require_solder().nozzle.cap.position
    park(connection, XYZStage(connection), cap, warn=lambda message: None)


class TestParking:
    def test_nozzle_cap(self, klipper: FakeKlipper, machine: MachineConfig):
        _park(klipper, machine)

        assert_transcript(klipper.sent, PROCEDURES / "park_nozzle_cap.gcode")

    @pytest.mark.parametrize(
        ("name", "printer_config"),
        [
            ("present", FAKE_MACHINE_PRINTER_CONFIG),
            ("relax", FAKE_PRINTER_CONFIG),
        ],
    )
    def test_without_nozzle_cap(
        self, tmp_path: Path, name: str, printer_config: dict[str, Any]
    ):
        """キャップ未記録なら PRESENT マクロ、無ければ M84 に退避する."""
        path = tmp_path / "machine.toml"
        text = MACHINE_TOML.read_text(encoding="utf-8")
        cap = "[solder.nozzle.cap]\nx = 150.0\ny = 10.0\nz = -2.0\n"
        path.write_text(text.replace(cap, ""), encoding="utf-8")
        klipper = FakeKlipper(printer_config)

        _park(klipper, load_machine(path))

        assert_transcript(klipper.sent, PROCEDURES / f"park_{name}.gcode")


class TestLoading:
    def test_extrude_suck_and_rotate(
        self, klipper: FakeKlipper, machine: MachineConfig
    ):
        """ローディング画面の押出・吸引・回転（引き戻し付き）."""
        stage = XYZStage(klipper)

        with build_applicator(klipper, stage, machine.require_solder()) as applicator:
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
    def test_probe_then_deposit(self, klipper: FakeKlipper, machine: MachineConfig):
        """全点をプローブしてから、表面 Z + 塗布高さで全点に点塗布する."""
        points, error = plan_toolhead_offset_points(
            load_board(LED_BLINKER_PCB).outline.polygon,
            point_count=4,
            point_spacing=5.0,
            edge_margin=5.0,
            paste_diameter_max=2.0,
        )
        assert points is not None, error
        procedure = ToolheadOffsetProcedure(
            _session(klipper, machine, LED_BLINKER_PCB),
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
    def test_probes_top_copper(self, klipper: FakeKlipper, machine: MachineConfig):
        _session(klipper, machine, LED_BLINKER_PCB).measure_height_plane()

        assert_transcript(klipper.sent, PROCEDURES / "height_plane.gcode")


class TestFlowCalibration:
    """吐出量キャリブレーション ①②③ の線引き（銅板の高さ計測から無効化まで）."""

    @pytest.fixture
    def params(self) -> CalibrationParams:
        return CalibrationParams()

    @pytest.fixture
    def session(
        self,
        tmp_path: Path,
        klipper: FakeKlipper,
        machine: MachineConfig,
        params: CalibrationParams,
    ) -> PasteSession:
        plate = _plate(tmp_path, params.board_width, params.board_height)
        return _session(klipper, machine, plate)

    def test_rotations_per_ul(
        self,
        klipper: FakeKlipper,
        machine: MachineConfig,
        params: CalibrationParams,
        session: PasteSession,
    ):
        """① 線を引いて計量し、算出値を採用する（ディスペンサーは有効のまま）."""
        density = machine.require_solder().paste_density
        with FlowCalibrationProcedure(CopperPlate.measure(session)) as procedure:
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
        machine: MachineConfig,
        params: CalibrationParams,
        session: PasteSession,
    ):
        """② レートごとに 1 本引いては退避する."""
        fill_speed = machine.require_solder().motion.max_fill_speed
        points, error = plan_rate_sweep(params, fill_speed=fill_speed)
        assert points is not None, error

        with FlowCalibrationProcedure(CopperPlate.measure(session)) as procedure:
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
        machine: MachineConfig,
        params: CalibrationParams,
        session: PasteSession,
    ):
        """③ 速度ごとに同じ量の線を、吐出レートの頭打ち無しで引く."""
        solder = machine.require_solder()
        sweep, error = plan_speed_sweep(
            params,
            ul_per_mm2=solder.defaults.ul_per_mm2,
            bead_width=solder.nozzle.diameter * solder.defaults.bead_width_factor,
        )
        assert sweep is not None, error

        with FlowCalibrationProcedure(CopperPlate.measure(session)) as procedure:
            procedure.draw_lines(
                [(point.start, point.end) for point in sweep.points],
                amount_ul=sweep.total_amount_ul,
                fill_speed=[point.fill_speed for point in sweep.points],
                rate_cap=math.inf,
            )

        assert_transcript(klipper.sent, PROCEDURES / "flowcalib_max_fill_speed.gcode")


class TestPasteDataset:
    def test_dot_grid(
        self, tmp_path: Path, klipper: FakeKlipper, machine: MachineConfig
    ):
        """塗布量データセットの収集で、銅板の高さを測ってからセルへ点塗布する."""
        spec = DotGridSpec()
        plan, error = plan_dot_grid(spec)
        assert plan is not None, error
        plate = _plate(tmp_path, spec.plate_width_mm, spec.plate_height_mm)
        session = _session(klipper, machine, plate)
        transform = CopperPlate.measure(session).transform
        params = attrs.evolve(
            machine.require_solder().defaults,
            paste_height=DEFAULT_PASTE_HEIGHT_MM,
            prime_extra_delay=PRIME_EXTRA_DELAY,
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
