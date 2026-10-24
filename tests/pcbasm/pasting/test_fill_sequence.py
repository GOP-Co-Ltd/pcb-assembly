"""FillSequence / MicroLineSequence のテスト.

FillSequence は1ポリゴンの塗布動作
（接近→下降→prime同期吐出→速度0→塗布後の静定待ち→リトラクト・上昇同時開始→同期）を1本のGCodeに組む
オーケストレーター。

stage / dispenser は FakeKlipper 上の実 XYZStage / PasteDispenser を使い、組み上がった G-code を FakeKlipper へ送り、その記録で動作の順序・量・速度を検証する。

FakeKlipper の printer.cfg は rotation_distance=1、max_velocity=100 なので、rotations_per_ul=1 のディスペンサーが出す ``MANUAL_STEPPER`` の MOVE / SPEED / ACCEL は μL・μL/s・μL/s² の値そのもの、移動（``Speed.rate(1.0)``）の feed は F6000 になる。

速度モデル: 移動速度 ``max_fill_speed`` を主設定とし、吐出レートはこれに追従して導出する
（``r = total_amount * max_fill_speed / path長``）。導出レートが頭打ち値（``rate_cap``、
既定は ``max_dispense_rate``）を超える場合はレートを上限で頭打ちし、移動速度を
``cap * 長 / total`` に下げて ``motion_time == dispense_time`` を保つ。
"""

import math

import attrs
import pytest

from pcbasm.gcode import GCode
from pcbasm.geometry import Path, Point3d
from pcbasm.hal import PasteDispenser, XYZStage
from pcbasm.hal.fake import FakeKlipper
from pcbasm.pasting.fill_sequence import FillSequence, MicroLineSequence
from pcbasm.pasting.flowcalib.flow import rate_sweep_amount_ul
from pcbasm.pasting.params import DispenseMotion

# retract_accel = factor * rate^2 / amount = 4.0 * 25 / 10 = 10.0
_MOTION = DispenseMotion(
    max_fill_speed=2.0,
    max_dispense_rate=10.0,
    dispense_accel=8.0,
    retract_amount=10.0,
    retract_rate=5.0,
    retract_accel_factor=4.0,
    lift_height=3.0,
    micro_line_settle_seconds=1.5,
    post_dispense_settle_seconds=0.0,
)


_STEPPER = "MANUAL_STEPPER STEPPER=paste_dispenser"
_TRAVEL_FEED = 6000.0


def _run(sequence: FillSequence | MicroLineSequence) -> FakeKlipper:
    """FakeKlipper 上の実 HAL で組んだ G-code を、その FakeKlipper へ送る."""
    klipper = FakeKlipper()
    dispenser = PasteDispenser(klipper, rotations_per_ul=1.0)
    klipper.send_gcode(sequence.to_gcode(XYZStage(klipper), dispenser))
    return klipper


def _stepper_moves(klipper: FakeKlipper) -> list[dict[str, float]]:
    """ディスペンサーの MOVE を送った順に {MOVE, SPEED, ACCEL[, SYNC]} で返す."""
    return [
        {
            key: float(value)
            for key, value in (word.split("=") for word in line.split()[2:])
        }
        for line in klipper.sent_lines
        if line.startswith(f"{_STEPPER} MOVE=")
    ]


def _travel_z(klipper: FakeKlipper) -> list[float]:
    """塗布経路以外の移動（接近・下降・上昇）の Z."""
    return [move["z"] for move in klipper.g1_moves() if move["f"] == _TRAVEL_FEED]


def _dispense_rate(klipper: FakeKlipper) -> float:
    """塗布吐出（非同期の最初の MOVE）の実効レート [μL/s]."""
    return next(
        move["SPEED"] for move in _stepper_moves(klipper) if move.get("SYNC") == 0
    )


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
        motion=_MOTION,
        prime_extra_delay=0.5,
        rate_cap=rate_cap,
    )


