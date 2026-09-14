"""`pcbasm.pasting.nozzle_clean` の仕様テスト.

塗布ジョブ開始時のノズル先端クリーニング（クリーニング位置でパージ → 十字往復で
こすり）の契約を固定する。

G-code を返す関数の契約:

- `approach_gcode`: 「Z を退避高さへ → クリーニング XY へ → 面 Z へ」の 3 段。
  中間セグメントに Z ワードを含めない（Z 先行退避の意味を壊さない）
- `wipe_gcode`: 面 Z から押し込み Z への単独 Z 下降（travel 速度）に続けて十字往復
  （wipe 速度）。押し込み下降を wipe 速度で行わない
- `depart_gcode`: 退避 Z へ戻すだけで XY を動かさない
- いずれも M400 / M84 を含めない（`clean_nozzle` が後置で合成する）

`clean_nozzle` の手順契約:

- 可動域検証は何かを送る前に行う。パージ後にこすりで落ちるとシリコン上に塊が残る
- リトラクトしない（負の押出 MOVE を出さない）。引き戻しは呼び出し側の
  `PasteApplicator.retract()` が担い、二重に引くとプライム収支が合わなくなる
- 最後は必ず退避 Z へ戻す（押し込み Z のまま板の上を走らせない）

自前 HAL の fake は `FakeKlipper` だけで、`XYZStage` / `PasteDispenser` /
`PasteApplicator` は実物を組み合わせる（skill `testing-strategy`）。
"""

from __future__ import annotations

import re

import attrs
import pytest

from pcbasm.config import (
    NozzleClean,
    PasteDispenser as PasteDispenserConfig,
    Toolhead,
)
from pcbasm.geometry import Point3d
from pcbasm.hal import XYZStage
from pcbasm.pasting.applicator import PasteApplicator, build_applicator
from pcbasm.pasting.nozzle_clean import (
    CLEAN_TRAVEL_VELOCITY,
    TRAVEL_Z,
    approach_gcode,
    clean_nozzle,
    clean_position_label,
    depart_gcode,
    validate_reach,
    wipe_gcode,
    wipe_points,
)
from tests.helpers import FAKE_PRINTER_CONFIG, FakeKlipper

# FAKE_PRINTER_CONFIG: x,y ∈ [0,300] / z ∈ [-5,50] / max_velocity = 100
CENTER_X = 68.0
CENTER_Y = 53.0
SURFACE_Z = -3.0

TRAVEL_FEED = CLEAN_TRAVEL_VELOCITY * 60
ROTATION_DISTANCE = float(
    FAKE_PRINTER_CONFIG["manual_stepper paste_dispenser"]["rotation_distance"]
)
ROTATIONS_PER_UL = 45.0

_MOVE_RE = re.compile(r"MANUAL_STEPPER STEPPER=paste_dispenser MOVE=(-?[\d.]+)")


def _clean(**overrides) -> NozzleClean:
    values = {
        "x": CENTER_X,
        "y": CENTER_Y,
        "z": SURFACE_Z,
        "press_depth": 0.5,
        "purge_ul": 0.2,
        "stroke": 2.0,
        "passes": 1,
        "wipe_speed": 10.0,
    }
    return NozzleClean(**(values | overrides))


@pytest.fixture
def klipper() -> FakeKlipper:
    return FakeKlipper()


@pytest.fixture
def stage(klipper: FakeKlipper) -> XYZStage:
    return XYZStage(klipper.readonly)


@pytest.fixture
def applicator(klipper: FakeKlipper) -> PasteApplicator:
    config = PasteDispenserConfig(
        rotations_per_ul=ROTATIONS_PER_UL,
        nozzle_diameter=0.34,
        max_fill_speed=5.0,
        max_dispense_rate=1.0,
        dispense_accel=10.0,
        retract_amount=10.0,
        retract_rate=10.0,
        retract_accel_factor=2.0,
        toolhead=Toolhead(x=0.0, y=0.0),
        paste_height=0.5,
        lift_height=5.0,
        ul_per_mm2=0.05,
    )
    return build_applicator(klipper, XYZStage(klipper.readonly), config)


def _stepper_amounts_ul(klipper: FakeKlipper) -> list[float]:
    """ディスペンサーの押出距離を μL に戻して呼び出し順に返す."""
    return [
        float(match.group(1)) / ROTATION_DISTANCE / ROTATIONS_PER_UL
        for line in klipper.sent_lines
        if (match := _MOVE_RE.match(line))
    ]


