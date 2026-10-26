"""運転時流量キャリブレーションの計画・実行・補正の公開契約.

実行で最優先は **補正できなくても例外にしない**こと。
この時点で位置合わせと高さ計測が終わっており、後から足した補正の失敗で塗布を取りやめるのは割に合わない。
装置側は自前 HAL の fake。
"""

import json
import math
import re
import time
from pathlib import Path

import attrs
import pytest
from shapely import Polygon

from pcbasm.config import load_machine
from pcbasm.config.solder import VolumeCorrectionConfig
from pcbasm.geometry import Point2d
from pcbasm.hal.fake import FAKE_PRINTER_CONFIG, FakeCamera, FakeKlipper
from pcbasm.pasting.paste_volume.calibration import (
    CALIBRATION_SUFFIX,
    parse_calibration,
    write_calibration,
)
from pcbasm.pasting.paste_volume.estimator import PasteVolumePrediction
from pcbasm.pasting.paste_volume.runtime_correction import (
    MAX_CORRECTION_SCALE,
    MIN_CORRECTION_SCALE,
    FlowCalibrationOutcome,
    correct_rotations_per_ul,
    overlapping_crops,
    plan_flow_calibration,
    run_flow_calibration,
    validate_crop_separation,
)
from tests.support.paste_session import (
    FOCUS_Z,
    RecordingHooks,
    full_board_correction,
    paste_session,
    uniform_frame,
)
from tests.support.paths import PASTE_VOLUME_CALIBRATION, TEST_CONFIG_DIR

OUTLINE = Polygon([(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)])
ENABLED = VolumeCorrectionConfig(calibration_file="cal.paste-volume.json")


def _accepted(volume_ul: float) -> PasteVolumePrediction:
    return PasteVolumePrediction(
        mean_volume_ul=volume_ul,
        accepted=True,
        rejection_reason=None,
    )


def _rejected(reason: str) -> PasteVolumePrediction:
    return PasteVolumePrediction(
        mean_volume_ul=0.0,
        accepted=False,
        rejection_reason=reason,
    )


class TestPlanFlowCalibration:
    """基板ごとに 1 点ずつ与えた測定位置の検証."""

    def test_keeps_the_configured_points_in_order(self):
        points = (Point2d(10.0, 12.0), Point2d(13.0, 12.0), Point2d(16.0, 20.0))

        plan, error = plan_flow_calibration(
            config=ENABLED, points=points, outline=OUTLINE
        )

        assert error is None
        assert plan is not None
        assert plan.points == points

    def test_carries_the_commanded_amount_and_crop_size(self):
        plan, error = plan_flow_calibration(
            config=ENABLED,
            points=(Point2d(10.0, 12.0), Point2d(13.0, 12.0), Point2d(16.0, 12.0)),
            outline=OUTLINE,
        )

        assert error is None
        assert plan is not None
        assert plan.amount_ul == pytest.approx(0.2)
        assert plan.crop_size_mm == pytest.approx(2.0)
        assert plan.settle_seconds == pytest.approx(10.0)
        assert plan.total_commanded_ul == pytest.approx(0.6)

    def test_is_disabled_when_no_point_is_configured(self):
        plan, error = plan_flow_calibration(config=ENABLED, points=(), outline=OUTLINE)

        assert error is None
        assert plan is None

    def test_is_disabled_when_no_calibration_file_is_configured(self):
        plan, error = plan_flow_calibration(
            config=VolumeCorrectionConfig(),
            points=(Point2d(10.0, 12.0),),
            outline=OUTLINE,
        )

        assert error is None
        assert plan is None

    def test_rejects_a_point_outside_the_board_outline(self):
        plan, error = plan_flow_calibration(
            config=ENABLED,
            points=(Point2d(10.0, 12.0), Point2d(-1.0, 12.0)),
            outline=OUTLINE,
        )

        assert plan is None
        assert error is not None
        assert "基板外形" in error

    def test_rejects_a_non_finite_point(self):
        plan, error = plan_flow_calibration(
            config=ENABLED, points=(Point2d(math.nan, 12.0),), outline=OUTLINE
        )

        assert plan is None
        assert error is not None

    def test_rejects_points_whose_crops_overlap(self):
        """隣のドットが crop へ写り込むと最大連結成分が別のドットになる."""
        plan, error = plan_flow_calibration(
            config=VolumeCorrectionConfig(
                calibration_file="cal.paste-volume.json", crop_size_mm=4.0
            ),
            points=(Point2d(10.0, 12.0), Point2d(13.0, 12.0)),
            outline=OUTLINE,
        )

        assert plan is None
        assert error is not None
        assert "撮影範囲" in error

    @pytest.mark.parametrize(
        ("crop_size_mm", "points"),
        [
            # crop が接するだけ（中心間距 = crop 幅）なら重ならない
            (4.0, (Point2d(10.0, 12.0), Point2d(14.0, 12.0))),
            # Crop は正方形なので、X が近くても Y が離れていれば重ならない
            (2.0, (Point2d(10.0, 12.0), Point2d(10.0, 15.0))),
            # 1 点だけなら crop がどんなに大きくても重ならない
            (8.0, (Point2d(10.0, 12.0),)),
        ],
    )
    def test_allows_points_whose_crops_do_not_overlap(
        self, crop_size_mm: float, points: tuple[Point2d, ...]
    ):
        plan, error = plan_flow_calibration(
            config=VolumeCorrectionConfig(
                calibration_file="cal.paste-volume.json", crop_size_mm=crop_size_mm
            ),
            points=points,
            outline=OUTLINE,
        )

        assert error is None
        assert plan is not None
        assert plan.points == points


