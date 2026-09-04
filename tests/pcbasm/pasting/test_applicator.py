"""PasteApplicator のテスト.

自前 HAL の fake は ``FakeKlipper`` だけで、``XYZStage`` / ``PasteDispenser`` は実物を
組み合わせる（testing-strategy「fake は自前 HAL 表面 1 層だけ」）。検証は送信された
G-code（``MANUAL_STEPPER ... MOVE=`` の距離を rotation_distance と rotations_per_ul で
μL に戻す、``G1`` の座標を読む）に対して行う。

``FillSequence.to_gcode`` は塗布吐出を ``SET_POSITION=0`` 直後の ``SYNC=0`` 付き
``MOVE`` として発行し、その量は ``retract_amount + extra_amount + total_amount_ul``。
``extra_amount`` は ``実効レート * prime_extra_delay``（既定 0）なので、塗布 MOVE の量から
各成分の ``total_amount_ul`` を復元して検証する。
"""

from __future__ import annotations

import math
import re

import attrs
import pytest
from shapely import Polygon, box

from pcbasm.config import PasteDispenser as PasteDispenserConfig, Toolhead
from pcbasm.geometry import Compose, HeightPlane, Identity, Point2d, Point3d, Shift
from pcbasm.hal import PasteDispenser, XYZStage
from pcbasm.pasting.applicator import PasteApplicator, build_applicator
from pcbasm.pasting.params import PasteParamsPatch
from tests.helpers import FAKE_PRINTER_CONFIG, FakeKlipper

ROTATIONS_PER_UL = 45.0
ROTATION_DISTANCE = float(
    FAKE_PRINTER_CONFIG["manual_stepper paste_dispenser"]["rotation_distance"]
)
RETRACT_AMOUNT = 10.0

# 既定ノズル径 0.34（inset=0.17）で 2 成分に分裂する細首ダンベル（凹形）。
# くびれ幅 0.3 < 2*0.17 のため buffer(-0.17) が左右 2 ローブに割れる。
_DUMBBELL_NECK_03 = Polygon(
    [
        (0, 0),
        (4, 0),
        (4, 1.85),
        (6, 1.85),
        (6, 0),
        (10, 0),
        (10, 4),
        (6, 4),
        (6, 2.15),
        (4, 2.15),
        (4, 4),
        (0, 4),
    ]
)

_MOVE_RE = re.compile(r"MANUAL_STEPPER STEPPER=paste_dispenser MOVE=(\S+)(.*)")
_G1_RE = re.compile(r"G1 (.*)")


def _machine_surface_z(x: float, y: float):
    return 0.2 + 0.01 * x - 0.005 * y + 0.0002 * x**2 + 0.0001 * y**2 - 0.00015 * x * y


def _machine_height_plane():
    points = [
        (100.0, 30.0),
        (145.0, 32.0),
        (104.0, 70.0),
        (142.0, 68.0),
        (120.0, 45.0),
        (133.0, 58.0),
    ]
    return HeightPlane(
        tuple(Point3d(x, y, _machine_surface_z(x, y)) for x, y in points)
    )


def _config(**overrides: object) -> PasteDispenserConfig:
    """テスト既定の PasteDispenser 設定（実 ``Machine.paste_dispenser`` と同型）."""
    config = PasteDispenserConfig(
        rotations_per_ul=ROTATIONS_PER_UL,
        nozzle_diameter=0.34,
        max_fill_speed=2.0,
        max_dispense_rate=5.0,
        dispense_accel=10.0,
        retract_amount=RETRACT_AMOUNT,
        retract_rate=10.0,
        retract_accel_factor=2.0,
        toolhead=Toolhead(x=0.0, y=0.0),
        paste_height=0.5,
        lift_height=5.0,
        ul_per_mm2=0.05,
        dispense_mode="area",
        prime_extra_delay=0.0,
    )
    return attrs.evolve(config, **overrides)


