"""塗布の所要時間の予測（estimate_paste_tact / estimate_prelude）の仕様.

単純な pad で手計算と一致すること、速度・加速度・pad 数に対して単調であることを固定する。
前段は、手で数えられる基準点・パージを手計算と突き合わせ、点数や箇所数が実際の計画から決まることを確かめる。
"""

import math

import attrs
import pytest
from shapely import Polygon

from pcbasm.board import Board, Component, Copper, Layer, Outline, Pad
from pcbasm.config import MachineConfig, load_machine
from pcbasm.config.settle import SettleConfig
from pcbasm.config.solder import NozzleOffsetConfig, TactConfig
from pcbasm.geometry import Path, Point2d
from pcbasm.pasting.overrides import BoardSession
from pcbasm.pasting.params import PasteParams
from pcbasm.pasting.paste_volume.runtime_correction import FlowCalibrationPlan
from pcbasm.pasting.plan import PastePlan, plan_paste
from pcbasm.pasting.tact import (
    MotionLimits,
    PreludeEstimate,
    estimate_paste_tact,
    estimate_prelude,
)
from tests.support.camera_model import make_camera_model
from tests.support.paths import MINIMAL_MACHINE_TOML
from tests.support.solder import solder_config

_LIMITS = MotionLimits(velocity=50.0, accel=500.0, z_velocity=5.0, z_accel=10.0)
_NO_PRELUDE = PreludeEstimate(
    fixed_seconds=0.0,
    reference_seconds=0.0,
    probe_count=0,
    probe_seconds=0.0,
    align_count=0,
    align_seconds=0.0,
    purge_seconds=0.0,
)
# 点塗布。prime の加速は無視できる大きさにし、prime を retract_amount / rate にする
_MOTION = {
    "max_fill_speed": 1.0,
    "max_dispense_rate": 0.1,
    "dispense_accel": 1e9,
    "retract_amount": 0.1,
    "retract_rate": 0.1,
    "retract_accel_factor": 2.0,
    "lift_height": 1.0,
    "post_dispense_settle_seconds": 0.1,
}
_DOT = PasteParams(
    dispense_mode="dot",
    line_direction="unconstrained",
    paste_height=0.05,
    ul_per_mm2=0.1,
    prime_extra_delay=0.0,
    bead_width_factor=1.0,
    overlap=0.0,
    boundary_margin=0.0,
    micro_line_z_start=0.07,
    micro_line_z_end=0.07,
)
# 0.5 mm 角の点塗布 1 つ（上の設定での手計算）。塗布量は 0.025 uL
_DOT_UL = 0.025
_DESCEND = 2 * math.sqrt(1.0 / 10.0)  # Z 1 mm は 5^2/10 = 2.5 mm より短いので三角形
_PRIME = 0.1 / 0.1
_DISPENSE = 0.5 * 0.5 * 0.1 / 0.1
_RETRACT = 0.1 / 0.2 + 0.1 / 0.1  # 加速度 2.0 * 0.1^2 / 0.1 = 0.2、台形
_DOT_SECONDS = _DESCEND + _PRIME + _DISPENSE + 0.1 + max(_RETRACT, _DESCEND)

# 前段の設定。カメラは 30 fps（machine_minimal.toml）
_SETTLE = SettleConfig(move_sec=0.5, probe_sec=0.25)
_CAMERA = make_camera_model(20.0, (640, 480))


def _rect(x: float, y: float, width: float = 0.5, height: float = 0.5) -> Polygon:
    """中心 (x, y) の矩形（既定は 0.5 mm 角）."""
    w, h = width / 2.0, height / 2.0
    return Polygon([(x - w, y - h), (x + w, y - h), (x + w, y + h), (x - w, y + h)])


def _board(*pads: Polygon, copper: tuple[Polygon, ...] = ()) -> Board:
    """100 mm 角の基板に、``pads`` を 1 つずつ持つ部品を並べる."""
    names = [f"R{i}" for i in range(1, len(pads) + 1)]
    return Board(
        outline=Outline(_rect(50.0, 50.0, 100.0, 100.0)),
        components=tuple(
            Component(
                name,
                "x",
                "0402",
                Point2d(pad.centroid.x, pad.centroid.y),
                0.0,
                Layer.TOP,
            )
            for name, pad in zip(names, pads, strict=True)
        ),
        pads=tuple(
            Pad(name, "1", "", Layer.TOP, pad)
            for name, pad in zip(names, pads, strict=True)
        ),
        copper=tuple(Copper(Layer.TOP, polygon) for polygon in copper),
    )


