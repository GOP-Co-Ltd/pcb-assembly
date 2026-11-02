"""[pnp] 節：Pick and Place（部品実装）.

``[pnp]`` があれば、その機体は部品を実装できる（:attr:`pcbasm.config.MachineConfig.pnp`）。
空の表も受け付ける。下位表は装置の部品で分ける。

- ``servo``: ピンセットのサーボ（Feetech STS3032 ×2）のポートと役割
"""

from __future__ import annotations

import attrs

from pcbasm.config.fields import setting
from pcbasm.validators import as_validator

_SERVO = "ピンセットのサーボ"

_MAX_SERVO_ID = 253
_MAX_POSITION = 4095
# 閉じ〜開きが半回転を超えるなら 4095/0 の境目をまたいでいる
_MAX_OPEN_CLOSE_SPAN = 2048


def _validate_servo_id(name: str, value: object) -> str | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_SERVO_ID
    ):
        return f"{name}は0〜{_MAX_SERVO_ID}の整数である必要があります: {value!r}"
    return None


def _validate_position(name: str, value: object) -> str | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_POSITION
    ):
        return f"{name}は0〜{_MAX_POSITION}の整数である必要があります: {value!r}"
    return None


_servo_id = attrs.validators.optional(as_validator(_validate_servo_id))
_position = attrs.validators.optional(as_validator(_validate_position))


@attrs.frozen(kw_only=True)
class ServoConfig:
    """ピンセットのサーボ（[pnp.servo]）.

    役割（どの ID が Rotation / Open Close か）と Open Close の閉じ・開きの位置は、
    サーボを動かして人が判別してから書く。書くまでは None で、ピンセットは使えない。

    Attributes:
        port: シリアルポート（FEETECH URT-2 なら ``/dev/ttyUSB0`` など）
        baudrate: 通信速度 [bps]
        rotation_id: Rotation（ステップモード）のサーボ ID
        open_close_id: Open Close（位置モード）のサーボ ID
        closed_position: Open Close の閉じた位置（raw 0〜4095）
        open_position: Open Close の開いた位置（raw 0〜4095）
    """

    port: str = attrs.field(
        metadata=setting(label="シリアルポート", group=_SERVO, order=1)
    )
    baudrate: int = attrs.field(
        default=1_000_000,
        metadata=setting(label="通信速度", group=_SERVO, order=2, unit="bps"),
    )
    rotation_id: int | None = attrs.field(
        default=None,
        validator=_servo_id,
        metadata=setting(label="Rotation の ID", group=_SERVO, order=3, minimum=0),
    )
    open_close_id: int | None = attrs.field(
        default=None,
        validator=_servo_id,
        metadata=setting(label="Open Close の ID", group=_SERVO, order=4, minimum=0),
    )
    closed_position: int | None = attrs.field(
        default=None,
        validator=_position,
        metadata=setting(label="閉じた位置", group=_SERVO, order=5, minimum=0),
    )
    open_position: int | None = attrs.field(
        default=None,
        validator=_position,
        metadata=setting(label="開いた位置", group=_SERVO, order=6, minimum=0),
    )

    def __attrs_post_init__(self) -> None:
        if error := self._validate_relations():
            raise ValueError(error)

    def _validate_relations(self) -> str | None:
        if self.rotation_id is not None and self.rotation_id == self.open_close_id:
            return f"rotation_idとopen_close_idは別のIDである必要があります: {self.rotation_id}"
        if self.closed_position is None or self.open_position is None:
            return None
        span = abs(self.open_position - self.closed_position)
        if span == 0:
            return f"closed_positionとopen_positionは別の位置である必要があります: {self.closed_position}"
        if span > _MAX_OPEN_CLOSE_SPAN:
            return (
                f"closed_position（{self.closed_position}）とopen_position（{self.open_position}）の差が"
                f"{_MAX_OPEN_CLOSE_SPAN}を超えます（4095/0 の境目をまたいでいます）"
            )
        return None


@attrs.frozen
class PnpConfig:
    """部品実装の設定（[pnp]）.

    Attributes:
        servo: ピンセットのサーボ（[pnp.servo] が無ければ None）
    """

    servo: ServoConfig | None = None