def _applicator(klipper: FakeKlipper, **overrides: object) -> PasteApplicator:
    stage = XYZStage(klipper.readonly)
    return build_applicator(klipper, stage, _config(**overrides))


@pytest.fixture
def klipper() -> FakeKlipper:
    return FakeKlipper()


@pytest.fixture
def applicator(klipper: FakeKlipper) -> PasteApplicator:
    return _applicator(klipper)


def _stepper_moves(klipper: FakeKlipper) -> list[tuple[float, str]]:
    """``MANUAL_STEPPER ...

    MOVE=`` を (距離 [mm], 残り引数) の列で返す.
    """
    moves: list[tuple[float, str]] = []
    for line in klipper.sent_lines:
        match = _MOVE_RE.match(line)
        if match:
            moves.append((float(match.group(1)), match.group(2)))
    return moves


def _dispense_amounts_ul(klipper: FakeKlipper) -> list[float]:
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
            distance_mm = float(match.group(1))
            amounts.append(distance_mm / ROTATION_DISTANCE / ROTATIONS_PER_UL)
    return amounts


def _g1_moves(klipper: FakeKlipper) -> list[dict[str, float]]:
    """``G1`` の座標を {軸: 値} の列で返す（F は feed）."""
    moves: list[dict[str, float]] = []
    for line in klipper.sent_lines:
        match = _G1_RE.match(line)
        if match:
            moves.append(
                {part[0].lower(): float(part[1:]) for part in match.group(1).split()}
            )
    return moves


def _down_z(klipper: FakeKlipper) -> float:
    """先頭点上空 → 下降 の 2 番目の G1 の Z."""
    return _g1_moves(klipper)[1]["z"]


class TestSingleComponentPad:
    """単一成分パッド: FillSequence 1 本・total_amount = area*ul_per_mm2."""

    def test_single_send_gcode_per_pad(self, applicator, klipper):
        applicator.apply(
            box(0, 0, 5, 4), params=applicator.default_params, transform=Identity()
        )

        assert len(klipper.sent) == 1

    def test_total_amount_is_area_based(self, applicator, klipper):
        polygon = box(0, 0, 5, 4)  # area = 20 mm^2

        applicator.apply(
            polygon, params=applicator.default_params, transform=Identity()
        )

        amounts = _dispense_amounts_ul(klipper)
        assert len(amounts) == 1
        assert amounts[0] == pytest.approx(RETRACT_AMOUNT + polygon.area * 0.05)


class TestMultiComponentPad:
    """複数成分パッド（凹形）: FillSequence N 本・各 total_amount = area*ul/N."""

    def test_send_gcode_once_per_component(self, applicator, klipper):
        applicator.apply(
            _DUMBBELL_NECK_03, params=applicator.default_params, transform=Identity()
        )

        assert len(klipper.sent) == 2

    def test_total_amount_split_evenly_and_sums_to_area_based(
        self, applicator, klipper
    ):
        polygon = _DUMBBELL_NECK_03

        result = applicator.apply(
            polygon, params=applicator.default_params, transform=Identity()
        )

        amounts = _dispense_amounts_ul(klipper)
        assert len(amounts) == 2
        for amount in amounts:
            assert amount == pytest.approx(RETRACT_AMOUNT + polygon.area * 0.05 / 2)
        assert result.summary.commanded_volume_ul == pytest.approx(polygon.area * 0.05)


class TestEmptyFallback:
    """空 / 不正ポリゴン → 経路が空 → warning ＆ skip（送信 0 回）."""

    @pytest.mark.parametrize(
        "polygon", [Polygon(), Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])]
    )
    def test_empty_or_invalid_polygon_skips(self, applicator, klipper, polygon):
        result = applicator.apply(
            polygon, params=applicator.default_params, transform=Identity()
        )

        assert klipper.sent == ()
        assert result.sequences == ()
        assert result.summary.applied_mode is None