def _plan_of(
    board: Board, params: PasteParams = _DOT, initial_purge_ul: float = 0.0
) -> PastePlan:
    config = solder_config(
        defaults=params, initial_purge_ul=initial_purge_ul, **_MOTION
    )
    return plan_paste(BoardSession.initial("t.kicad_pcb", board, config))


def _plan(*pads: Polygon, params: PasteParams = _DOT) -> PastePlan:
    """``pads`` を 1 つずつ持つ部品を並べた基板の塗布計画."""
    return _plan_of(_board(*pads), params)


def _machine(
    plan: PastePlan, *, refine_max_short_side: float = 0.0, **tact: float
) -> MachineConfig:
    """machine_minimal.toml に、塗布計画の [solder] と前段の設定を入れる."""
    machine = load_machine(MINIMAL_MACHINE_TOML)
    return attrs.evolve(
        machine,
        settle=_SETTLE,
        alignment=attrs.evolve(
            machine.alignment, refine_max_short_side=refine_max_short_side
        ),
        solder=attrs.evolve(plan.config, tact=TactConfig(**tact)),
    )


def _estimate(plan: PastePlan, board: Board, machine: MachineConfig) -> PreludeEstimate:
    """前段を予測する（予測できない基板はテストの誤りとして落とす）."""
    prelude, error = estimate_prelude(plan, board, machine, _LIMITS, _CAMERA)
    assert prelude is not None, error
    return prelude


def _move(distance: float, velocity: float, accel: float) -> float:
    """止まった状態から止まるまでの台形（短ければ三角形）速度プロファイルの時間."""
    if distance <= velocity**2 / accel:
        return 2.0 * math.sqrt(distance / accel)
    return velocity / accel + distance / velocity


class TestEstimatePasteTact:
    def test_one_dot_is_descend_prime_dispense_settle_and_the_longer_of_retract_and_lift(
        self,
    ):
        estimate = estimate_paste_tact(_plan(_rect(10.0, 10.0)), _NO_PRELUDE, _LIMITS)

        assert estimate.pad_count == 1
        assert estimate.paste_seconds == pytest.approx(_DOT_SECONDS)

    def test_travel_between_pads_uses_the_xy_limits(self):
        estimate = estimate_paste_tact(
            _plan(_rect(10.0, 10.0), _rect(20.0, 10.0)), _NO_PRELUDE, _LIMITS
        )

        # XY 10 mm は 50^2/500 = 5 mm より長いので台形: 50/500 + 10/50
        travel = 50.0 / 500.0 + 10.0 / 50.0
        assert estimate.pad_count == 2
        assert estimate.paste_seconds == pytest.approx(2 * _DOT_SECONDS + travel)

    def test_total_adds_the_prelude(self):
        prelude = attrs.evolve(_NO_PRELUDE, fixed_seconds=100.0, probe_seconds=20.0)

        estimate = estimate_paste_tact(_plan(_rect(10.0, 10.0)), prelude, _LIMITS)

        assert estimate.prelude == prelude
        assert estimate.total_seconds == pytest.approx(120.0 + _DOT_SECONDS)

    def test_no_pads_is_only_the_prelude(self):
        prelude = attrs.evolve(_NO_PRELUDE, fixed_seconds=60.0)

        estimate = estimate_paste_tact(_plan(), prelude, _LIMITS)

        assert estimate.pad_count == 0
        assert estimate.total_seconds == 60.0

    def test_micro_line_pushes_above_then_scrubs(self):
        """上空で押し出し → 静定待ち → 下降 → こすりつけ → 静定待ち → 上昇 → retract."""
        params = attrs.evolve(_DOT, dispense_mode="micro_line")
        plan = _plan(_rect(10.0, 10.0, 0.3, 1.5), params=params)
        (pad,) = plan.routed_pads
        fill = plan.fill_plan(pad)
        assert fill.dispense_mode == "micro_line"
        (line,) = fill.paths
        length = Path(line).length()

        estimate = estimate_paste_tact(plan, _NO_PRELUDE, _LIMITS)

        # 押し出す量は retract 分と塗布量（面積 0.45 x 0.1）。加速は無視できるので量 / レート。
        # こすりつけは 1^2/500 mm より長いので台形
        push = (0.1 + 0.3 * 1.5 * 0.1) / 0.1
        scrub = 1.0 / 500.0 + length / 1.0
        assert estimate.paste_seconds == pytest.approx(
            push + 0.1 + _DESCEND + scrub + 0.1 + _DESCEND + _RETRACT
        )