class TestValidateCropSeparation:
    """撮影範囲の重なり判定（保存では撥ねず、計画時にだけ効く）."""

    def test_reports_which_pair_overlaps_with_1_based_numbers(self):
        error = validate_crop_separation(
            points=(Point2d(0.0, 0.0), Point2d(10.0, 0.0), Point2d(11.0, 0.0)),
            crop_size_mm=2.0,
        )

        assert error is not None
        assert "測定位置 2 と 3" in error

    def test_accepts_an_empty_set(self):
        assert validate_crop_separation(points=(), crop_size_mm=2.0) is None

    def test_rejects_a_non_positive_crop_size(self):
        error = validate_crop_separation(points=(Point2d(0.0, 0.0),), crop_size_mm=0.0)

        assert error is not None


class TestOverlappingCrops:
    """図で置き直しの対象を示すための、点ごとの重なりフラグ."""

    def test_marks_both_points_of_an_overlapping_pair(self):
        flags = overlapping_crops(
            (Point2d(0.0, 0.0), Point2d(1.0, 0.0), Point2d(10.0, 0.0)),
            crop_size_mm=2.0,
        )

        assert flags == (True, True, False)

    def test_marks_nothing_when_all_are_separated(self):
        flags = overlapping_crops(
            (Point2d(0.0, 0.0), Point2d(3.0, 0.0)), crop_size_mm=2.0
        )

        assert flags == (False, False)

    def test_a_non_positive_crop_size_marks_nothing(self):
        assert overlapping_crops((Point2d(0.0, 0.0),), crop_size_mm=0.0) == (False,)