class TestDispenseProtocol:
    """吐出プロトコル（プライム+吐出の連続動作・sync・コンテキスト）."""

    def test_dispense_then_retract_are_async_and_synced_at_end(
        self, applicator, klipper
    ):
        applicator.apply(
            box(0, 0, 2, 3), params=applicator.default_params, transform=Identity()
        )

        moves = _stepper_moves(klipper)
        assert len(moves) == 2  # prime+吐出 / リトラクション
        assert all("SYNC=0" in args for _, args in moves)
        lines = klipper.sent_lines
        assert "MANUAL_STEPPER STEPPER=paste_dispenser SYNC=1" in lines
        assert lines[-1] == "M400"

    def test_context_manager_enables_and_disables(self, applicator, klipper):
        with applicator:
            pass

        assert klipper.sent_lines == [
            "SET_PIN PIN=air_pump VALUE=1",
            "MANUAL_STEPPER STEPPER=paste_dispenser ENABLE=1",
            "SET_PIN PIN=air_pump VALUE=0",
            "MANUAL_STEPPER STEPPER=paste_dispenser ENABLE=0",
        ]


class TestLoading:
    """Load / load_rotations / retract."""

    def test_load_pushes_requested_volume(self, applicator, klipper):
        applicator.load(2.0)

        ((distance, args),) = _stepper_moves(klipper)
        assert distance / ROTATION_DISTANCE / ROTATIONS_PER_UL == pytest.approx(2.0)
        assert "SYNC=0" not in args
        assert klipper.sent_lines[-1] == "M400"

    def test_retract_pulls_back_retract_amount(self, applicator, klipper):
        applicator.retract()

        ((distance, _),) = _stepper_moves(klipper)
        assert distance / ROTATION_DISTANCE / ROTATIONS_PER_UL == pytest.approx(
            -RETRACT_AMOUNT
        )

    def test_load_rotations_uses_raw_revolutions(self, applicator, klipper):
        applicator.load_rotations(5.0, 0.5, 0.5)

        ((distance, args),) = _stepper_moves(klipper)
        assert distance == pytest.approx(5.0 * ROTATION_DISTANCE)
        assert "SPEED=0.5" in args
        assert klipper.sent_lines[-1] == "M400"

    def test_load_rotations_retracts_after_extrude_only(self, applicator, klipper):
        applicator.load_rotations(5.0, 0.5, 0.5, retract_rotations=1.0)
        applicator.load_rotations(-2.0, 0.5, 0.5, retract_rotations=1.0)

        distances = [d for d, _ in _stepper_moves(klipper)]
        assert distances == pytest.approx([5.0, -1.0, -2.0])


class TestTransformApplication:
    """塗布座標変換: board_transform → toolhead_offset → height_plane."""

    def test_height_plane_evaluates_final_toolhead_machine_xy(
        self, applicator, klipper
    ):
        paste_height = 0.5
        board_transform = Shift(x=20.0, y=10.0, z=0.0)
        toolhead_offset = Shift(x=100.0, y=30.0, z=0.0)
        before_height_plane = Compose([board_transform, toolhead_offset])
        transform = Compose([board_transform, toolhead_offset, _machine_height_plane()])

        applicator.apply(
            box(0.0, 0.0, 5.0, 4.0),
            params=applicator.default_params,
            transform=transform,
        )

        down = _g1_moves(klipper)[1]
        assert down["z"] == pytest.approx(
            paste_height + _machine_surface_z(down["x"], down["y"])
        )
        board_point = before_height_plane.inverse().apply(
            Point3d(down["x"], down["y"], 0.0)
        )
        assert down["z"] != pytest.approx(
            paste_height + _machine_surface_z(board_point.x, board_point.y)
        )