class TestMonotonic:
    """上限が低いほど長く、pad が多いほど長い."""

    _PADS = (_rect(10.0, 10.0), _rect(30.0, 10.0), _rect(30.0, 40.0))

    @pytest.mark.parametrize("field", ["velocity", "accel", "z_velocity", "z_accel"])
    def test_lower_limit_is_longer(self, field: str):
        plan = _plan(*self._PADS)
        slower = attrs.evolve(_LIMITS, **{field: getattr(_LIMITS, field) / 2})

        assert (
            estimate_paste_tact(plan, _NO_PRELUDE, slower).paste_seconds
            > estimate_paste_tact(plan, _NO_PRELUDE, _LIMITS).paste_seconds
        )

    def test_more_pads_is_longer(self):
        seconds = [
            estimate_paste_tact(
                _plan(*self._PADS[:count]), _NO_PRELUDE, _LIMITS
            ).paste_seconds
            for count in range(len(self._PADS) + 1)
        ]

        assert seconds == sorted(seconds)
        assert len(set(seconds)) == len(seconds)


class TestEstimatePrelude:
    """前段（ジョブの開始から pad を塗り始めるまで）の予測."""

    _COPPER = (_rect(50.0, 50.0, 90.0, 90.0),)
    _PADS = (_rect(20.0, 20.0), _rect(80.0, 20.0), _rect(50.0, 80.0))
    _BOARD = _board(*_PADS, copper=_COPPER)

    def _prelude(self, plan: PastePlan, **machine: float) -> PreludeEstimate:
        return _estimate(plan, self._BOARD, _machine(plan, **machine))

    @staticmethod
    def _capture(machine: MachineConfig, frames: int) -> float:
        """撮影 1 回: 移動後の静定待ち + フレームを撮る時間 + 撮影の処理."""
        tact = machine.require_solder().tact
        return _SETTLE.move_sec + frames / machine.camera.fps + tact.capture_sec

    def test_reference_visits_the_four_markers(self):
        """左上 → 右上 → 右下 → 左下の基準点を 0.9 x max_velocity で回り、各所で撮影を繰り返す."""
        plan = _plan_of(self._BOARD)
        machine = _machine(plan, align_passes=2.0, capture_sec=0.3)

        prelude = _estimate(plan, self._BOARD, machine)

        # machine_minimal.toml の基準点: 左上 (0, -5)、右上 (100, -5)、右下 (105, 105)、左下 (5, 105)
        travel = sum(
            _move(distance, 0.9 * 50.0, 500.0)
            for distance in (100.0, math.hypot(5.0, 110.0), 100.0)
        )
        frames = machine.detection.sample_count
        assert prelude.reference_seconds == pytest.approx(
            travel + 4 * 2.0 * self._capture(machine, frames)
        )

    def test_probe_points_come_from_the_probe_plan(self):
        """点数は銅箔の上に計画した点で決まり、1 点ごとに静定待ちと PROBE の時間がかかる."""
        plan = _plan_of(self._BOARD)
        probe = load_machine(MINIMAL_MACHINE_TOML).probe

        quick = self._prelude(plan, probe_point_sec=0.0)
        slow = self._prelude(plan, probe_point_sec=2.0)

        assert probe.min_samples <= quick.probe_count <= probe.max_samples
        assert slow.probe_count == quick.probe_count
        assert slow.probe_seconds - quick.probe_seconds == pytest.approx(
            quick.probe_count * 2.0
        )
        assert quick.probe_seconds > quick.probe_count * (
            _SETTLE.move_sec + _SETTLE.probe_sec
        )

    def test_alignment_adds_the_pads_to_refine(self):
        """短辺が上限以下の pad は、領域の照合に加えて pad 中心でも照合する."""
        plan = _plan_of(self._BOARD)

        regions_only = self._prelude(plan, refine_max_short_side=0.0)
        refined = self._prelude(plan, refine_max_short_side=1.0)

        assert regions_only.align_count > 0
        assert refined.align_count == regions_only.align_count + len(self._PADS)
        assert refined.align_seconds > regions_only.align_seconds

    def test_alignment_repeats_the_capture_per_pass(self):
        plan = _plan_of(self._BOARD)
        machine = _machine(plan, capture_sec=0.3)

        once = self._prelude(plan, align_passes=1.0, capture_sec=0.3)
        three_times = self._prelude(plan, align_passes=3.0, capture_sec=0.3)

        assert three_times.align_seconds - once.align_seconds == pytest.approx(
            2.0 * once.align_count * self._capture(machine, 1)
        )

    def test_no_pads_needs_no_alignment(self):
        board = _board(copper=self._COPPER)
        plan = _plan_of(board)

        prelude = _estimate(plan, board, _machine(plan))

        assert (prelude.align_count, prelude.align_seconds) == (0, 0.0)
        assert prelude.probe_count > 0

    def test_initial_purge_captures_before_and_after_the_dot(self):
        """ノズルがカメラ中心にあるので移動は無く、撮影 2 回と点塗布 1 回になる."""
        plan = _plan_of(self._BOARD, initial_purge_ul=_DOT_UL)
        assert plan.initial_purge.resolved is not None
        machine = _machine(plan)

        prelude = _estimate(plan, self._BOARD, machine)

        assert prelude.purge_seconds == pytest.approx(
            2 * self._capture(machine, 1) + _DOT_SECONDS
        )

    def test_initial_purge_moves_the_nozzle_over_the_captured_point(self):
        """塗る位置はカメラで撮る位置からツールヘッドオフセットだけずれるので、撮影と塗布のあいだを往復する."""
        plan = _plan_of(self._BOARD, initial_purge_ul=_DOT_UL)
        solder = attrs.evolve(
            plan.config,
            nozzle=attrs.evolve(
                plan.config.nozzle, offset=NozzleOffsetConfig(x=3.0, y=-4.0)
            ),
        )
        plan = attrs.evolve(plan, config=solder)
        machine = _machine(plan)

        prelude = _estimate(plan, self._BOARD, machine)

        assert prelude.purge_seconds == pytest.approx(
            2 * self._capture(machine, 1) + _DOT_SECONDS + 2 * _move(5.0, 50.0, 500.0)
        )

    def test_board_without_copper_to_probe_returns_the_reason(self):
        """高さ計測の点を計画できない基板は、塗布ジョブも高さ計測で止まるので予測しない."""
        board = _board(*self._PADS)
        plan = _plan_of(board)

        prelude, error = estimate_prelude(plan, board, _machine(plan), _LIMITS, _CAMERA)

        assert prelude is None
        assert error is not None and error.startswith("高さ計測の点を計画できません")

    def test_flow_calibration_captures_all_points_then_dots_then_waits(self):
        """塗布前の撮影 → 点塗布 → 静定待ち → 塗布後の撮影を、測定点を並びの順に回って行う."""
        flow = FlowCalibrationPlan(
            points=(Point2d(10.0, 10.0), Point2d(20.0, 10.0)),
            amount_ul=_DOT_UL,
            crop_size_mm=2.0,
            calibration_file="c.json",
            settle_seconds=3.0,
            max_correction_scale=2.0,
        )
        plan = attrs.evolve(_plan_of(self._BOARD), flow_calibration=flow)
        machine = _machine(plan)

        prelude = _estimate(plan, self._BOARD, machine)

        # 撮影 2 点 → 塗布 2 点 → 撮影 2 点で、10 mm の移動が 5 回
        assert prelude.purge_seconds == pytest.approx(
            4 * self._capture(machine, 1)
            + 2 * _DOT_SECONDS
            + 3.0
            + 5 * _move(10.0, 50.0, 500.0)
        )

    def test_total_is_the_sum_of_the_parts(self):
        plan = _plan_of(self._BOARD, initial_purge_ul=_DOT_UL)

        prelude = self._prelude(plan, fixed_sec=10.0)

        assert prelude.total_seconds == pytest.approx(
            10.0
            + prelude.reference_seconds
            + prelude.probe_seconds
            + prelude.align_seconds
            + prelude.purge_seconds
        )


class TestMotionLimits:
    def test_reads_the_printer_section(self):
        limits = MotionLimits.from_printer_config(
            {
                "printer": {
                    "max_velocity": "50",
                    "max_accel": "500",
                    "max_z_velocity": "3.0",
                    "max_z_accel": "15",
                }
            }
        )

        assert limits == MotionLimits(
            velocity=50.0, accel=500.0, z_velocity=3.0, z_accel=15.0
        )

    def test_z_falls_back_to_the_xy_limits(self):
        limits = MotionLimits.from_printer_config(
            {"printer": {"max_velocity": "50", "max_accel": "500"}}
        )

        assert (limits.z_velocity, limits.z_accel) == (50.0, 500.0)
