"""ピンセットと XYZ を手で操作する Xbox コントローラ（ゲームパッド）の入力.

Raspberry Pi に USB か Bluetooth でつないだコントローラを Linux の evdev（``/dev/input/event*``）から読む。
読むのは左トリガー（LT）、左バンパー（LB）、右バンパー（RB）、左スティックの横・縦、右スティックの縦だけ。

evdev での割り当ては接続方法とドライバで違うので、:func:`open_gamepad` がデバイスの capability から判別する。

- USB の ``xpad``（カーネル標準）: LT は ``ABS_Z``（RT は ``ABS_RZ``）、左スティックは ``ABS_X`` / ``ABS_Y``、右スティックの縦は
  ``ABS_RY``。LT の範囲は Xbox One / Series が 0〜1023（Xbox Series S|X を実機で確認）、Xbox 360 が 0〜255。スティックは
  -32768〜32767
- Bluetooth の ``hid-microsoft``（カーネル標準）: LT は ``ABS_BRAKE``（RT は ``ABS_GAS``）、左スティックは ``ABS_X`` / ``ABS_Y``、
  右スティックは ``ABS_Z``（横）/ ``ABS_RZ``（縦）。LT は 0〜1023、スティックは 0〜65535（未実機。カーネルの割り当てからの推測）
- ``xpadneo``（Bluetooth）、``xone``（USB・ワイヤレスアダプタ）: どちらも LT は ``ABS_Z``、左スティックは ``ABS_X`` / ``ABS_Y``、
  右スティックの縦は ``ABS_RY``（未実機。各ドライバの文書からの推測）

LB / RB はどれも ``BTN_TL`` / ``BTN_TR``。``ABS_BRAKE`` があればそれを、無ければ ``ABS_Z`` を LT とみなす（``hid-microsoft``
では ``ABS_Z`` が右スティックのため）。右スティックの縦は、LT が ``ABS_BRAKE`` なら ``ABS_RZ``、それ以外は ``ABS_RY``。

LT は absinfo の min / max で 0〜1 に正規化する。スティックは absinfo の min と max の中点（整数に切り上げ）を中立とし、-1〜1 に正規化する。
evdev の縦軸は下が + なので、符号を返して「右と上が +」にそろえる。デッドゾーンはここでは掛けない（使う側が掛ける）。
LT・LB・RB とスティック（左の横・縦と右の縦）のどれかが欠けるデバイスは開かない。実装は evdev の :class:`EvdevGamepad` と、
``pcbasm.hal.fake.FakeGamepad`` の 2 つに限る。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import override

import attrs
from evdev import AbsInfo, InputDevice, InputEvent, ecodes, list_devices

from pcbasm.errors import DeviceError

# LT の候補（先にあるものを採る）
_TRIGGER_CODES = (ecodes.ABS_BRAKE, ecodes.ABS_Z)


@attrs.frozen
class GamepadState:
    """コントローラの入力（既定は何も押していない状態）."""

    left_trigger: float = 0.0  # LT（0=離す、1=押し込み）
    left_bumper: bool = False  # LB
    right_bumper: bool = False  # RB
    # スティック（-1〜1。右と上が +、中立は 0。デッドゾーンは掛けていない）
    left_x: float = 0.0  # 左スティックの横
    left_y: float = 0.0  # 左スティックの縦
    right_y: float = 0.0  # 右スティックの縦


class GamepadError(DeviceError):
    """コントローラが見つからない・開けない・切断された."""


@attrs.frozen
class _Axis:
    """入力デバイスの軸（evdev の ABS_*）1 本とその範囲."""

    code: int  # ecodes.ABS_*
    minimum: int
    maximum: int

    def ratio(self, raw: int) -> float:
        """生の値を 0〜1 に正規化する（範囲外は収める）."""
        ratio = (raw - self.minimum) / (self.maximum - self.minimum)
        return min(max(ratio, 0.0), 1.0)

    def signed(self, raw: int) -> float:
        """生の値を、min と max の中点を 0 とする -1〜1 に正規化する（範囲外は収める）.

        中点は整数に切り上げ（xpad の -32768〜32767 は 0、hid-microsoft の 0〜65535 は
        32768）、静止の値をちょうど 0 にする。

        中点の両側をそれぞれの幅で割るので、min と max はちょうど -1 と 1 になる。
        """
        center = (self.minimum + self.maximum + 1) // 2
        if raw >= center:
            return min((raw - center) / (self.maximum - center), 1.0)
        return max((raw - center) / (center - self.minimum), -1.0)


@attrs.frozen
class _GamepadLayout:
    """デバイスで LT とスティックが載る evdev の軸（LB / RB は ``BTN_TL`` / ``BTN_TR``）."""

    trigger: _Axis  # LT
    left_x: _Axis
    left_y: _Axis
    right_y: _Axis

    @classmethod
    def detect(
        cls, capabilities: Mapping[int, Sequence[int | tuple[int, AbsInfo]]]
    ) -> _GamepadLayout | None:
        """デバイスの capability から割り当てを判別する（LT・LB・RB・スティックがそろわなければ None）.

        capabilities は ``InputDevice.capabilities(absinfo=True)`` の戻り値の形で渡す。
        """
        keys = set(capabilities.get(ecodes.EV_KEY, ()))
        if not {ecodes.BTN_TL, ecodes.BTN_TR} <= keys:
            return None
        axes = {
            entry[0]: _Axis(entry[0], entry[1].min, entry[1].max)
            for entry in capabilities.get(ecodes.EV_ABS, ())
            if isinstance(entry, tuple) and entry[1].max > entry[1].min
        }
        trigger = next((axes[c] for c in _TRIGGER_CODES if c in axes), None)
        if trigger is None:
            return None
        # hid-microsoft（LT が ABS_BRAKE）は右スティックが ABS_Z / ABS_RZ に載る
        right_y = ecodes.ABS_RZ if trigger.code == ecodes.ABS_BRAKE else ecodes.ABS_RY
        left_x, left_y, right = (
            axes.get(code) for code in (ecodes.ABS_X, ecodes.ABS_Y, right_y)
        )
        if left_x is None or left_y is None or right is None:
            return None
        return cls(trigger, left_x, left_y, right)

    def read(self, raw: Mapping[int, int], keys: set[int]) -> GamepadState:
        """軸の生の値と押されているキーから状態を組む."""
        return GamepadState(
            left_trigger=self.trigger.ratio(raw[self.trigger.code]),
            left_bumper=ecodes.BTN_TL in keys,
            right_bumper=ecodes.BTN_TR in keys,
            left_x=self.left_x.signed(raw[self.left_x.code]),
            left_y=-self.left_y.signed(raw[self.left_y.code]),
            right_y=-self.right_y.signed(raw[self.right_y.code]),
        )

    def axes(self) -> tuple[_Axis, ...]:
        """読む軸の一覧."""
        return (self.trigger, self.left_x, self.left_y, self.right_y)

    def apply(self, state: GamepadState, event: InputEvent) -> GamepadState:
        """イベント 1 つを反映した状態を返す（LT・LB・RB・スティック以外のイベントは無視する）."""
        if event.type == ecodes.EV_ABS:
            if event.code == self.trigger.code:
                return attrs.evolve(state, left_trigger=self.trigger.ratio(event.value))
            if event.code == self.left_x.code:
                return attrs.evolve(state, left_x=self.left_x.signed(event.value))
            if event.code == self.left_y.code:
                return attrs.evolve(state, left_y=-self.left_y.signed(event.value))
            if event.code == self.right_y.code:
                return attrs.evolve(state, right_y=-self.right_y.signed(event.value))
            return state
        if event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TL:
            return attrs.evolve(state, left_bumper=event.value != 0)
        if event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TR:
            return attrs.evolve(state, right_bumper=event.value != 0)
        return state


class Gamepad(ABC):
    """コントローラ 1 台."""

    @abstractmethod
    def state(self) -> GamepadState:
        """最新の入力を返す（ブロックしない。前回からのイベントを読み切って反映する）.

        Raises:
            GamepadError: コントローラが切断された、または閉じた後に呼んだ場合
        """

    @abstractmethod
    def close(self) -> None:
        """デバイスを閉じる."""


class EvdevGamepad(Gamepad):
    """入力デバイスを evdev で読む :class:`Gamepad`（:func:`open_gamepad` で作る）.

    デバイスは grab しない（他のプロセスも同じコントローラを読める）。カーネルのイベントの溜まりが
    あふれた（``SYN_DROPPED``）ときは、デバイスの今の値を読み直す。
    """

    def __init__(self, device: InputDevice, layout: _GamepadLayout) -> None:
        """開いたデバイスと、その割り当てから作る（開いた時点で押されている入力も読む）.

        Raises:
            OSError: デバイスを読めない場合
        """
        self._device = device
        self._layout = layout
        self._state = self._current()
        self._closed = False

    @override
    def state(self) -> GamepadState:
        if self._closed:
            raise GamepadError(f"ゲームパッド {self._device.path} は閉じています")
        try:
            while (event := self._device.read_one()) is not None:
                if event.type == ecodes.EV_SYN and event.code == ecodes.SYN_DROPPED:
                    self._state = self._current()
                else:
                    self._state = self._layout.apply(self._state, event)
        except OSError as exc:
            raise GamepadError(
                f"ゲームパッド {self._device.path} が切断されました: {exc}"
            ) from exc
        return self._state

    @override
    def close(self) -> None:
        self._device.close()
        self._closed = True

    def _current(self) -> GamepadState:
        """デバイスが今持つ値（イベントを介さず ioctl で読む）."""
        raw = {
            axis.code: self._device.absinfo(axis.code).value
            for axis in self._layout.axes()
        }
        return self._layout.read(raw, set(self._device.active_keys()))


def open_gamepad(path: str | None = None) -> EvdevGamepad:
    """コントローラを開く.

    Args:
        path: 入力デバイス（``/dev/input/event*``）。None なら、読み書きできる入力デバイス（evdev の ``list_devices``）のうち
            LT・LB・RB・スティックを持つ最初のものを使う

    Raises:
        GamepadError: path を開けない・path が LT・LB・RB・スティックを持たない・None で該当するデバイスが無い場合
    """
    if path is not None:
        try:
            device = InputDevice(path)
        except OSError as exc:
            raise GamepadError(f"ゲームパッド {path} を開けません: {exc}") from exc
        if (layout := _GamepadLayout.detect(device.capabilities(absinfo=True))) is None:
            device.close()
            raise GamepadError(
                f"{path}（{device.name}）は LT・LB・RB・スティックを持つゲームパッドではありません"
            )
        return _gamepad(device, layout)
    for candidate in list_devices():
        try:
            device = InputDevice(candidate)
        except OSError:
            continue  # 権限が無い・その間に外れた
        if (layout := _GamepadLayout.detect(device.capabilities(absinfo=True))) is None:
            device.close()
            continue
        return _gamepad(device, layout)
    raise GamepadError("LT・LB・RB・スティックを持つゲームパッドが見つかりません")


def _gamepad(device: InputDevice, layout: _GamepadLayout) -> EvdevGamepad:
    try:
        return EvdevGamepad(device, layout)
    except OSError as exc:
        device.close()
        raise GamepadError(f"ゲームパッド {device.path} を読めません: {exc}") from exc