class TestCorrectRotationsPerUl:
    """推定体積から ``rotations_per_ul`` を補正する."""

    @pytest.mark.parametrize(("previous", "expected"), [(1.0, 0.8), (1.5, 1.2)])
    def test_over_dispensing_raises_rotations_per_ul(
        self, previous: float, expected: float
    ):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.25), _accepted(0.25), _accepted(0.25)],
            amount_ul=0.2,
            rotations_per_ul=previous,
        )

        assert error is None
        assert outcome is not None
        assert outcome.ratio == pytest.approx(1.25)
        # 報告用に補正前の係数も残す
        assert outcome.previous_rotations_per_ul == pytest.approx(previous)
        assert outcome.rotations_per_ul == pytest.approx(expected)

    def test_under_dispensing_lowers_rotations_per_ul(self):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.16)], amount_ul=0.2, rotations_per_ul=1.0
        )

        assert error is None
        assert outcome is not None
        assert outcome.ratio == pytest.approx(0.8)
        assert outcome.rotations_per_ul == pytest.approx(1.25)

    def test_aggregates_by_total_volume_not_by_averaging_ratios(self):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.1), _accepted(0.3)], amount_ul=0.2, rotations_per_ul=2.0
        )

        assert error is None
        assert outcome is not None
        assert outcome.estimated_ul == pytest.approx(0.4)
        assert outcome.commanded_ul == pytest.approx(0.4)
        assert outcome.ratio == pytest.approx(1.0)
        assert outcome.rotations_per_ul == pytest.approx(2.0)

    def test_only_accepted_predictions_enter_the_aggregate(self):
        outcome, error = correct_rotations_per_ul(
            [
                _accepted(0.25),
                _rejected("diameter_below_reliable_range"),
                _accepted(0.25),
            ],
            amount_ul=0.2,
            rotations_per_ul=1.0,
        )

        assert error is None
        assert outcome is not None
        assert outcome.accepted_count == 2
        assert outcome.commanded_ul == pytest.approx(0.4)
        assert outcome.ratio == pytest.approx(1.25)
        assert outcome.rejections == ("diameter_below_reliable_range",)

    def test_no_accepted_prediction_means_no_correction(self):
        outcome, error = correct_rotations_per_ul(
            [_rejected("no_deposit_detected")], amount_ul=0.2, rotations_per_ul=1.0
        )

        assert outcome is None
        assert error is not None
        assert "no_deposit_detected" in error

    def test_an_empty_prediction_list_means_no_correction(self):
        outcome, error = correct_rotations_per_ul(
            [], amount_ul=0.2, rotations_per_ul=1.0
        )

        assert outcome is None
        assert error is not None

    def test_clamps_a_ratio_that_would_shrink_the_factor_too_far(self):
        outcome, error = correct_rotations_per_ul(
            [_accepted(2.0)], amount_ul=0.2, rotations_per_ul=1.0
        )

        assert error is None
        assert outcome is not None
        assert outcome.ratio == pytest.approx(10.0)
        assert outcome.clamped is True
        assert outcome.rotations_per_ul == pytest.approx(MIN_CORRECTION_SCALE)

    def test_clamps_a_ratio_that_would_grow_the_factor_too_far(self):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.01)], amount_ul=0.2, rotations_per_ul=1.0
        )

        assert error is None
        assert outcome is not None
        assert outcome.clamped is True
        assert outcome.rotations_per_ul == pytest.approx(MAX_CORRECTION_SCALE)

    def test_a_zero_estimate_cannot_produce_a_factor(self):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.0)], amount_ul=0.2, rotations_per_ul=1.0
        )

        assert outcome is None
        assert error is not None

    @pytest.mark.parametrize("amount_ul", [0.0, -0.1, math.nan])
    def test_rejects_an_invalid_commanded_amount(self, amount_ul: float):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.2)], amount_ul=amount_ul, rotations_per_ul=1.0
        )

        assert outcome is None
        assert error is not None

    @pytest.mark.parametrize("rotations_per_ul", [0.0, -1.0, math.inf])
    def test_rejects_an_invalid_previous_factor(self, rotations_per_ul: float):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.2)], amount_ul=0.2, rotations_per_ul=rotations_per_ul
        )

        assert outcome is None
        assert error is not None


def _outcome(**overrides: object) -> FlowCalibrationOutcome:
    values: dict[str, object] = {
        "commanded_ul": 0.6,
        "estimated_ul": 0.75,
        "ratio": 1.25,
        "previous_rotations_per_ul": 1.0,
        "rotations_per_ul": 0.8,
        "clamped": False,
        "accepted_count": 3,
        "rejections": (),
    }
    values.update(overrides)
    return FlowCalibrationOutcome(**values)  # pyright: ignore[reportArgumentType]


class TestOutcomeSummary:
    """補正結果の 1 行表示（ログとジョブのまとめで共用する）."""

    def test_carries_the_factor_change_and_the_accepted_count(self):
        line = _outcome().summary()

        assert "3 点採用" in line
        assert "1.0000" in line
        assert "0.8000" in line

    def test_marks_a_clamped_correction(self):
        assert "頭打ち" in _outcome(clamped=True).summary()


POINTS = (Point2d(10.0, 6.0), Point2d(13.0, 6.0), Point2d(16.0, 6.0))
_MOVE_RE = re.compile(r"MANUAL_STEPPER STEPPER=paste_dispenser MOVE=(\S+)(.*)")
_ROTATION_DISTANCE = float(
    FAKE_PRINTER_CONFIG["manual_stepper paste_dispenser"]["rotation_distance"]
)


def _dispense_amounts_ul(klipper: FakeKlipper, rotations_per_ul: float) -> list[float]:
    """塗布吐出（SET_POSITION 直後の SYNC=0 付き MOVE）の量 [μL] を呼び出し順に返す."""
    lines = klipper.sent_lines
    amounts: list[float] = []
    for previous, line in zip(lines, lines[1:], strict=False):
        match = _MOVE_RE.match(line)
        if (
            previous.endswith("SET_POSITION=0.0")
            and match
            and "SYNC=0" in match.group(2)
        ):
            amounts.append(
                float(match.group(1)) / _ROTATION_DISTANCE / rotations_per_ul
            )
    return amounts