class TestFillSequence:
    """FillSequence クラスのテスト."""

    def test_fill_speed_resolves_to_max_fill_speed_when_rate_not_capped(self):
        # L=10: r_desired=4 ≤ max=10 → 減速なし。速度 = max_fill_speed = 2.0。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)])

        speed = _sequence(path).actual_fill_speed()

        assert speed is not None
        # 絶対速度なので max_velocity に依らず 2.0 に解決される。
        assert speed.resolve(100.0) == pytest.approx(2.0)

    def test_fill_speed_is_none_for_zero_length_path(self):
        # 単点 path は length=0 のため吐出移動が成立せず None。
        path = Path([Point3d(0.0, 0.0, 5.0)])

        assert _sequence(path).actual_fill_speed() is None

    def test_dispense_rate_follows_speed_when_not_capped(self):
        # L=10（非 cap）: 実効レート = total*max_fill_speed/L = 20*2/10 = 4.0。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)])

        assert _dispense_rate(_run(_sequence(path))) == pytest.approx(4.0)

    def test_to_gcode_runs_one_fill_program(self):
        """接近→下降→prime+吐出→経路→速度0→リトラクトと上昇を同時開始→同期."""
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)])

        klipper = _run(_sequence(path))

        assert klipper.sent_lines == [
            # 接近上空(z=first.z+lift) → 下降(z=first.z)。lift_height=3
            "G1 X0.0 Y0.0 Z8.0 F6000.0",
            "G1 X0.0 Y0.0 Z5.0 F6000.0",
            "M400",
            # prime+吐出を1つの連続動作として非同期開始。
            # 量 = retraction + extra_amount + total_amount。
            # L=10 で実効レート=4.0、extra = 4.0 * 0.5 = 2.0 → 10 + 2 + 20 = 32。
            f"{_STEPPER} SET_POSITION=0.0",
            f"{_STEPPER} MOVE=32.0 SPEED=4.0 ACCEL=8.0 SYNC=0",
            # prime の所要時間（台形 2.75 s + prime_extra_delay 0.5 s）を待ってから
            # path 全体を fill_speed（非 cap なので max_fill_speed = 2.0 mm/s）で走る
            "G4 P3250",
            "G1 X0.0 Y0.0 Z5.0 F120.0",
            "G1 X10.0 Y0.0 Z5.0 F120.0",
            # 速度 0 の境界。ここまで M400 を挟まない
            "G4 P0",
            # 吐出後の座標を維持したまま（SET_POSITION なし）リトラクションを非同期 queue し、
            # 同時に上昇(z=last.z+lift)する
            f"{_STEPPER} MOVE=22.0 SPEED=5.0 ACCEL=10.0 SYNC=0",
            "G1 X10.0 Y0.0 Z8.0 F6000.0",
            f"{_STEPPER} SYNC=1",
        ]

    def test_to_gcode_skips_path_fill_for_zero_length_path(self):
        # 単点 path は fill_speed_actual=None なので塗布移動を発行しない。
        path = Path([Point3d(0.0, 0.0, 5.0)])

        klipper = _run(_sequence(path))

        assert _travel_z(klipper) == pytest.approx([8.0, 5.0, 8.0])
        assert len(klipper.g1_moves()) == 3
        # それでも吐出と連続リトラクションは行われる。
        assert [move.get("SYNC") for move in _stepper_moves(klipper)] == [0.0, 0.0]


