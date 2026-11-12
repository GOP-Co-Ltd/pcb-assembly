"""Xbox コントローラでピンセットと XYZ を操作する.

- LT（0〜1）は Open Close の開き具合（押し込むほど開き、離すと閉じる）
- LB / RB は Rotation の - / +。押している間は [pnp.servo] rotation_speed で回り続け、離すと止まる。
  両方押したら止まる
- XYZ のステージ（:class:`~pcbasm.hal.MotionStage`）を渡したときは、左スティックで XY、右スティックの縦で Z の
  速度を送る（上 = Y−、右 = X+、右スティックの上 = Z+。倒し切ると各軸の最高速度）。デッドゾーンは
  [pnp.gamepad] の値を掛ける

画面の仮想スティック（:meth:`GamepadControl.set_virtual`）は、受けてから :data:`VIRTUAL_TIMEOUT` の間だけ有効で、
中立でなければゲームパッドのスティックより優先する。仮想スティックにはデッドゾーンを掛けない。

:class:`GamepadControl` は 1 周期ぶんを :meth:`GamepadControl.step` で進める。:meth:`GamepadControl.run`
はそれを固定周期で回し、終わるときに回転とステージを止める（トルクは保持し、つまんだ部品を落とさない）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import NoReturn

from pcbasm.config.pnp import GamepadConfig
from pcbasm.hal import (
    Gamepad,
    GamepadError,
    GamepadState,
    MotionError,
    MotionStage,
    ServoError,
    Tweezer,
    Velocity,
)

from .stick import StickInput, stick_velocity

logger = logging.getLogger(__name__)

DEFAULT_PERIOD = 0.02  # [sec]（50 Hz）
# 仮想スティックの入力が途切れたら、これを過ぎて中立とみなす
VIRTUAL_TIMEOUT = 0.3  # [sec]
_DEADBAND = 0.01  # これより小さい LT の変化は送らない（0 と 1 には必ず届ける）
_NEUTRAL = StickInput()


class GamepadControl:
    """コントローラ（と画面の仮想スティック）の入力をピンセットと XYZ の指令にする."""

    def __init__(
        self,
        gamepad: Gamepad | None,
        tweezer: Tweezer | None,
        *,
        motion: MotionStage | None = None,
        settings: GamepadConfig = GamepadConfig(),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """入力元と送り先を受け取る（ここでは何も送らない）.

        Args:
            gamepad: 物理コントローラ（None なら仮想スティックだけ）
            tweezer: ピンセット（None ならピンセットの操作をしない）
            motion: XYZ のステージ（None なら XYZ を動かさない）
            settings: スティックのデッドゾーン（[pnp.gamepad]）
            clock: 仮想スティックの期限を数える時計 [sec]
        """
        self._gamepad = gamepad
        self._tweezer = tweezer
        self._motion = motion
        self._settings = settings
        self._clock = clock
        self._sent_opening: float | None = None
        self._rotating = False
        self._virtual = _NEUTRAL
        self._virtual_at = -float("inf")
        self._reported_unhomed: set[str] = set()

    def set_virtual(self, stick: StickInput) -> None:
        """画面の仮想スティックの入力を受ける（:data:`VIRTUAL_TIMEOUT` を過ぎたら中立とみなす）."""
        self._virtual = stick
        self._virtual_at = self._clock()

    def step(self) -> None:
        """入力を 1 回読み、ピンセットには変わった分だけ、ステージには毎回速度を送る.

        ステージがあれば、コントローラが外れてもログに出して続ける（以後は仮想スティックだけ）。

        Raises:
            GamepadError: ステージが無く、コントローラが外れた場合
            ServoError: サーボとの通信に失敗した場合
            MotionError: ステージが指令を受け付けない場合
        """
        state = self._read_gamepad()
        if self._tweezer is not None:
            self._drive_tweezer(self._tweezer, state)
        if self._motion is not None:
            self._motion.set_velocity(self._velocity(self._motion, state))

    def run(
        self, checkpoint: Callable[[], None], *, period: float = DEFAULT_PERIOD
    ) -> NoReturn:
        """Checkpoint が例外を投げるまで、period ごとに :meth:`step` を回す.

        ステージが無ければ、コントローラが外れても終わる。例外でしか終わらず、終わるときに回転とステージを止める。
        トルクは切らない。どちらかを止めるのに失敗しても、もう片方も試し、ログに残して元の例外（中止・切断など）を
        投げ直す。

        Args:
            checkpoint: 毎周期の最初に呼ぶ（中止なら例外を投げる。ジョブはここで仮想スティックや
                パネルの命令を処理する）
            period: 周期 [sec]

        Raises:
            GamepadError: ステージが無く、コントローラが外れた場合
            ServoError: サーボとの通信に失敗した場合
            MotionError: ステージが指令を受け付けない場合
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
            self._stop_all()
            raise

    def _read_gamepad(self) -> GamepadState | None:
        if self._gamepad is None:
            return None
        try:
            return self._gamepad.state()
        except GamepadError:
            if self._motion is None:
                raise
            logger.warning(
                "コントローラが外れました。画面の仮想スティックで操作を続けます",
                exc_info=True,
            )
            self._gamepad = None
            return None

    def _drive_tweezer(self, tweezer: Tweezer, state: GamepadState | None) -> None:
        if state is not None and self._should_send_opening(state.left_trigger):
            tweezer.open_to(state.left_trigger)
            self._sent_opening = state.left_trigger
        # コントローラが無ければ開き具合はそのままにし、回っていれば止める
        direction = (
            0 if state is None else int(state.right_bumper) - int(state.left_bumper)
        )
        if direction != 0:
            tweezer.keep_rotating(direction)
            self._rotating = True
        elif self._rotating:
            tweezer.stop_rotation()
            self._rotating = False

    def _velocity(self, motion: MotionStage, state: GamepadState | None) -> Velocity:
        virtual = self._virtual
        if self._clock() - self._virtual_at > VIRTUAL_TIMEOUT:
            virtual = _NEUTRAL
        if virtual != _NEUTRAL:
            velocity = stick_velocity(virtual, motion.max_velocity)
        elif state is not None:
            velocity = stick_velocity(
                StickInput(x=state.left_x, y=state.left_y, z=state.right_y),
                motion.max_velocity,
                deadzone_xy=self._settings.deadzone_xy,
                deadzone_z=self._settings.deadzone_z,
            )
        else:
            return Velocity()
        if velocity == Velocity():
            return velocity
        return self._drop_unhomed(motion, velocity)

    def _drop_unhomed(self, motion: MotionStage, velocity: Velocity) -> Velocity:
        """原点が未の軸を 0 にする（ステージは 0 以外を拒むので、例外にせずログに 1 回だけ出す）."""
        homed = motion.status().homed
        values = {"x": velocity.x, "y": velocity.y, "z": velocity.z}
        dropped = sorted(a for a, v in values.items() if v != 0.0 and a not in homed)
        if fresh := [a for a in dropped if a not in self._reported_unhomed]:
            logger.warning(
                "原点出しをしていない軸はスティックで動かしません: %s", ", ".join(fresh)
            )
            self._reported_unhomed.update(fresh)
        return Velocity(
            x=0.0 if "x" in dropped else velocity.x,
            y=0.0 if "y" in dropped else velocity.y,
            z=0.0 if "z" in dropped else velocity.z,
        )

    def _stop_all(self) -> None:
        if self._motion is not None:
            try:
                self._motion.stop()
            except MotionError:
                logger.warning(
                    "コントローラ操作の終わりにステージを止められませんでした",
                    exc_info=True,
                )
        if self._tweezer is not None:
            try:
                self._tweezer.stop_rotation()
            except ServoError:
                logger.warning(
                    "コントローラ操作の終わりに回転を止められませんでした",
                    exc_info=True,
                )

    def _should_send_opening(self, opening: float) -> bool:
        if self._sent_opening is None:
            return True
        if opening == self._sent_opening:
            return False
        return abs(opening - self._sent_opening) >= _DEADBAND or opening in (
            0.0,
            1.0,
        )
