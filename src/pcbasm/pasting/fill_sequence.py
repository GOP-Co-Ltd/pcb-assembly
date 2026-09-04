"""1 ポリラインの塗布動作を 1 本の GCode プログラムに組む."""

import math

import attrs

from pcbasm.gcode import GCode
from pcbasm.geometry import Path
from pcbasm.hal import PasteDispenser, Speed, XYZStage
from pcbasm.pasting.params import DispenseSettings

# 接近・退避の移動速度（ステージ最大速度に対する比率）
_TRAVEL_SPEED = Speed.rate(1.0)


def _trapezoidal_time(distance: float, rate: float, accel: float) -> float:
    """台形速度プロファイルで距離を走行する時間を計算する.

    速度0から加速し、rateに達したら定速走行する。

    Args:
        distance: 走行距離
        rate: 最大速度
        accel: 加速度

    Returns:
        走行時間 [sec]
    """
    d_accel = rate**2 / (2 * accel)
    if distance <= d_accel:
        return math.sqrt(2 * distance / accel)
    return rate / accel + (distance - d_accel) / rate


@attrs.frozen
class FillSequence:
    """1ポリゴンの塗布動作を1本の送信可能なGCodeに組むプログラムオブジェクト.

    接近→下降→prime同期吐出→速度0→リトラクト・上昇同時開始→同期の順で実行する。

    移動速度（``fill_speed``、既定 ``settings.max_fill_speed``）を主設定とし、吐出レートは
    これに追従して導出する。導出レートが吐出レート上限を超える（吐出が移動に追いつかない）
    場合はレートを上限で頭打ちし、移動速度を下げて ``motion_time == dispense_time`` を保つ。
    これにより塗布量 ``total_amount_ul`` は経路全体に均一塗布され、吐出が痩せない。

    吐出レートの頭打ち値は ``rate_cap`` で決まる:

    - ``None`` = ``settings.max_dispense_rate`` で cap（通常塗布）
    - ``float`` = その値で cap（固定レート塗布）
    - ``math.inf`` = cap 無効（移動速度のみで律速）

    prime（リトラクション押し戻し）と吐出は1つの連続ステッパー動作として
    実行するため、prime も同じ実効レートで押し出される。prime_time はその実効
    レートから算出する。

    path は空でないこと（呼び出し側が保証する）。
    """

    path: Path
    total_amount_ul: float
    settings: DispenseSettings
    prime_extra_delay: float = 0.0  # prime 後の追加遅延 [sec]
    fill_speed: float | None = (
        None  # 塗布移動速度 [mm/sec]（None=settings.max_fill_speed）
    )
    rate_cap: float | None = (
        None  # 吐出レートの頭打ち値（None=settings.max_dispense_rate）
    )

    def _rate_cap(self) -> float:
        return (
            self.settings.max_dispense_rate if self.rate_cap is None else self.rate_cap
        )

    def _fill_speed(self) -> float:
        return (
            self.settings.max_fill_speed if self.fill_speed is None else self.fill_speed
        )

    def _effective_rate(self) -> float:
        """実効吐出レート [μL/sec]。fill_speed で量を出すのに要るレートを cap で頭打ち."""
        cap = self._rate_cap()
        length = self.path.length()
        if length <= 0:  # 点フィル: その場吐出なので上限レートで出す
            return cap
        r_desired = self.total_amount_ul * self._fill_speed() / length
        return min(r_desired, cap)

    def _dispense_time(self) -> float:
        rate = self._effective_rate()
        return self.total_amount_ul / rate if rate > 0 else 0.0

    def _extra_amount(self) -> float:
        """prime_extra_delay の間に実効レートで余分に押し出す量 [μL]."""
        return self._effective_rate() * self.prime_extra_delay

    @property
    def effective_rate(self) -> float:
        """実効吐出レート [μL/sec]."""
        return self._effective_rate()

    @property
    def prime_extra_volume_ul(self) -> float:
        """Prime追加遅延中に基板上へ押し出す体積 [μL]."""
        return self._extra_amount()

    def _prime_time(self) -> float:
        """Prime（リトラクション押し戻し）の所要時間 [sec]（prime_extra_delay 込み）.

        prime は吐出と同じ実効レートで押し出されるため、実効レートで算出する。
        """
        return (
            _trapezoidal_time(
                self.settings.retract_amount,
                self._effective_rate(),
                self.settings.dispense_accel,
            )
            + self.prime_extra_delay
        )

    def actual_fill_speed(self) -> Speed | None:
        """実際の塗布移動速度。経路長>0かつ吐出時間>0なら Speed.absolute、それ以外 None.

        レート非 cap 時は ``fill_speed`` に一致し、cap 時は減速後の速度になる。
        """
        length = self.path.length()
        dispense_time = self._dispense_time()
        if length > 0 and dispense_time > 0:
            return Speed.absolute(length / dispense_time)
        return None

    def to_gcode(self, stage: XYZStage, dispenser: PasteDispenser) -> GCode:
        """塗布プログラムを 1 本の GCode として組み立てる（明示座標のみ使用）."""
        first = self.path[0]
        last = self.path[-1]
        rate = self._effective_rate()
        prime_time = self._prime_time()
        gc = GCode()

        # 1. 最初の点の上空へ移動 → Z 下降（両方とも明示座標）
        lift = self.settings.lift_height
        gc.append(
            stage.move(x=first.x, y=first.y, z=first.z + lift, speed=_TRAVEL_SPEED)
        )
        gc.append(stage.move(x=first.x, y=first.y, z=first.z, speed=_TRAVEL_SPEED))
        gc.append(GCode.wait_for_done())

        # 2. prime+吐出を 1 連続動作として非同期開始
        amount = (
            self.settings.retract_amount + self._extra_amount() + self.total_amount_ul
        )
        gc.append(
            dispenser.pushpull(amount, rate, self.settings.dispense_accel, sync=False)
        )

        # 3. prime 後にステージ移動（経路長 0 の場合は吐出時間ぶん待機）
        speed = self.actual_fill_speed()
        if speed is not None:
            gc.append(GCode.wait(prime_time))
            gc.append(stage.to_gcode(self.path, speed=speed))
        else:
            gc.append(GCode.wait(prime_time + self._dispense_time()))
        # 4. Klipperのlookaheadを非ブロッキングでflushし、塗布移動の終端を
        # 速度0に固定する。リトラクションは吐出profileが速度0になる時刻まで
        # queueされ、直後のZ上昇も同じ時刻から始まる。
        gc.append(GCode("G4 P0"))

        # 5. 速度0からリトラクションを非同期開始
        gc.append(
            dispenser.continue_pushpull(
                amount,
                -self.settings.retract_amount,
                self.settings.retract_rate,
                self.settings.retract_accel,
                sync=False,
            )
        )

        # 6. リトラクションと同時にZ上昇（明示座標）
        gc.append(stage.move(x=last.x, y=last.y, z=last.z + lift, speed=_TRAVEL_SPEED))
        gc.append(dispenser.sync())
        return gc
