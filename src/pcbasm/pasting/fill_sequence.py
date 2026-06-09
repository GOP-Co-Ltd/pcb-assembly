import math

import attrs

from pcbasm import gcode
from pcbasm.geometry import Path
from pcbasm.hal import PasteDispenser, Speed, XYZStage


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
    """1ポリゴンの塗布動作（接近→下降→prime同期吐出→リトラクト→上昇）を 1本の送信可能な GCode に組むプログラムオブジェクト.

    移動速度（``fill_speed``）を主設定とし、吐出レートはこれに追従して導出する。
    導出レートが ``max_dispense_rate`` を超える（吐出が移動に追いつかない）場合は
    レートを上限で頭打ちし、移動速度を下げて ``motion_time == dispense_time`` を保つ。
    これにより塗布量 ``total_amount`` は経路全体に均一塗布され、吐出が痩せない。

    prime（リトラクション押し戻し）と吐出は1つの連続ステッパー動作として
    実行するため、prime も同じ実効レートで押し出される。prime_time はその実効
    レートから算出する。

    path は空でないこと（呼び出し側が保証する）。
    """

    path: Path
    total_amount: float
    retraction: float
    fill_speed: float  # 希望移動速度 [mm/sec]（主設定）
    max_dispense_rate: float  # 吐出レート上限 [μL/sec]
    dispense_accel: float
    retraction_rate: float
    retraction_accel: float
    prime_extra_delay: float  # prime 後の追加遅延 [sec]
    lift_height: float
    travel_speed: Speed

    def _effective_rate(self) -> float:
        """実効吐出レート [μL/sec]。fill_speed で量を出すのに要るレートを max で頭打ち."""
        length = self.path.length()
        if length <= 0:  # 点フィル: その場吐出なので上限レートで出す
            return self.max_dispense_rate
        r_desired = self.total_amount * self.fill_speed / length
        return min(r_desired, self.max_dispense_rate)

    def _dispense_time(self) -> float:
        rate = self._effective_rate()
        return self.total_amount / rate if rate > 0 else 0.0

    def _extra_amount(self) -> float:
        """prime_extra_delay の間に実効レートで余分に押し出す量 [μL]."""
        return self._effective_rate() * self.prime_extra_delay

    def _prime_time(self) -> float:
        """Prime（リトラクション押し戻し）の所要時間 [sec]（prime_extra_delay 込み）.

        prime は吐出と同じ実効レートで押し出されるため、実効レートで算出する。
        """
        return (
            _trapezoidal_time(
                self.retraction, self._effective_rate(), self.dispense_accel
            )
            + self.prime_extra_delay
        )

    def fill_speed_actual(self) -> Speed | None:
        """実際の塗布移動速度。経路長>0かつ吐出時間>0なら Speed.absolute、それ以外 None.

        レート非 cap 時は ``fill_speed`` に一致し、cap 時は減速後の速度になる。
        """
        length = self.path.length()
        dispense_time = self._dispense_time()
        if length > 0 and dispense_time > 0:
            return Speed.absolute(length / dispense_time)
        return None

    def to_gcode(self, stage: XYZStage, dispenser: PasteDispenser) -> gcode.GCode:
        """塗布プログラムを 1 本の GCode として組み立てる（明示座標のみ使用）."""
        first = self.path[0]
        last = self.path[-1]
        rate = self._effective_rate()
        prime_time = self._prime_time()
        gc = gcode.GCode()

        # 1. 最初の点の上空へ移動 → Z 下降（両方とも明示座標）
        gc.append(
            stage.move(
                x=first.x,
                y=first.y,
                z=first.z + self.lift_height,
                speed=self.travel_speed,
            )
        )
        gc.append(stage.move(x=first.x, y=first.y, z=first.z, speed=self.travel_speed))
        gc.append(gcode.wait_for_done())

        # 2. prime+吐出を 1 連続動作として非同期開始
        amount = self.retraction + self._extra_amount() + self.total_amount
        gc.append(dispenser.pushpull(amount, rate, self.dispense_accel, sync=False))

        # 3. prime 後にステージ移動（経路長 0 の場合は吐出時間ぶん待機）
        speed = self.fill_speed_actual()
        if speed is not None:
            gc.append(gcode.wait(prime_time))
            gc.append(stage.to_gcode(self.path, speed=speed))
        else:
            gc.append(gcode.wait(prime_time + self._dispense_time()))
        gc.append(gcode.wait_for_done())

        # 4. リトラクション
        gc.append(
            dispenser.pushpull(
                -self.retraction, self.retraction_rate, self.retraction_accel
            )
        )

        # 5. Z 上昇（明示座標）
        gc.append(
            stage.move(
                x=last.x, y=last.y, z=last.z + self.lift_height, speed=self.travel_speed
            )
        )
        return gc