class TestWipePoints:
    """十字往復の点列（マシン座標）の契約."""

    def test_single_pass_traces_cross_through_center(self):
        points = wipe_points(_clean(passes=1))

        assert points.points == (
            Point3d(68.0, 53.0, -3.5),
            Point3d(70.0, 53.0, -3.5),
            Point3d(66.0, 53.0, -3.5),
            Point3d(68.0, 53.0, -3.5),
            Point3d(68.0, 55.0, -3.5),
            Point3d(68.0, 51.0, -3.5),
            Point3d(68.0, 53.0, -3.5),
        )

    def test_every_point_sits_at_press_z(self):
        clean = _clean(passes=2)

        assert {p.z for p in wipe_points(clean)} == {clean.press_z}

    def test_two_passes_do_not_repeat_the_center(self):
        """各 pass 末尾の中心が次 pass の始点を兼ねるので 13 点になる."""
        points = wipe_points(_clean(passes=2))

        assert len(points) == 13
        assert points[6] == points[0]

    def test_starts_and_ends_at_center(self):
        """退避が XY を動かさない前提を担保する."""
        clean = _clean(passes=3)
        points = wipe_points(clean)
        center = Point3d(clean.x, clean.y, clean.press_z)

        assert points[0] == center
        assert points[-1] == center

    @pytest.mark.parametrize("overrides", [{"stroke": 0.0}, {"passes": 0}])
    def test_disabled_wipe_yields_center_only(self, overrides):
        points = wipe_points(_clean(**overrides))

        assert len(points) == 1
        assert points[0] == Point3d(CENTER_X, CENTER_Y, SURFACE_Z - 0.5)


class TestApproachGcode:
    """クリーニング位置への接近シーケンスの契約."""

    def test_retracts_z_before_moving_xy(self, stage: XYZStage):
        lines = approach_gcode(stage, _clean()).to_list()

        assert lines == [
            "G90",
            f"G1 Z{TRAVEL_Z} F{TRAVEL_FEED}",
            f"G1 X68.0 Y53.0 F{TRAVEL_FEED}",
            f"G1 Z-3.0 F{TRAVEL_FEED}",
        ]

    def test_caps_travel_speed_at_stage_max_velocity(self, stage: XYZStage):
        slow = FakeKlipper(
            config={**FAKE_PRINTER_CONFIG, "printer": {"max_velocity": "5"}}
        )
        lines = approach_gcode(XYZStage(slow.readonly), _clean()).to_list()

        assert lines[1] == f"G1 Z{TRAVEL_Z} F{5 * 60.0}"

    def test_contains_no_m400_or_m84(self, stage: XYZStage):
        lines = approach_gcode(stage, _clean()).to_list()

        assert "M400" not in lines
        assert "M84" not in lines

    def test_out_of_limits_position_raises(self, stage: XYZStage):
        with pytest.raises(ValueError, match="制限外"):
            approach_gcode(stage, _clean(x=999.0))


class TestWipeGcode:
    """押し込みと十字往復の契約."""

    def test_presses_down_at_travel_speed_before_wiping(self, stage: XYZStage):
        lines = wipe_gcode(stage, _clean()).to_list()

        assert lines[0] == f"G1 Z-3.5 F{TRAVEL_FEED}"

    def test_wipe_moves_use_wipe_speed(self, stage: XYZStage):
        lines = wipe_gcode(stage, _clean(wipe_speed=10.0)).to_list()

        assert all(line.endswith(f"F{10.0 * 60}") for line in lines[1:])

    def test_wipe_moves_follow_wipe_points(self, stage: XYZStage):
        clean = _clean(passes=1)

        lines = wipe_gcode(stage, clean).to_list()

        assert len(lines) == 1 + len(wipe_points(clean))
        assert lines[1] == f"G1 X68.0 Y53.0 Z-3.5 F{10.0 * 60}"
        assert lines[2] == f"G1 X70.0 Y53.0 Z-3.5 F{10.0 * 60}"

    def test_contains_no_m400_or_m84(self, stage: XYZStage):
        lines = wipe_gcode(stage, _clean()).to_list()

        assert "M400" not in lines
        assert "M84" not in lines

    def test_press_below_z_limit_raises(self, stage: XYZStage):
        # z ∈ [-5, 50] に対し press_z = -4.8 - 0.5 = -5.3
        with pytest.raises(ValueError, match="制限外"):
            wipe_gcode(stage, _clean(z=-4.8, press_depth=0.5))


class TestDepartGcode:
    """こすり後の退避の契約."""

    def test_lifts_to_travel_z_without_moving_xy(self, stage: XYZStage):
        lines = depart_gcode(stage, _clean()).to_list()

        assert lines == [f"G1 Z{TRAVEL_Z} F{TRAVEL_FEED}"]