class TestRateCap:
    """``rate_cap`` による吐出レート頭打ち値の切替テスト."""

    def test_none_caps_at_max_dispense_rate(self):
        # rate_cap=None は max_dispense_rate で頭打ち（現行等価）。
        # L=2: r_desired=20 > cap=max=10 → rate=10、速度 = 2/2 = 1.0。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(2.0, 0.0, 5.0)])

        seq = _sequence(path, rate_cap=None)

        assert _dispense_rate(_run(seq)) == pytest.approx(10.0)
        speed = seq.actual_fill_speed()
        assert speed is not None
        assert speed.resolve(100.0) == pytest.approx(1.0)

    def test_explicit_cap_overrides_max_dispense_rate(self):
        # rate_cap=5.0（< max_dispense_rate=10）で頭打ち値を下げる。
        # L=2: r_desired=20 > cap=5 → rate=5、dispense_time=20/5=4、速度=2/4=0.5。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(2.0, 0.0, 5.0)])

        seq = _sequence(path, rate_cap=5.0)

        assert _dispense_rate(_run(seq)) == pytest.approx(5.0)
        speed = seq.actual_fill_speed()
        assert speed is not None
        assert speed.resolve(100.0) == pytest.approx(0.5)

    def test_inf_cap_disables_capping(self):
        # rate_cap=inf は cap 無効。L=2: r_desired=20 をそのまま採用 →
        # dispense_time=20/20=1、速度=2/1=2.0=max_fill_speed（減速なし）。
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(2.0, 0.0, 5.0)])

        seq = _sequence(path, rate_cap=math.inf)

        assert _dispense_rate(_run(seq)) == pytest.approx(20.0)
        speed = seq.actual_fill_speed()
        assert speed is not None
        assert speed.resolve(100.0) == pytest.approx(2.0)

    @pytest.mark.parametrize(("rate_cap", "expected"), [(None, 10.0), (5.0, 5.0)])
    def test_cap_applies_to_point_fill(self, rate_cap: float | None, expected: float):
        # 点フィル（L=0）は速度概念がないので、頭打ち値そのものでその場吐出する。
        path = Path([Point3d(0.0, 0.0, 5.0)])

        klipper = _run(_sequence(path, rate_cap=rate_cap))

        assert _dispense_rate(klipper) == pytest.approx(expected)

    @pytest.mark.parametrize("rate", [1.0, 4.0, 16.0])
    def test_rate_sweep_amount_reaches_commanded_rate_at_fixed_speed(self, rate: float):
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
            motion=_MOTION,
            fill_speed=speed,
            rate_cap=rate,
        )

        assert _dispense_rate(_run(sequence)) == pytest.approx(rate)
        actual = sequence.actual_fill_speed()
        assert actual is not None
        assert actual.resolve(100.0) == pytest.approx(speed)


def _micro_sequence(path: Path, *, settle_seconds: float = 1.5) -> MicroLineSequence:
    """total_amount=0.5 の微小線シーケンス（静定待ちだけ _MOTION から差し替える）."""
    return MicroLineSequence(
        path=path,
        total_amount_ul=0.5,
        motion=attrs.evolve(_MOTION, micro_line_settle_seconds=settle_seconds),
    )


# 始点 z=0.08、終点 z=0.05 の 2 点経路（board 表面からの隙間を変える経路）
_MICRO_PATH = Path([Point3d(0.0, 0.0, 0.08), Point3d(1.0, 0.0, 0.05)])


