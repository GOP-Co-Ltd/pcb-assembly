"""Xbox コントローラでピンセットを操作する.

- LT（0〜1）は Open Close の開き具合（押し込むほど開き、離すと閉じる）
- LB / RB は Rotation の - / +。押している間は [pnp.servo] rotation_speed で回り続け、離すと止まる。
  両方押したら止まる

:class:`GamepadControl` は 1 周期ぶんを :meth:`GamepadControl.step` で進める。:meth:`GamepadControl.run`
はそれを固定周期で回し、終わるときに回転を止める（トルクは保持し、つまんだ部品を落とさない）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import NoReturn

from pcbasm.hal import Gamepad, ServoError, Tweezer

logger = logging.getLogger(__name__)

DEFAULT_PERIOD = 0.02  # [sec]（50 Hz）
_DEADBAND = 0.01  # これより小さい LT の変化は送らない（0 と 1 には必ず届ける）


class GamepadControl:
    """コントローラの入力をピンセットの指令にする."""

    def __init__(self, gamepad: Gamepad, tweezer: Tweezer) -> None:
        """入力元と送り先を受け取る（ここでは何も送らない）."""
        self._gamepad = gamepad
        self._tweezer = tweezer
        self._sent_opening: float | None = None
        self._rotating = False

    def step(self) -> None:
        """入力を 1 回読み、変わった分だけ指令を送る.

        Raises:
            GamepadError: コントローラが外れた場合
            ServoError: サーボとの通信に失敗した場合
        """
        state = self._gamepad.state()
        if self._should_send_opening(state.left_trigger):
            self._tweezer.open_to(state.left_trigger)
            self._sent_opening = state.left_trigger
        direction = int(state.right_bumper) - int(state.left_bumper)
        if direction != 0:
            self._tweezer.keep_rotating(direction)
            self._rotating = True
        elif self._rotating:
            self._tweezer.stop_rotation()
            self._rotating = False

    def run(
        self, checkpoint: Callable[[], None], *, period: float = DEFAULT_PERIOD
    ) -> NoReturn:
        """Checkpoint が例外を投げるか、コントローラが外れるまで、period ごとに :meth:`step` を回す.

        例外でしか終わらず、終わるときに回転を止める。トルクは切らない。止めるのに失敗しても、
        ログに残して元の例外（中止・切断など）を投げ直す。

        Args:
            checkpoint: 毎周期の最初に呼ぶ（中止なら例外を投げる。ジョブの ``ctx.checkpoint``）
            period: 周期 [sec]

        Raises:
            GamepadError: コントローラが外れた場合
            ServoError: サーボとの通信に失敗した場合
        """
        try:
            deadline = time.monotonic()
            while True:
                checkpoint()
                self.step()
                # 遅れたら追いつこうとせず、今から次の周期を数える
                deadline = max(deadline + period, time.monotonic())
                time.sleep(max(deadline - time.monotonic(), 0.0))
        except BaseException:
            try:
                self._tweezer.stop_rotation()
            except ServoError:
                logger.warning(
                    "コントローラ操作の終わりに回転を止められませんでした",
                    exc_info=True,
                )
            raise

    def _should_send_opening(self, opening: float) -> bool:
        if self._sent_opening is None:
            return True
        if opening == self._sent_opening:
            return False
        return abs(opening - self._sent_opening) >= _DEADBAND or opening in (
            0.0,
            1.0,
        )
