"""ピンセットを手で操作する Xbox コントローラ（ゲームパッド）の入力.

Raspberry Pi に USB か Bluetooth でつないだコントローラを Linux の evdev（``/dev/input/event*``）から読む。
読むのは左トリガー（LT）、左バンパー（LB）、右バンパー（RB）の 3 つだけ。

evdev での割り当ては接続方法とドライバで違うので、:func:`open_gamepad` がデバイスの capability から判別する。

- USB の ``xpad``（カーネル標準）: LT は ``ABS_Z``、LB / RB は ``BTN_TL`` / ``BTN_TR``。LT の範囲は Xbox One / Series が
  0〜1023（Xbox Series S|X を実機で確認）、Xbox 360 が 0〜255
- Bluetooth の ``hid-microsoft``（カーネル標準）: LT は ``ABS_BRAKE``（RT は ``ABS_GAS``）、右スティックが ``ABS_Z`` /
  ``ABS_RZ`` に載る。LB / RB は ``BTN_TL`` / ``BTN_TR``。範囲は 0〜1023（未実機。カーネルの割り当てからの推測）
- ``xpadneo``（Bluetooth）、``xone``（USB・ワイヤレスアダプタ）: どちらも LT は ``ABS_Z``、LB / RB は ``BTN_TL`` /
  ``BTN_TR``（未実機。各ドライバの文書からの推測）

``ABS_BRAKE`` があればそれを、無ければ ``ABS_Z`` を LT とみなす（``hid-microsoft`` では ``ABS_Z`` が右スティックのため）。
LT は absinfo の min / max で 0〜1 に正規化する。実装は evdev の :class:`EvdevGamepad` と、``pcbasm.hal.fake.FakeGamepad``
の 2 つに限る。
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


class GamepadError(DeviceError):
    """コントローラが見つからない・開けない・切断された."""


@attrs.frozen
class _GamepadLayout:
    """デバイスで LT が載る evdev の軸とその範囲（LB / RB は ``BTN_TL`` / ``BTN_TR``）."""

    trigger: int  # LT の軸（ecodes.ABS_*）
    trigger_min: int
    trigger_max: int

    @classmethod
    def detect(
        cls, capabilities: Mapping[int, Sequence[int | tuple[int, AbsInfo]]]
    ) -> _GamepadLayout | None:
        """デバイスの capability から割り当てを判別する（LT と LB・RB がそろわなければ None）.

        capabilities は ``InputDevice.capabilities(absinfo=True)`` の戻り値の形で渡す。
        """
        keys = set(capabilities.get(ecodes.EV_KEY, ()))
        if not {ecodes.BTN_TL, ecodes.BTN_TR} <= keys:
            return None
        axes = {
            entry[0]: entry[1]
            for entry in capabilities.get(ecodes.EV_ABS, ())
            if isinstance(entry, tuple)
        }
        for code in _TRIGGER_CODES:
            if (info := axes.get(code)) is not None and info.max > info.min:
                return cls(code, info.min, info.max)
        return None

    def trigger_ratio(self, raw: int) -> float:
        """LT の生の値を 0〜1 に正規化する（範囲外は収める）."""
        ratio = (raw - self.trigger_min) / (self.trigger_max - self.trigger_min)
        return min(max(ratio, 0.0), 1.0)

    def apply(self, state: GamepadState, event: InputEvent) -> GamepadState:
        """イベント 1 つを反映した状態を返す（LT・LB・RB 以外のイベントは無視する）."""
        if event.type == ecodes.EV_ABS and event.code == self.trigger:
            return attrs.evolve(state, left_trigger=self.trigger_ratio(event.value))
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
        keys = set(self._device.active_keys())
        raw = self._device.absinfo(self._layout.trigger).value
        return GamepadState(
            left_trigger=self._layout.trigger_ratio(raw),
            left_bumper=ecodes.BTN_TL in keys,
            right_bumper=ecodes.BTN_TR in keys,
        )


def open_gamepad(path: str | None = None) -> EvdevGamepad:
    """コントローラを開く.

    Args:
        path: 入力デバイス（``/dev/input/event*``）。None なら、読み書きできる入力デバイス（evdev の ``list_devices``）のうち LT と LB・RB を
            持つ最初のものを使う

    Raises:
        GamepadError: path を開けない・path が LT と LB・RB を持たない・None で該当するデバイスが無い場合
    """
    if path is not None:
        try:
            device = InputDevice(path)
        except OSError as exc:
            raise GamepadError(f"ゲームパッド {path} を開けません: {exc}") from exc
        if (layout := _GamepadLayout.detect(device.capabilities(absinfo=True))) is None:
            device.close()
            raise GamepadError(
                f"{path}（{device.name}）は LT と LB・RB を持つゲームパッドではありません"
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
    raise GamepadError("LT と LB・RB を持つゲームパッドが見つかりません")


def _gamepad(device: InputDevice, layout: _GamepadLayout) -> EvdevGamepad:
    try:
        return EvdevGamepad(device, layout)
    except OSError as exc:
        device.close()
        raise GamepadError(f"ゲームパッド {device.path} を読めません: {exc}") from exc
