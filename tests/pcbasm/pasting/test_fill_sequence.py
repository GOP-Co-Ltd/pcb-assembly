"""FillSequence のテスト.

FillSequence は1ポリゴンの塗布動作
（接近→下降→prime同期吐出→速度0→リトラクト・上昇同時開始→同期）を1本のGCodeに組む
オーケストレーター。ここでは stage / dispenser（いずれも pcbasm 自前の HAL ABC）を
mock し、発行される動作の順序・量・速度を call assertion で検証する。個々の GCode 文字列は
stage/dispenser 側の責務なので（mock は空 GCode を返す）、 本テストは FillSequence が両 HAL
をどう駆動するかの契約のみを固定する。

速度モデル: 移動速度 ``max_fill_speed`` を主設定とし、吐出レートはこれに追従して導出する
（``r = total_amount * max_fill_speed / path長``）。導出レートが頭打ち値（``rate_cap``、
既定は ``max_dispense_rate``）を超える場合はレートを上限で頭打ちし、移動速度を
``cap * 長 / total`` に下げて ``motion_time == dispense_time`` を保つ。
"""

import math

import pytest
from pytest_mock import MockerFixture

from pcbasm.gcode import GCode
from pcbasm.geometry import Path, Point3d
from pcbasm.hal import Speed
from pcbasm.pasting.fill_sequence import FillSequence
from pcbasm.pasting.flowcalib.flow import rate_sweep_amount_ul
from pcbasm.pasting.params import DispenseSettings

# retract_accel = factor * rate^2 / amount = 4.0 * 25 / 10 = 10.0
_SETTINGS = DispenseSettings(
    max_fill_speed=2.0,
    max_dispense_rate=10.0,
    dispense_accel=8.0,
    retract_amount=10.0,
    retract_rate=5.0,
    retract_accel_factor=4.0,
    lift_height=3.0,
)
_TRAVEL = Speed.rate(1.0)


@pytest.fixture
def mock_stage(mocker: MockerFixture):
    stage = mocker.Mock()
    stage.max_velocity = 100.0
    stage.move.return_value = GCode()
    stage.to_gcode.return_value = GCode()
    return stage


@pytest.fixture
def mock_dispenser(mocker: MockerFixture):
    dispenser = mocker.Mock()
    dispenser.pushpull.return_value = GCode()
    dispenser.continue_pushpull.return_value = GCode()
    dispenser.sync.return_value = GCode()
    return dispenser


def _sequence(path: Path, *, rate_cap: float | None = None) -> FillSequence:
    """具体的な数値で構成した FillSequence を返す（テスト間で共有）.

    total_amount=20, max_fill_speed=2.0, max_dispense_rate=10.0。
    - 経路長 L=10（cap 非バインド）: r_desired = 20*2/10 = 4 ≤ 10 → rate=4,
      dispense_time = 20/4 = 5, 速度 = 10/5 = 2.0 = max_fill_speed。
    - 経路長 L=2（cap バインド）: r_desired = 20*2/2 = 20 > 10 → rate=10,
      dispense_time = 20/10 = 2, 速度 = 2/2 = 1.0 = cap*L/total。
    prime_extra_delay=0.5 のとき extra = 実効レート * 0.5。
    ``rate_cap`` 未指定（None）のとき頭打ち値は max_dispense_rate（現行等価）。
    """
    return FillSequence(
        path=path,
        total_amount_ul=20.0,
        settings=_SETTINGS,
        prime_extra_delay=0.5,
        rate_cap=rate_cap,
    )


def _dispense_rate(mock_dispenser) -> float:
    """塗布吐出（sync=False の pushpull）に渡された実効レートを返す."""
    for call in mock_dispenser.pushpull.call_args_list:
        if call.kwargs.get("sync") is False:
            return call.args[1]
    raise AssertionError("塗布吐出 pushpull が発行されていない")