class TestValidateReach:
    """可動域検証の契約（送信前に全点を弾く）."""

    def test_reachable_position_returns_none(self, stage: XYZStage):
        assert validate_reach(stage, _clean()) is None

    def test_reports_xy_outside_limits(self, stage: XYZStage):
        error = validate_reach(stage, _clean(x=999.0))

        assert error is not None
        assert "999" in error

    def test_reports_press_z_below_limit(self, stage: XYZStage):
        """面 Z は届くが押し込むと下限を割る設定を弾く."""
        error = validate_reach(stage, _clean(z=-4.8, press_depth=0.5))

        assert error is not None

    def test_reports_wipe_point_outside_limits(self, stage: XYZStage):
        """中心は届くがこすりの振れ幅で域外へ出る設定を弾く."""
        error = validate_reach(stage, _clean(x=299.0, stroke=2.0))

        assert error is not None


class TestCleanPositionLabel:
    """表示文字列はサーバー側で組む（JS に整形させない）."""

    def test_label_shows_position_and_press_depth(self):
        label = clean_position_label(_clean())

        assert "68.00" in label
        assert "53.00" in label


class TestCleanNozzle:
    """パージ → こすり → 退避の手順契約."""

    def test_sends_approach_purge_wipe_and_depart_in_order(
        self, klipper: FakeKlipper, stage: XYZStage, applicator: PasteApplicator
    ):
        clean_nozzle(klipper, stage, applicator, _clean())

        lines = klipper.sent_lines
        purge_index = next(i for i, l in enumerate(lines) if _MOVE_RE.match(l))
        press_index = next(
            i for i, l in enumerate(lines) if l.startswith(f"G1 Z-3.5 F{TRAVEL_FEED}")
        )
        assert lines[0] == "G90"
        assert purge_index < press_index
        assert lines[-2] == f"G1 Z{TRAVEL_Z} F{TRAVEL_FEED}"
        assert lines[-1] == "M400"

    def test_purges_the_configured_amount(
        self, klipper: FakeKlipper, stage: XYZStage, applicator: PasteApplicator
    ):
        clean_nozzle(klipper, stage, applicator, _clean(purge_ul=0.2))

        assert _stepper_amounts_ul(klipper) == pytest.approx([0.2])

    def test_never_retracts(
        self, klipper: FakeKlipper, stage: XYZStage, applicator: PasteApplicator
    ):
        """引き戻しは呼び出し側の retract() が担う（二重に引かない）."""
        clean_nozzle(klipper, stage, applicator, _clean())

        assert all(amount > 0 for amount in _stepper_amounts_ul(klipper))

    def test_zero_purge_skips_extrusion(
        self, klipper: FakeKlipper, stage: XYZStage, applicator: PasteApplicator
    ):
        clean_nozzle(klipper, stage, applicator, _clean(purge_ul=0.0))

        assert _stepper_amounts_ul(klipper) == []

    def test_zero_stroke_skips_wipe_moves(
        self, klipper: FakeKlipper, stage: XYZStage, applicator: PasteApplicator
    ):
        clean_nozzle(klipper, stage, applicator, _clean(stroke=0.0))

        wipe_feed = f"F{10.0 * 60}"
        assert [line for line in klipper.sent_lines if wipe_feed in line] == [
            f"G1 X68.0 Y53.0 Z-3.5 {wipe_feed}"
        ]

    def test_ends_at_travel_z(
        self, klipper: FakeKlipper, stage: XYZStage, applicator: PasteApplicator
    ):
        """押し込み Z のまま次工程へ渡さない."""
        clean_nozzle(klipper, stage, applicator, _clean())

        assert klipper.g1_moves()[-1]["z"] == TRAVEL_Z

    def test_out_of_limits_setting_sends_nothing(
        self, klipper: FakeKlipper, stage: XYZStage, applicator: PasteApplicator
    ):
        """パージ済みで落ちるとシリコン上に塊が残るので、送信前に弾く."""
        with pytest.raises(ValueError):
            clean_nozzle(klipper, stage, applicator, _clean(x=999.0))

        assert klipper.sent == ()

    def test_logs_one_line_with_purge_amount_and_passes(
        self, klipper: FakeKlipper, stage: XYZStage, applicator: PasteApplicator
    ):
        messages: list[str] = []

        clean_nozzle(klipper, stage, applicator, _clean(), log=messages.append)

        assert len(messages) == 1
        assert "0.2" in messages[0]
