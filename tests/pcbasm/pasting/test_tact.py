"""塗布の所要時間の予測（estimate_paste_tact）の仕様.

単純な pad で手計算と一致すること、速度・加速度・pad 数に対して単調であることを固定する。
"""

import math

import attrs
import pytest
from shapely import Polygon

from pcbasm.board import Board, Component, Layer, Outline, Pad
from pcbasm.config import load_machine
from pcbasm.config.dispenser import PasteDispenserConfig
from pcbasm.config.tact import TactConfig
from pcbasm.geometry import Path, Point2d
from pcbasm.pasting.overrides import BoardSession
from pcbasm.pasting.params import DispenseMotion, PasteParams
from pcbasm.pasting.plan import PastePlan, plan_paste
from pcbasm.pasting.tact import MotionLimits, estimate_paste_tact
from tests.support.paths import TEST_CONFIG_DIR

_LIMITS = MotionLimits(velocity=50.0, accel=500.0, z_velocity=5.0, z_accel=10.0)
_NO_SETUP = TactConfig(setup_sec=0.0)
# 点塗布。prime の加速は無視できる大きさにし、prime を retract_amount / rate にする
_MOTION = DispenseMotion(
    max_fill_speed=1.0,
    max_dispense_rate=0.1,
    dispense_accel=1e9,
    retract_amount=0.1,
    retract_rate=0.1,
    retract_accel_factor=2.0,
    lift_height=1.0,
    post_dispense_settle_seconds=0.1,
)
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
# 0.5 mm 角の点塗布 1 つ（上の設定での手計算）
_DESCEND = 2 * math.sqrt(1.0 / 10.0)  # Z 1 mm は 5^2/10 = 2.5 mm より短いので三角形
_PRIME = 0.1 / 0.1
_DISPENSE = 0.5 * 0.5 * 0.1 / 0.1
_RETRACT = 0.1 / 0.2 + 0.1 / 0.1  # 加速度 2.0 * 0.1^2 / 0.1 = 0.2、台形
_DOT_SECONDS = _DESCEND + _PRIME + _DISPENSE + 0.1 + max(_RETRACT, _DESCEND)


def _rect(x: float, y: float, width: float = 0.5, height: float = 0.5) -> Polygon:
    """中心 (x, y) の矩形（既定は 0.5 mm 角）."""
    w, h = width / 2.0, height / 2.0
    return Polygon([(x - w, y - h), (x + w, y - h), (x + w, y + h), (x - w, y + h)])


def _plan(*pads: Polygon, params: PasteParams = _DOT) -> PastePlan:
    """``pads`` を 1 つずつ持つ部品を並べた基板の塗布計画."""
    config: PasteDispenserConfig = attrs.evolve(
        load_machine(TEST_CONFIG_DIR / "machine.toml").paste_dispenser,
        defaults=params,
        motion=_MOTION,
    )
    names = [f"R{i}" for i in range(1, len(pads) + 1)]
    board = Board(
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
        copper=(),
    )
    return plan_paste(BoardSession.initial("t.kicad_pcb", board, config))


class TestEstimatePasteTact:
    def test_one_dot_is_descend_prime_dispense_settle_and_the_longer_of_retract_and_lift(
        self,
    ):
        estimate = estimate_paste_tact(_plan(_rect(10.0, 10.0)), _NO_SETUP, _LIMITS)

        assert estimate.pad_count == 1
        assert estimate.paste_seconds == pytest.approx(_DOT_SECONDS)

    def test_travel_between_pads_uses_the_xy_limits(self):
        estimate = estimate_paste_tact(
            _plan(_rect(10.0, 10.0), _rect(20.0, 10.0)), _NO_SETUP, _LIMITS
        )

        # XY 10 mm は 50^2/500 = 5 mm より長いので台形: 50/500 + 10/50
        travel = 50.0 / 500.0 + 10.0 / 50.0
        assert estimate.pad_count == 2
        assert estimate.paste_seconds == pytest.approx(2 * _DOT_SECONDS + travel)

    def test_total_adds_the_setup(self):
        estimate = estimate_paste_tact(
            _plan(_rect(10.0, 10.0)), TactConfig(setup_sec=120.0), _LIMITS
        )

        assert estimate.setup_seconds == 120.0
        assert estimate.total_seconds == pytest.approx(120.0 + _DOT_SECONDS)

    def test_no_pads_is_only_the_setup(self):
        estimate = estimate_paste_tact(_plan(), TactConfig(setup_sec=60.0), _LIMITS)

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

        estimate = estimate_paste_tact(plan, _NO_SETUP, _LIMITS)

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
            estimate_paste_tact(plan, _NO_SETUP, slower).paste_seconds
            > estimate_paste_tact(plan, _NO_SETUP, _LIMITS).paste_seconds
        )

    def test_more_pads_is_longer(self):
        seconds = [
            estimate_paste_tact(
                _plan(*self._PADS[:count]), _NO_SETUP, _LIMITS
            ).paste_seconds
            for count in range(len(self._PADS) + 1)
        ]

        assert seconds == sorted(seconds)
        assert len(set(seconds)) == len(seconds)


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