class TestFillSequence:
    """FillSequence クラスのテスト."""

    def test_fill_speed_resolves_to_max_fill_speed_when_rate_not_capped(self):
        # L=10: r_desired=4 ≤ max=10 → 減速なし。速度 = max_fill_speed = 2.0。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)])

        speed = _sequence(path).actual_fill_speed()

        assert speed is not None
        # 絶対速度なので max_velocity に依らず 2.0 に解決される。
        assert speed.resolve(100.0) == pytest.approx(2.0)

    def test_fill_speed_slows_down_when_rate_capped(self):
        # L=2: r_desired=20 > max=10 → rate を 10 で頭打ち、速度 = max*L/total = 1.0。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(2.0, 0.0, 5.0)])

        speed = _sequence(path).actual_fill_speed()

        assert speed is not None
        assert speed.resolve(100.0) == pytest.approx(1.0)

    def test_fill_speed_is_none_for_zero_length_path(self):
        # 単点 path は length=0 のため吐出移動が成立せず None。
        path = Path([Point3d(0.0, 0.0, 5.0)])

        assert _sequence(path).actual_fill_speed() is None

    def test_dispense_rate_follows_speed_when_not_capped(
        self, mock_stage, mock_dispenser
    ):
        # L=10（非 cap）: 実効レート = total*max_fill_speed/L = 20*2/10 = 4.0。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)])

        _sequence(path).to_gcode(mock_stage, mock_dispenser)

        assert _dispense_rate(mock_dispenser) == pytest.approx(4.0)

    def test_dispense_rate_is_capped_at_max(self, mock_stage, mock_dispenser):
        # L=2（cap）: 実効レート = max_dispense_rate = 10.0。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(2.0, 0.0, 5.0)])

        _sequence(path).to_gcode(mock_stage, mock_dispenser)

        assert _dispense_rate(mock_dispenser) == pytest.approx(10.0)

    def test_dot_fill_dispenses_at_max_rate(self, mock_stage, mock_dispenser):
        # 点フィル（L=0）: 速度概念がないので上限レートでその場吐出。
        path = Path([Point3d(0.0, 0.0, 5.0)])

        _sequence(path).to_gcode(mock_stage, mock_dispenser)

        assert _dispense_rate(mock_dispenser) == pytest.approx(10.0)

    def test_to_gcode_queues_dispense_then_continuous_retraction(
        self, mock_stage, mock_dispenser
    ):
        # 吐出後の座標を維持したまま、リトラクションを非同期 queue する。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)])

        _sequence(path).to_gcode(mock_stage, mock_dispenser)

        mock_dispenser.pushpull.assert_called_once()
        dispense = mock_dispenser.pushpull.call_args
        # prime+吐出を1つの連続動作として非同期開始（sync=False）。
        # 量 = retraction + extra_amount + total_amount。
        # L=10 で実効レート=4.0、extra = 4.0 * 0.5 = 2.0 → 10 + 2 + 20 = 32。
        assert dispense.args[0] == pytest.approx(32.0)
        assert dispense.kwargs.get("sync") is False
        mock_dispenser.continue_pushpull.assert_called_once_with(
            32.0, -10.0, 5.0, 10.0, sync=False
        )
        mock_dispenser.sync.assert_called_once_with()

    def test_to_gcode_starts_retraction_and_ascent_after_zero_velocity_boundary(
        self, mock_stage, mock_dispenser
    ):
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)])
        mock_stage.move.side_effect = [
            GCode("TRAVEL"),
            GCode("DESCEND"),
            GCode("ASCEND"),
        ]
        mock_stage.to_gcode.return_value = GCode("FILL")
        mock_dispenser.pushpull.return_value = GCode("DISPENSE")
        mock_dispenser.continue_pushpull.return_value = GCode("RETRACT")
        mock_dispenser.sync.return_value = GCode("SYNC_DISPENSER")

        commands = _sequence(path).to_gcode(mock_stage, mock_dispenser).to_list()

        fill = commands.index("FILL")
        stop = commands.index("G4 P0")
        retract = commands.index("RETRACT")
        ascent = commands.index("ASCEND")
        sync = commands.index("SYNC_DISPENSER")
        assert fill < stop < retract < ascent < sync
        assert "M400" not in commands[fill + 1 : retract]
        assert commands.count("M400") == 1

    def test_to_gcode_descends_then_ascends_with_explicit_coords(
        self, mock_stage, mock_dispenser
    ):
        # 接近上空(z=first.z+lift) → 下降(z=first.z)、最後に上昇(z=last.z+lift)。
        # first=(0,0,5), last=(10,0,5), lift_height=3。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)])

        _sequence(path).to_gcode(mock_stage, mock_dispenser)

        moves = mock_stage.move.call_args_list
        assert len(moves) == 3
        # 1. 最初の点の上空へ移動。
        assert moves[0].kwargs == {
            "x": 0.0,
            "y": 0.0,
            "z": 8.0,
            "speed": _TRAVEL,
        }
        # 2. 塗布高さへ下降。
        assert moves[1].kwargs == {
            "x": 0.0,
            "y": 0.0,
            "z": 5.0,
            "speed": _TRAVEL,
        }
        # 3. 最後の点で上昇。
        assert moves[2].kwargs == {
            "x": 10.0,
            "y": 0.0,
            "z": 8.0,
            "speed": _TRAVEL,
        }

    def test_to_gcode_fills_along_path_with_fill_speed(
        self, mock_stage, mock_dispenser
    ):
        # 塗布速度が成立するとき、塗布移動を path 全体に対し1回発行する。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)])

        _sequence(path).to_gcode(mock_stage, mock_dispenser)

        mock_stage.to_gcode.assert_called_once()
        call = mock_stage.to_gcode.call_args
        # path 全体を渡す（位置引数 or path= キーワードのどちらでも許容）。
        passed_path = call.kwargs.get("path", call.args[0] if call.args else None)
        assert passed_path == path
        # 速度は fill_speed_actual（非 cap なので max_fill_speed = 2.0 mm/s）。
        passed_speed = call.kwargs["speed"]
        assert passed_speed.resolve(100.0) == pytest.approx(2.0)

    def test_to_gcode_skips_path_fill_for_zero_length_path(
        self, mock_stage, mock_dispenser
    ):
        # 単点 path は fill_speed_actual=None なので塗布移動（stage.to_gcode）を発行しない。
        path = Path([Point3d(0.0, 0.0, 5.0)])

        _sequence(path).to_gcode(mock_stage, mock_dispenser)

        mock_stage.to_gcode.assert_not_called()
        # それでも吐出と連続リトラクションは行われる。
        mock_dispenser.pushpull.assert_called_once()
        mock_dispenser.continue_pushpull.assert_called_once()

    def test_to_gcode_returns_gcode(self, mock_stage, mock_dispenser):
        # 単一の連結 GCode を返す（送信側はこれを1回で送る）。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)])

        result = _sequence(path).to_gcode(mock_stage, mock_dispenser)

        assert isinstance(result, GCode)