class TestAutoPasteHeight:
    """paste_height=auto は dispense_mode によらず ul_per_mm2（膜厚 [mm]）を高さに使う."""

    @pytest.mark.parametrize(
        ("dispense_mode", "polygon"),
        [
            ("area", box(0, 0, 5, 4)),
            ("line", box(0, 0, 1, 4)),
            ("dot", box(0, 0, 1, 1)),
        ],
    )
    def test_auto_uses_ul_per_mm2_as_height(self, klipper, dispense_mode, polygon):
        applicator = _applicator(
            klipper,
            nozzle_diameter=0.5,
            paste_height="auto",
            ul_per_mm2=0.08,
            dispense_mode=dispense_mode,
        )

        applicator.apply(
            polygon, params=applicator.default_params, transform=Identity()
        )

        assert _down_z(klipper) == pytest.approx(0.08)


class TestBuildApplicator:
    """build_applicator は machine 設定から HAL ごと組み立てる."""

    def test_uses_config_rotations_per_ul_by_default(self, klipper):
        applicator = _applicator(klipper)

        assert applicator.rotations_per_ul == pytest.approx(ROTATIONS_PER_UL)

    def test_rotations_per_ul_override_changes_dispensed_distance(self, klipper):
        applicator = build_applicator(
            klipper, XYZStage(klipper.readonly), _config(), rotations_per_ul=90.0
        )

        applicator.load(1.0)

        ((distance, _),) = _stepper_moves(klipper)
        assert distance == pytest.approx(90.0 * ROTATION_DISTANCE)

    def test_lift_height_override_changes_approach_and_retreat_z(self, klipper):
        applicator = build_applicator(
            klipper, XYZStage(klipper.readonly), _config(), lift_height=4.0
        )

        applicator.deposit_at(Point2d(1.0, 2.0), amount_ul=0.1, transform=Identity())

        assert [move["z"] for move in _g1_moves(klipper)] == pytest.approx(
            [4.5, 0.5, 4.5]
        )

    def test_default_params_mirror_config(self, klipper):
        applicator = _applicator(klipper, ul_per_mm2=0.08, dispense_mode="line")

        assert applicator.default_params.ul_per_mm2 == pytest.approx(0.08)
        assert applicator.default_params.dispense_mode == "line"


class TestDrawLine:
    """公開 draw_line（キャリブ用の 1 本線塗布プリミティブ）."""

    def test_sends_single_blocking_gcode(self, applicator, klipper):
        applicator.draw_line(
            Point2d(0.0, 0.0), Point2d(10.0, 0.0), amount_ul=2.0, transform=Identity()
        )

        assert len(klipper.sent) == 1
        assert klipper.sent_lines[-1] == "M400"

    def test_dispense_amount_is_retraction_plus_amount(self, applicator, klipper):
        applicator.draw_line(
            Point2d(0.0, 0.0), Point2d(10.0, 0.0), amount_ul=2.0, transform=Identity()
        )

        assert _dispense_amounts_ul(klipper) == pytest.approx([RETRACT_AMOUNT + 2.0])

    def test_returns_execution_with_effective_fill_speed(self, applicator):
        # L=10, amount=2.0, max_fill_speed=2.0, max_dispense_rate=5.0:
        # r_desired = 2*2/10 = 0.4 ≤ 5 → 非 cap。速度 = max_fill_speed = 2.0。
        execution = applicator.draw_line(
            Point2d(0.0, 0.0), Point2d(10.0, 0.0), amount_ul=2.0, transform=Identity()
        )

        assert execution.applied_mode == "line"
        assert execution.path_length_mm == pytest.approx(10.0)
        assert execution.fill_speed is not None
        assert execution.fill_speed.resolve(100.0) == pytest.approx(2.0)

    def test_transform_is_applied_to_the_line(self, applicator, klipper):
        applicator.draw_line(
            Point2d(0.0, 0.0),
            Point2d(10.0, 0.0),
            amount_ul=2.0,
            transform=Shift(x=100.0, y=50.0, z=0.0),
        )

        first = _g1_moves(klipper)[0]
        assert (first["x"], first["y"]) == (pytest.approx(100.0), pytest.approx(50.0))

    def test_rate_cap_inf_disables_capping(self, klipper):
        # max_dispense_rate=0.1, max_fill_speed=2.0, L=10, amount=2.0:
        # r_desired = 0.4。cap=None なら 0.1 に頭打ち→減速、cap=inf なら 0.4 で速度維持。
        applicator = _applicator(klipper, max_dispense_rate=0.1)

        execution = applicator.draw_line(
            Point2d(0.0, 0.0),
            Point2d(10.0, 0.0),
            amount_ul=2.0,
            transform=Identity(),
            rate_cap=math.inf,
        )

        assert execution.fill_speed is not None
        assert execution.fill_speed.resolve(100.0) == pytest.approx(2.0)
        assert _dispense_amounts_ul(klipper) == pytest.approx([RETRACT_AMOUNT + 2.0])

    def test_fill_speed_override_exceeds_config_default(self, applicator):
        # ③ 速度スイープの肝: per-line の fill_speed 上書きで既定（2.0）を超える速度を出す。
        execution = applicator.draw_line(
            Point2d(0.0, 0.0),
            Point2d(10.0, 0.0),
            amount_ul=2.0,
            transform=Identity(),
            fill_speed=8.0,
            rate_cap=math.inf,
        )

        assert execution.fill_speed is not None
        assert execution.fill_speed.resolve(100.0) == pytest.approx(8.0)

    def test_auto_height_uses_ul_per_mm2(self, klipper):
        applicator = _applicator(klipper, nozzle_diameter=0.5, paste_height="auto")

        applicator.draw_line(
            Point2d(0.0, 0.0), Point2d(10.0, 0.0), amount_ul=1.5, transform=Identity()
        )

        assert _down_z(klipper) == pytest.approx(0.05)


