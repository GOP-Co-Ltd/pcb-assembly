"""FlowCalibrationProcedure のテスト.

自前 HAL の fake は ``FakeKlipper`` だけで、``XYZStage`` / ``PasteDispenser`` /
``PasteApplicator`` は実物を組み合わせる。``setup`` は Board 計測結果（実カメラ）を
要するため unit では通さず、コンストラクタに ``PasteSession`` と transform を直接渡す。
検証は送信された G-code（``MANUAL_STEPPER ... MOVE=`` の距離を rotation_distance と
rotations_per_ul で μL に戻す、``G1`` の座標を読む）に対して行う。
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from pcbasm.config import Machine
from pcbasm.geometry import Identity, Point2d, Shift
from pcbasm.hal import XYZStage
from pcbasm.pasting.flowcalib.flow import RotationsPerUlRound
from pcbasm.pasting.flowcalib.procedure import FlowCalibrationProcedure
from pcbasm.pasting.session import PasteSession
from tests.helpers import FAKE_PRINTER_CONFIG, TESTING_DATA_DIR, FakeKlipper

MACHINE = Machine(TESTING_DATA_DIR / "machine.toml")
ROTATION_DISTANCE = float(
    FAKE_PRINTER_CONFIG["manual_stepper paste_dispenser"]["rotation_distance"]
)
RETRACT_AMOUNT = MACHINE.paste_dispenser.retract_amount

_MOVE_RE = re.compile(r"MANUAL_STEPPER STEPPER=paste_dispenser MOVE=(\S+)(.*)")
_G1_RE = re.compile(r"G1 (.*)")

LINES = (
    (Point2d(0.0, 0.0), Point2d(10.0, 0.0)),
    (Point2d(0.0, 3.0), Point2d(10.0, 3.0)),
    (Point2d(0.0, 6.0), Point2d(10.0, 6.0)),
)


def _session(klipper: FakeKlipper) -> PasteSession:
    unused: Any = object()
    return PasteSession(
        machine=MACHINE,
        klipper=klipper,
        stage=XYZStage(klipper.readonly),
        camera=unused,
        calibration=unused,
        calibration_result=unused,
        board_transform=Identity(),
        offset_transform=unused,
        toolhead_offset=Identity(),
        pcb=unused,
        probe_executor=unused,
        height_measurer=unused,
    )


def _dispense_distances_mm(klipper: FakeKlipper) -> list[float]:
    """塗布吐出（SET_POSITION 直後の SYNC=0 付き MOVE）の距離 [mm] を呼び出し順に返す."""
    lines = klipper.sent_lines
    distances: list[float] = []
    for previous, line in zip(lines, lines[1:], strict=False):
        match = _MOVE_RE.match(line)
        if (
            previous.endswith("SET_POSITION=0.0")
            and match
            and "SYNC=0" in match.group(2)
        ):
            distances.append(float(match.group(1)))
    return distances


def _dispense_amounts_ul(klipper: FakeKlipper, rotations_per_ul: float) -> list[float]:
    return [
        distance / ROTATION_DISTANCE / rotations_per_ul
        for distance in _dispense_distances_mm(klipper)
    ]


def _g1_moves(klipper: FakeKlipper) -> list[dict[str, float]]:
    moves: list[dict[str, float]] = []
    for line in klipper.sent_lines:
        match = _G1_RE.match(line)
        if match:
            moves.append(
                {part[0].lower(): float(part[1:]) for part in match.group(1).split()}
            )
    return moves


@pytest.fixture
def klipper() -> FakeKlipper:
    return FakeKlipper()


@pytest.fixture
def procedure(klipper: FakeKlipper) -> FlowCalibrationProcedure:
    return FlowCalibrationProcedure(_session(klipper), Shift(x=100.0, y=50.0, z=0.0))


class TestLifecycle:
    """With で applicator を有効化し、抜けるときに無効化する."""

    def test_enter_enables_and_exit_disables_dispenser(self, procedure, klipper):
        with procedure:
            enabled = klipper.sent_lines
            klipper.clear_sent()
        disabled = klipper.sent_lines

        assert any("ENABLE=1" in line for line in enabled)
        assert any("ENABLE=0" in line for line in disabled)

    def test_initial_values_mirror_machine_config(self, procedure):
        config = MACHINE.paste_dispenser

        assert procedure.rotations_per_ul == config.rotations_per_ul
        assert procedure.dispense_accel == config.dispense_accel
        assert procedure.applicator.rotations_per_ul == config.rotations_per_ul


class TestDrawLines:
    """Retract 1 回 + 線 N 本を transform 適用で塗布する."""

    def test_sends_one_retract_then_one_gcode_per_line(self, procedure, klipper):
        with procedure:
            klipper.clear_sent()
            executions = procedure.draw_lines(LINES, amount_ul=0.5)
            # retract（load）1 送信 + 各線 1 送信（__exit__ の disable 送信は含めない）
            assert len(klipper.sent) == 1 + len(LINES)

        assert len(executions) == len(LINES)
        assert all(e.applied_mode == "line" for e in executions)
        assert _dispense_amounts_ul(
            klipper, procedure.rotations_per_ul
        ) == pytest.approx([RETRACT_AMOUNT + 0.5] * len(LINES))

    def test_retract_false_skips_the_leading_retract(self, procedure, klipper):
        with procedure:
            klipper.clear_sent()
            procedure.draw_lines(LINES[:1], amount_ul=0.5, retract=False)
            assert len(klipper.sent) == 1

        assert _dispense_amounts_ul(
            klipper, procedure.rotations_per_ul
        ) == pytest.approx([RETRACT_AMOUNT + 0.5])

    def test_transform_is_applied_to_every_line(self, procedure, klipper):
        with procedure:
            klipper.clear_sent()
            procedure.draw_lines(LINES, amount_ul=0.5)

        visited = {
            (round(m["x"], 6), round(m["y"], 6)) for m in _g1_moves(klipper) if "x" in m
        }
        for start, end in LINES:
            assert (start.x + 100.0, start.y + 50.0) in visited
            assert (end.x + 100.0, end.y + 50.0) in visited

    def test_per_line_amounts(self, procedure, klipper):
        with procedure:
            klipper.clear_sent()
            procedure.draw_lines(LINES, amount_ul=(0.5, 1.0, 1.5))

        assert _dispense_amounts_ul(
            klipper, procedure.rotations_per_ul
        ) == pytest.approx(
            [RETRACT_AMOUNT + 0.5, RETRACT_AMOUNT + 1.0, RETRACT_AMOUNT + 1.5]
        )

    def test_per_line_fill_speed_changes_effective_speed(self, procedure):
        with procedure:
            executions = procedure.draw_lines(
                LINES, amount_ul=0.5, fill_speed=(1.0, 4.0, 8.0), rate_cap=float("inf")
            )

        speeds = [
            e.fill_speed.resolve(100.0) for e in executions if e.fill_speed is not None
        ]
        assert speeds == pytest.approx([1.0, 4.0, 8.0])

    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(amount_ul=(0.5, 1.0)),
            dict(amount_ul=0.5, fill_speed=(1.0,)),
            dict(amount_ul=0.5, rate_cap=(1.0, 2.0, 3.0, 4.0)),
        ],
    )
    def test_sequence_length_mismatch_raises(self, procedure, klipper, kwargs):
        with procedure, pytest.raises(ValueError, match="線数"):
            procedure.draw_lines(LINES, **kwargs)

    def test_checkpoint_is_called_with_each_line_index_before_drawing(
        self, procedure, klipper
    ):
        seen: list[tuple[int, int]] = []

        def checkpoint(index: int) -> None:
            seen.append((index, len(klipper.sent)))

        with procedure:
            klipper.clear_sent()
            procedure.draw_lines(LINES, amount_ul=0.5, checkpoint=checkpoint)

        # retract 送信（1 件）の後、線 i を引く前に (i, 1 + i) で呼ばれる
        assert seen == [(0, 1), (1, 2), (2, 3)]


class TestRemovalZ:
    """退避 Z = max(z_min, z_max - offset)（FAKE の Z 可動域は -5..50）."""

    @pytest.mark.parametrize(
        ("offset", "expected"), [(0.0, 50.0), (10.0, 40.0), (100.0, -5.0)]
    )
    def test_removal_z_clamps_to_limits(self, procedure, offset, expected):
        assert procedure.removal_z(offset) == expected

    def test_move_to_removal_z_sends_blocking_z_move(self, procedure, klipper):
        z = procedure.move_to_removal_z(10.0)

        assert z == 40.0
        assert _g1_moves(klipper)[-1]["z"] == pytest.approx(40.0)
        assert klipper.sent_lines[-1] == "M400"

    def test_move_to_loading_z_sends_z_zero(self, procedure, klipper):
        procedure.move_to_loading_z()

        assert _g1_moves(klipper)[-1]["z"] == pytest.approx(0.0)
        assert klipper.sent_lines[-1] == "M400"


class TestAdopt:
    """① の採用で rotations_per_ul / dispense_accel を更新し applicator を作り直す."""

    def test_adopt_updates_values_and_rebuilds_applicator(self, procedure, klipper):
        round_ = RotationsPerUlRound(
            previous=procedure.rotations_per_ul,
            computed=2.0,
            dispense_accel=5.0,
            rotations_used=5.0,
        )
        with procedure:
            klipper.clear_sent()
            procedure.adopt(round_)
            switched = klipper.sent_lines
            klipper.clear_sent()
            procedure.draw_lines(LINES[:1], amount_ul=0.5)

        assert procedure.rotations_per_ul == 2.0
        assert procedure.dispense_accel == 5.0
        assert procedure.applicator.rotations_per_ul == 2.0
        # 旧 applicator の無効化 → 新 applicator の有効化
        assert [line for line in switched if "ENABLE=" in line] == [
            next(line for line in switched if "ENABLE=0" in line),
            next(line for line in switched if "ENABLE=1" in line),
        ]
        # 以降の線引きは新 rotations_per_ul で回転距離が決まる
        assert _dispense_distances_mm(klipper) == pytest.approx(
            [(RETRACT_AMOUNT + 0.5) * 2.0 * ROTATION_DISTANCE]
        )
