"""FillSequence のテスト.

FillSequence は1ポリゴンの塗布動作（接近→下降→prime同期吐出→リトラクト→上昇）を
1本のGCodeに組むオーケストレーター。ここでは stage / dispenser（いずれも pcbasm 自前の HAL ABC）を
mock し、発行される動作の順序・量・速度を call assertion で検証する。 個々の GCode 文字列は
stage/dispenser 側の責務なので（mock は空 GCode を返す）、 本テストは FillSequence が両 HAL
をどう駆動するかの契約のみを固定する。
"""

import pytest
from pytest_mock import MockerFixture

from pcbasm.gcode import GCode
from pcbasm.geometry import Path, Point3d
from pcbasm.hal import Speed
from pcbasm.pasting import FillSequence


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
    return dispenser


def _sequence(path: Path) -> FillSequence:
    """具体的な数値で構成した FillSequence を返す（テスト間で共有）.

    total_amount/dispense_rate = 20/4 = 5.0 [sec] (= dispense_time)。
    path.length() が L のとき fill_speed は L/5.0 [mm/s] に解決される。
    """
    return FillSequence(
        path=path,
        total_amount=20.0,
        retraction=10.0,
        extra_amount=2.0,
        dispense_rate=4.0,
        dispense_accel=8.0,
        retraction_rate=5.0,
        retraction_accel=10.0,
        prime_time=0.5,
        lift_height=3.0,
        travel_speed=Speed.absolute(30.0),
    )


class TestFillSequence:
    """FillSequence クラスのテスト."""

    def test_fill_speed_resolves_to_length_over_dispense_time(self):
        # 2点が 10mm 離れた path。length=10, dispense_time=20/4=5 → 10/5=2.0 mm/s。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)])

        speed = _sequence(path).fill_speed()

        assert speed is not None
        # 絶対速度なので max_velocity に依らず 2.0 に解決される。
        assert speed.resolve(100.0) == pytest.approx(2.0)

    def test_fill_speed_is_none_for_zero_length_path(self):
        # 単点 path は length=0 のため吐出移動が成立せず None。
        path = Path([Point3d(0.0, 0.0, 5.0)])

        assert _sequence(path).fill_speed() is None

    def test_to_gcode_pushpull_called_twice_with_dispense_then_retraction(
        self, mock_stage, mock_dispenser
    ):
        # 吐出（prime+dispense）とリトラクションの2回。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)])

        _sequence(path).to_gcode(mock_stage, mock_dispenser)

        calls = mock_dispenser.pushpull.call_args_list
        assert len(calls) == 2
        # 1回目: prime+吐出を1つの連続動作として非同期開始（sync=False）。
        # 量 = retraction + extra_amount + total_amount = 10 + 2 + 20 = 32。
        assert calls[0].args[0] == pytest.approx(32.0)
        assert calls[0].kwargs.get("sync") is False
        # 2回目: リトラクション = -retraction = -10。
        assert calls[1].args[0] == pytest.approx(-10.0)

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
            "speed": Speed.absolute(30.0),
        }
        # 2. 塗布高さへ下降。
        assert moves[1].kwargs == {
            "x": 0.0,
            "y": 0.0,
            "z": 5.0,
            "speed": Speed.absolute(30.0),
        }
        # 3. 最後の点で上昇。
        assert moves[2].kwargs == {
            "x": 10.0,
            "y": 0.0,
            "z": 8.0,
            "speed": Speed.absolute(30.0),
        }

    def test_to_gcode_fills_along_path_with_fill_speed(
        self, mock_stage, mock_dispenser
    ):
        # fill_speed が None でないとき、塗布移動を path 全体に対し1回発行する。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)])

        _sequence(path).to_gcode(mock_stage, mock_dispenser)

        mock_stage.to_gcode.assert_called_once()
        call = mock_stage.to_gcode.call_args
        # path 全体を渡す（位置引数 or path= キーワードのどちらでも許容）。
        passed_path = call.kwargs.get("path", call.args[0] if call.args else None)
        assert passed_path == path
        # 速度は fill_speed（length/dispense_time → 2.0 mm/s）。
        passed_speed = call.kwargs["speed"]
        assert passed_speed.resolve(100.0) == pytest.approx(2.0)

    def test_to_gcode_skips_path_fill_for_zero_length_path(
        self, mock_stage, mock_dispenser
    ):
        # 単点 path は fill_speed=None なので塗布移動（stage.to_gcode）を発行しない。
        path = Path([Point3d(0.0, 0.0, 5.0)])

        _sequence(path).to_gcode(mock_stage, mock_dispenser)

        mock_stage.to_gcode.assert_not_called()
        # それでも吐出・リトラクションは行われる（2回）。
        assert mock_dispenser.pushpull.call_count == 2

    def test_to_gcode_returns_gcode(self, mock_stage, mock_dispenser):
        # 単一の連結 GCode を返す（送信側はこれを1回で送る）。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)])

        result = _sequence(path).to_gcode(mock_stage, mock_dispenser)

        assert isinstance(result, GCode)