class TestDepositAt:
    """公開 deposit_at（初回パージ用の単点塗布プリミティブ）."""

    def test_uses_single_point_without_stage_path_motion(self, applicator, klipper):
        point = Point2d(4.0, 5.0)

        applicator.deposit_at(point, amount_ul=0.2, transform=Identity())

        moves = _g1_moves(klipper)
        assert len(moves) == 3  # 上空 → 下降 → 上昇（塗布移動なし）
        assert all(
            (m["x"], m["y"]) == (pytest.approx(point.x), pytest.approx(point.y))
            for m in moves
        )
        assert moves[1]["z"] == pytest.approx(0.5)
        assert klipper.sent_lines[-1] == "M400"

    def test_params_override_paste_height(self, applicator, klipper):
        params = applicator.default_params.patched(PasteParamsPatch(paste_height=0.6))

        applicator.deposit_at(
            Point2d(4.0, 5.0), amount_ul=0.2, transform=Identity(), params=params
        )

        assert _down_z(klipper) == pytest.approx(0.6)

    def test_returns_dot_execution_without_retraction_rotations(
        self, applicator, klipper
    ):
        result = applicator.deposit_at(
            Point2d(4.0, 5.0), amount_ul=0.2, transform=Identity()
        )

        (execution,) = result.sequences
        assert execution.applied_mode == "dot"
        assert execution.path_length_mm == 0.0
        assert execution.commanded_volume_ul == pytest.approx(0.2)
        assert execution.prime_extra_volume_ul == 0.0
        assert execution.effective_rate_ul_s == pytest.approx(5.0)
        assert execution.rotations == pytest.approx(0.2 * ROTATIONS_PER_UL)
        assert _dispense_amounts_ul(klipper) == pytest.approx([RETRACT_AMOUNT + 0.2])

    def test_rotations_include_prime_extra_but_exclude_retraction(self, klipper):
        applicator = _applicator(klipper, prime_extra_delay=0.5)

        result = applicator.deposit_at(
            Point2d(4.0, 5.0), amount_ul=0.2, transform=Identity()
        )

        (execution,) = result.sequences
        assert execution.prime_extra_volume_ul == pytest.approx(5.0 * 0.5)
        assert execution.rotations == pytest.approx(
            (0.2 + 5.0 * 0.5) * ROTATIONS_PER_UL
        )
        # 実 G-code のプライム押し戻し 10 uL は回転数 metadata には含まれない。
        assert _dispense_amounts_ul(klipper) == pytest.approx([12.7])


