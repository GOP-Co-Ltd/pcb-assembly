from types import TracebackType
from typing import Self

import attrs
from gpiozero import RotaryEncoder


@attrs.frozen
class ProbeResult:
    """プローブの計測結果."""

    min: float
    max: float


class Probe:
    """ロータリーエンコーダを用いた接触式距離センサー.

    Example:
        probe = Probe(a_pin=17, b_pin=18, rotation_distance=10.0)

        with probe:
            # プローブを基板に接触させる操作
            pass
        result = probe.result()
        print(result.min, result.max)
    """

    def __init__(
        self,
        a_pin: int,
        b_pin: int,
        rotation_distance: float,
        rotation_pulse: int = 600,
        inverse: bool = False,
    ) -> None:
        """プローブを初期化する.

        Args:
            a_pin: A相のGPIOピン番号
            b_pin: B相のGPIOピン番号
            rotation_distance: 1回転あたりの移動距離
            rotation_pulse: 1回転あたりのパルス数 (PPR)
            inverse: Trueの場合、エンコーダの回転方向を反転
        """
        self._encoder = RotaryEncoder(a_pin, b_pin, max_steps=0)
        self._encoder.when_rotated = self._update_minmax
        self._distance_per_step = rotation_distance / rotation_pulse
        self._sign = -1 if inverse else 1
        self._min_steps: int | None = None
        self._max_steps: int | None = None
        self._measuring: bool = False

    def __del__(self) -> None:
        if hasattr(self, "_encoder"):
            self._encoder.close()

    @property
    def _steps(self) -> int:
        return self._encoder.steps * self._sign

    def _reset(self) -> None:
        self._encoder.steps = 0
        self._min_steps = 0
        self._max_steps = 0

    def _update_minmax(self) -> None:
        if not self._measuring:
            return
        current = self._steps
        self._min_steps = min(self._min_steps or 0, current)
        self._max_steps = max(self._max_steps or 0, current)

    def start(self) -> None:
        """計測を開始する."""
        if self._measuring:
            raise RuntimeError("計測中です")
        self._reset()
        self._measuring = True

    def stop(self) -> None:
        """計測を終了する."""
        if not self._measuring:
            raise RuntimeError("計測中ではありません")
        self._update_minmax()
        self._measuring = False

    def result(self) -> ProbeResult:
        """計測結果の距離を返す."""
        if self._measuring:
            raise RuntimeError("計測中です。stopを呼んでください")
        if self._min_steps is None or self._max_steps is None:
            raise RuntimeError("計測が行われていません")
        return ProbeResult(
            min=self._min_steps * self._distance_per_step,
            max=self._max_steps * self._distance_per_step,
        )

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.stop()