class TestMicroLineSequence:
    """先に上空で押し出し、静定後に下降してこすりつける微小線の G-code 契約."""

    def test_pushes_retraction_plus_amount_synchronously_before_descent(self):
        klipper = _run(_micro_sequence(_MICRO_PATH))

        # prime（retract_amount）+ 塗布量を吐出レート上限で、完了まで待つ同期動作で押し出し、
        # 上昇後に retract_amount を同期で引き戻す（SYNC=0 が付かない）
        assert _stepper_moves(klipper) == [
            {"MOVE": 10.5, "SPEED": 10.0, "ACCEL": 8.0},
            {"MOVE": -10.0, "SPEED": 5.0, "ACCEL": 10.0},
        ]

    def test_commands_run_push_settle_descend_fill_ascend_retract_in_order(self):
        klipper = _run(_micro_sequence(_MICRO_PATH))

        assert klipper.sent_lines == [
            # 押し出し前に上空到達を待つ
            "G1 X0.0 Y0.0 Z3.08 F6000.0",
            "M400",
            f"{_STEPPER} SET_POSITION=0.0",
            f"{_STEPPER} MOVE=10.5 SPEED=10.0 ACCEL=8.0",
            GCode.wait(1.5).to_list()[0],
            "G1 X0.0 Y0.0 Z0.08 F6000.0",
            "G1 X0.0 Y0.0 Z0.08 F120.0",
            "G1 X1.0 Y0.0 Z0.05 F120.0",
            "G1 X1.0 Y0.0 Z3.05 F6000.0",
            "M400",
            f"{_STEPPER} SET_POSITION=0.0",
            f"{_STEPPER} MOVE=-10.0 SPEED=5.0 ACCEL=10.0",
        ]

    def test_zero_settle_emits_no_wait(self):
        commands = _run(_micro_sequence(_MICRO_PATH, settle_seconds=0.0)).sent_lines

        assert not any(line.startswith("G4") for line in commands)

    def test_z_follows_path_endpoints_with_lift(self):
        # 上空(始点 z + lift) → 始点 z → (経路) → 終点 z + lift。lift_height=3
        klipper = _run(_micro_sequence(_MICRO_PATH))

        assert _travel_z(klipper) == pytest.approx([3.08, 0.08, 3.05])
        path = [move for move in klipper.g1_moves() if move["f"] != _TRAVEL_FEED]
        assert [Point3d(move["x"], move["y"], move["z"]) for move in path] == list(
            _MICRO_PATH
        )

    def test_fill_runs_at_max_fill_speed(self):
        sequence = _micro_sequence(_MICRO_PATH)

        klipper = _run(sequence)

        fill_feeds = {move["f"] for move in klipper.g1_moves()} - {_TRAVEL_FEED}
        assert fill_feeds == {2.0 * 60}
        actual = sequence.actual_fill_speed()
        assert actual is not None
        assert actual.resolve(100.0) == pytest.approx(2.0)
        assert sequence.effective_rate == pytest.approx(10.0)


class TestPostDispenseSettle:
    """塗布の移動が終わってから静定待ちし、そのあとでヘッドを上げる."""

    @staticmethod
    def _settled(settle: float = 0.25) -> DispenseMotion:
        return attrs.evolve(_MOTION, post_dispense_settle_seconds=settle)

    @pytest.mark.parametrize(
        "path",
        [
            Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)]),
            Path([Point3d(0.0, 0.0, 5.0)]),  # 点塗布
        ],
    )
    def test_fill_waits_then_retracts_and_ascends_together(self, path: Path):
        sequence = attrs.evolve(_sequence(path), motion=self._settled())

        commands = _run(sequence).sent_lines

        stop = commands.index("G4 P0")
        settle = commands.index("G4 P250")
        retract = max(
            index
            for index, line in enumerate(commands)
            if line.startswith(f"{_STEPPER} MOVE=")
        )
        ascent = max(
            index for index, line in enumerate(commands) if line.startswith("G1 ")
        )
        # リトラクトの MOVE は冒頭でツールヘッドの時刻へ揃うので、待ちの後に上昇と同時に始まる
        assert stop < settle < retract < ascent

    def test_zero_settle_keeps_the_sequence_unchanged(self):
        path = Path([Point3d(0.0, 0.0, 5.0), Point3d(10.0, 0.0, 5.0)])
        settled = attrs.evolve(_sequence(path), motion=self._settled(0.0))

        commands = _run(settled).sent_lines

        # 待ちが 0 なら、prime 待ちと速度 0 の G4 P0 のほかに G4 を足さない
        after_fill = commands[commands.index("G1 X10.0 Y0.0 Z5.0 F120.0") + 1 :]
        assert [line for line in after_fill if line.startswith("G4")] == ["G4 P0"]

    def test_micro_line_waits_after_fill_before_ascent(self):
        sequence = MicroLineSequence(
            path=_MICRO_PATH, total_amount_ul=0.5, motion=self._settled()
        )

        commands = _run(sequence).sent_lines

        fill = commands.index("G1 X1.0 Y0.0 Z0.05 F120.0")
        settle = commands.index("G4 P250")
        ascent = commands.index("G1 X1.0 Y0.0 Z3.05 F6000.0")
        retract = commands.index(f"{_STEPPER} MOVE=-10.0 SPEED=5.0 ACCEL=10.0")
        assert fill < settle < ascent < retract