class TestApplyResult:
    """Apply の pad 単位集計結果."""

    def test_summary_aggregates_every_sequence(self, applicator):
        result = applicator.apply(
            box(0.0, 0.0, 5.0, 4.0),
            params=applicator.default_params,
            transform=Identity(),
        )

        summary = result.summary
        assert summary.applied_mode == "area"
        assert summary.commanded_volume_ul == pytest.approx(20.0 * 0.05)
        assert summary.prime_extra_volume_ul == 0.0
        assert summary.rotations == pytest.approx(20.0 * 0.05 * ROTATIONS_PER_UL)
        assert summary.path_length_mm > 0.0

    def test_summary_of_mixed_modes_is_mixed(self, applicator):
        result = applicator.apply(
            box(0.0, 0.0, 5.0, 4.0),
            params=applicator.default_params,
            transform=Identity(),
        )
        dot = applicator.deposit_at(
            Point2d(0.0, 0.0), amount_ul=0.1, transform=Identity()
        )

        combined = attrs.evolve(result, sequences=(*result.sequences, *dot.sequences))
        assert combined.summary.applied_mode == "mixed"


class TestPerPadParams:
    """Apply() の params が塗布量・FillSequence・経路生成へ伝わる."""

    def test_ul_per_mm2_changes_total(self, applicator, klipper):
        params = applicator.default_params.patched(PasteParamsPatch(ul_per_mm2=0.08))

        applicator.apply(box(0, 0, 5, 4), params=params, transform=Identity())

        assert _dispense_amounts_ul(klipper) == pytest.approx(
            [RETRACT_AMOUNT + 20 * 0.08]
        )

    def test_prime_extra_delay_adds_extra_amount(self, applicator, klipper):
        polygon = box(0, 0, 5, 4)
        applicator.apply(
            polygon, params=applicator.default_params, transform=Identity()
        )
        base_amount = _dispense_amounts_ul(klipper)[0]
        klipper.clear_sent()

        delayed = applicator.default_params.patched(
            PasteParamsPatch(prime_extra_delay=1.0)
        )
        applicator.apply(polygon, params=delayed, transform=Identity())

        assert _dispense_amounts_ul(klipper)[0] > base_amount

    def test_boundary_margin_changes_fill_path(self, applicator, klipper):
        polygon = box(0, 0, 10, 10)
        applicator.apply(
            polygon, params=applicator.default_params, transform=Identity()
        )
        without_margin = klipper.sent
        klipper.clear_sent()

        margin = applicator.default_params.patched(
            PasteParamsPatch(boundary_margin=2.0)
        )
        applicator.apply(polygon, params=margin, transform=Identity())

        assert klipper.sent != without_margin

    @pytest.mark.parametrize(
        ("line_direction", "starts_near_component"),
        [("outward", True), ("inward", False)],
    )
    def test_line_direction_changes_observed_stage_path(
        self, applicator, klipper, line_direction, starts_near_component
    ):
        reference = Point2d(0.5, -5.0)
        params = applicator.default_params.patched(
            PasteParamsPatch(dispense_mode="line", line_direction=line_direction)
        )

        applicator.apply(
            box(0.0, 0.0, 1.0, 4.0),
            params=params,
            transform=Identity(),
            line_reference=reference,
        )

        # 上空 → 下降 → 塗布移動（2 点）→ 上昇 の順。塗布経路は 3 番目と 4 番目の G1。
        moves = _g1_moves(klipper)
        start = Point2d(moves[2]["x"], moves[2]["y"])
        end = Point2d(moves[3]["x"], moves[3]["y"])
        assert (
            (start - reference).norm < (end - reference).norm
        ) is starts_near_component