class TestRateCap:
    """``rate_cap`` による吐出レート頭打ち値の切替テスト."""

    def test_none_caps_at_max_dispense_rate(self, mock_stage, mock_dispenser):
        # rate_cap=None は max_dispense_rate で頭打ち（現行等価）。
        # L=2: r_desired=20 > cap=max=10 → rate=10、速度 = 2/2 = 1.0。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(2.0, 0.0, 5.0)])

        seq = _sequence(path, rate_cap=None)
        seq.to_gcode(mock_stage, mock_dispenser)

        assert _dispense_rate(mock_dispenser) == pytest.approx(10.0)
        speed = seq.actual_fill_speed()
        assert speed is not None
        assert speed.resolve(100.0) == pytest.approx(1.0)

    def test_explicit_cap_overrides_max_dispense_rate(self, mock_stage, mock_dispenser):
        # rate_cap=5.0（< max_dispense_rate=10）で頭打ち値を下げる。
        # L=2: r_desired=20 > cap=5 → rate=5、dispense_time=20/5=4、速度=2/4=0.5。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(2.0, 0.0, 5.0)])

        seq = _sequence(path, rate_cap=5.0)
        seq.to_gcode(mock_stage, mock_dispenser)

        assert _dispense_rate(mock_dispenser) == pytest.approx(5.0)
        speed = seq.actual_fill_speed()
        assert speed is not None
        assert speed.resolve(100.0) == pytest.approx(0.5)

    def test_inf_cap_disables_capping(self, mock_stage, mock_dispenser):
        # rate_cap=inf は cap 無効。L=2: r_desired=20 をそのまま採用 →
        # dispense_time=20/20=1、速度=2/1=2.0=max_fill_speed（減速なし）。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(2.0, 0.0, 5.0)])

        seq = _sequence(path, rate_cap=math.inf)
        seq.to_gcode(mock_stage, mock_dispenser)

        assert _dispense_rate(mock_dispenser) == pytest.approx(20.0)
        speed = seq.actual_fill_speed()
        assert speed is not None
        assert speed.resolve(100.0) == pytest.approx(2.0)

    def test_explicit_cap_applies_to_point_fill(self, mock_stage, mock_dispenser):
        # 点フィル（L=0）でも頭打ち値が反映される。rate_cap=5 → その場吐出レート=5。
        path = Path([Point3d(0.0, 0.0, 5.0)])

        _sequence(path, rate_cap=5.0).to_gcode(mock_stage, mock_dispenser)

        assert _dispense_rate(mock_dispenser) == pytest.approx(5.0)

    @pytest.mark.parametrize("rate", [1.0, 4.0, 16.0])
    def test_rate_sweep_amount_reaches_commanded_rate_at_fixed_speed(
        self, mock_stage, mock_dispenser, rate: float
    ):
        """吐出量キャリブ ② のレート掃引契約.

        rate_cap は頭打ちにしか働かないため、固定量では r_desired = 量×速度/長
        を超えるレートを指令しても届かない。amount = rate_sweep_amount_ul(rate, L, v) と
        rate_cap=rate の組で、実効レート = 指令レート・実効移動速度 = v （max_dispense_rate
        超えも掃引時は rate_cap 側が優先）を同時に満たす。
        """
        length, speed = 10.0, 2.0
        sequence = FillSequence(
            path=Path([Point3d(0.0, 0.0, 5.0), Point3d(length, 0.0, 5.0)]),
            total_amount_ul=rate_sweep_amount_ul(rate, length, speed),
            settings=_SETTINGS,
            fill_speed=speed,
            rate_cap=rate,
        )
        sequence.to_gcode(mock_stage, mock_dispenser)

        assert _dispense_rate(mock_dispenser) == pytest.approx(rate)
        actual = sequence.actual_fill_speed()
        assert actual is not None
        assert actual.resolve(100.0) == pytest.approx(speed)