@attrs.frozen
class _Ran:
    """`run_flow_calibration` 1 回の結果.

    Attributes:
        outcome: 補正の結果
        focus_z_moves: 送った G1 のうちピント高さへの移動の位置（撮影パスと塗布パスの並びを見る）
        move_count: 送った G1 の数
        dispensed: 塗布吐出の量 [μL]（プライム押し戻しを含む）
        elapsed: かかった秒数
    """

    outcome: FlowCalibrationOutcome | None
    focus_z_moves: list[int]
    move_count: int
    dispensed: list[float]
    elapsed: float


class TestRunFlowCalibration:
    """撮影 → 塗布 → 撮影 → 推定 → 補正の実行."""

    @staticmethod
    def _write_calibration(root: Path) -> str:
        """検証用のピン済み校正を保存し、ファイル名を返す."""
        calibration, error = parse_calibration(
            json.loads(PASTE_VOLUME_CALIBRATION.read_text(encoding="utf-8"))
        )
        assert error is None, error
        assert calibration is not None
        name = f"runtime{CALIBRATION_SUFFIX}"
        write_calibration(root / name, calibration)
        return name

    @staticmethod
    def _run(
        root: Path,
        calibration_file: str,
        *,
        settle_seconds: float = 0.0,
        prime_extra_delay: float = 0.0,
    ) -> _Ran:
        machine = load_machine(TEST_CONFIG_DIR / "machine.toml")
        machine = attrs.evolve(
            machine,
            solder=attrs.evolve(
                machine.require_solder(),
                defaults=attrs.evolve(
                    machine.require_solder().defaults,
                    prime_extra_delay=prime_extra_delay,
                ),
            ),
        )
        klipper = FakeKlipper()
        session = paste_session(FakeCamera([uniform_frame()]), klipper, machine)
        plan, error = plan_flow_calibration(
            config=VolumeCorrectionConfig(
                calibration_file=calibration_file, settle_seconds=settle_seconds
            ),
            points=POINTS,
            outline=session.board_frame.board.outline.polygon,
        )
        assert plan is not None, error
        with session.make_applicator() as applicator:
            klipper.clear_sent()
            started = time.monotonic()
            outcome = run_flow_calibration(
                RecordingHooks(),
                session,
                full_board_correction(),
                applicator,
                plan,
                calibration_dir=root,
            )
            elapsed = time.monotonic() - started
            moves = [move.get("z") for move in klipper.g1_moves()]
            return _Ran(
                outcome=outcome,
                focus_z_moves=[
                    index
                    for index, z in enumerate(moves)
                    if z is not None and z == pytest.approx(FOCUS_Z)
                ],
                move_count=len(moves),
                dispensed=_dispense_amounts_ul(
                    klipper, machine.require_solder().dispenser.rotations_per_ul
                ),
                elapsed=elapsed,
            )

    def test_a_missing_calibration_file_does_not_correct(self, tmp_path: Path):
        ran = self._run(tmp_path, "absent.paste-volume.json")

        assert ran.outcome is None
        assert ran.move_count == 0

    def test_undetectable_dots_do_not_correct(self, tmp_path: Path):
        """一様なフレームでは塗布前後の差が無い。補正せず進む."""
        ran = self._run(tmp_path, self._write_calibration(tmp_path))

        assert ran.outcome is None

    def test_captures_every_point_before_dispensing_and_again_after(
        self, tmp_path: Path
    ):
        """3 パス構成。塗ってすぐ撮ると点ごとに落ち着き時間が変わる."""
        ran = self._run(tmp_path, self._write_calibration(tmp_path))

        focus = ran.focus_z_moves
        assert len(focus) == 6
        # 先頭 3 つが pre パス、末尾 3 つが post パス。間に塗布の移動が挟まる
        assert focus[:3] == [0, 1, 2]
        last = ran.move_count
        assert focus[3:] == [last - 3, last - 2, last - 1]

    def test_waits_for_the_paste_to_settle_before_the_post_pass(self, tmp_path: Path):
        """塗り終えてすぐ撮ると、広がりきる前の小さい円を測ることになる."""
        ran = self._run(tmp_path, self._write_calibration(tmp_path), settle_seconds=1.5)

        assert ran.elapsed >= 1.5

    def test_dispenses_only_the_commanded_amount_despite_prime_extra_delay(
        self, tmp_path: Path
    ):
        """追加遅延ぶんの押し出しは補正の比の分母に入らないので、出さない."""
        ran = self._run(
            tmp_path, self._write_calibration(tmp_path), prime_extra_delay=0.5
        )

        retract_ul = (
            load_machine(TEST_CONFIG_DIR / "machine.toml")
            .require_solder()
            .dispenser.retract_amount
        )
        expected = retract_ul + VolumeCorrectionConfig().amount_ul
        assert ran.dispensed == pytest.approx([expected] * len(POINTS))
