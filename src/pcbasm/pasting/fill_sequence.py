import attrs

from pcbasm import gcode
from pcbasm.geometry import Path
from pcbasm.hal import PasteDispenser, Speed, XYZStage


@attrs.frozen
class FillSequence:
    """1ポリゴンの塗布動作（接近→下降→prime同期吐出→リトラクト→上昇）を 1本の送信可能な GCode に組むプログラムオブジェクト.

    path は空でないこと（呼び出し側が保証する）。
    """

    path: Path
    total_amount: float
    retraction: float
    extra_amount: float
    dispense_rate: float
    dispense_accel: float
    retraction_rate: float
    retraction_accel: float
    prime_time: float  # prime_extra_delay 込みの待機時間 [sec]
    lift_height: float
    travel_speed: Speed

    def _dispense_time(self) -> float:
        return self.total_amount / self.dispense_rate

    def fill_speed(self) -> Speed | None:
        """塗布移動の速度。経路長>0かつ吐出時間>0なら Speed.absolute、それ以外 None."""
        if self.path.length() > 0 and self._dispense_time() > 0:
            return Speed.absolute(self.path.length() / self._dispense_time())
        return None

    def to_gcode(self, stage: XYZStage, dispenser: PasteDispenser) -> gcode.GCode:
        """塗布プログラムを 1 本の GCode として組み立てる（明示座標のみ使用）."""
        first = self.path[0]
        last = self.path[-1]
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
        amount = self.retraction + self.extra_amount + self.total_amount
        gc.append(
            dispenser.pushpull(
                amount, self.dispense_rate, self.dispense_accel, sync=False
            )
        )

        # 3. prime 後にステージ移動（経路長 0 の場合は吐出時間ぶん待機）
        speed = self.fill_speed()
        if speed is not None:
            gc.append(gcode.wait(self.prime_time))
            gc.append(stage.to_gcode(self.path, speed=speed))
        else:
            gc.append(gcode.wait(self.prime_time + self._dispense_time()))
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
