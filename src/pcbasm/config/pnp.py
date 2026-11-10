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
_FULL_TURN = 360.0
# 閉じ〜開きが半回転を超えるなら 360°/0° の境目をまたいでいる
_MAX_OPEN_CLOSE_SPAN = 180.0


def open_close_span_error(closed: float, opened: float) -> str | None:
    """Open Close の閉じ切りと開き切りの角度 [deg] の組が使えない理由（使えるなら None）.

    2 つが同じ角度なら開閉できない。差が半回転（180°）を超えるなら 360°/0° の境目をまたいでおり、
    サーボは短い側の逆向きに回ってしまう（サーボの角度制限も下限 < 上限でしか書けない）。
    設定の読み込みと、サーボ設定ジョブの両方が使う。
    """
    span = abs(opened - closed)
    if span == 0:
        return f"閉じ切りと開き切りが同じ角度（{closed:g}°）です"
    if span > _MAX_OPEN_CLOSE_SPAN:
        return (
            f"閉じ切り（{closed:g}°）と開き切り（{opened:g}°）の差が {_MAX_OPEN_CLOSE_SPAN:g}° を超え、"
            "サーボの 360°/0° の境目をまたいでいます"
        )
    return None


def _validate_servo_id(name: str, value: object) -> str | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_SERVO_ID
    ):
        return f"{name}は0〜{_MAX_SERVO_ID}の整数である必要があります: {value!r}"
    return None


def _validate_angle(name: str, value: object) -> str | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 <= value < _FULL_TURN
    ):
        return f"{name}は0以上{_FULL_TURN:g}未満の角度である必要があります: {value!r}"
    return None


_servo_id = attrs.validators.optional(as_validator(_validate_servo_id))
_angle = attrs.validators.optional(as_validator(_validate_angle))


@attrs.frozen(kw_only=True)
class ServoConfig:
    """ピンセットのサーボ（[pnp.servo]）.

    役割（どの ID が Rotation / Open Close か）と Open Close の閉じ切り・開き切りの角度は、
    サーボ設定ジョブがサーボを動かして人が判別してから書く。書くまでは None で、ピンセットは使えない。

    ピンセットは逆作動式で、Open Close を開き切りより先へ回すと機構を壊す。サーボ設定ジョブは
    閉じ切り〜開き切りをサーボ自身の角度制限（EEPROM）にも書くので、ここで範囲を広げても
    サーボはその外へ動かない（広げるならサーボ設定ジョブをやり直す）。

    Attributes:
        port: シリアルポート（FEETECH URT-2 なら ``/dev/ttyUSB0`` など）
        baudrate: 通信速度 [bps]
        rotation_id: Rotation（ステップモード）のサーボ ID
        open_close_id: Open Close（位置モード）のサーボ ID
        closed_angle: Open Close の閉じ切りの角度（サーボの絶対角 [deg]、0〜360）
        open_angle: Open Close の開き切りの角度（サーボの絶対角 [deg]、0〜360。閉じ切りより小さくてもよい）
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
    closed_angle: float | None = attrs.field(
        default=None,
        validator=_angle,
        metadata=setting(
            label="閉じ切りの角度", group=_SERVO, order=5, unit="°", minimum=0
        ),
    )
    open_angle: float | None = attrs.field(
        default=None,
        validator=_angle,
        metadata=setting(
            label="開き切りの角度", group=_SERVO, order=6, unit="°", minimum=0
        ),
    )

    def __attrs_post_init__(self) -> None:
        if error := self._validate_relations():
            raise ValueError(error)

    def _validate_relations(self) -> str | None:
        if self.rotation_id is not None and self.rotation_id == self.open_close_id:
            return f"rotation_idとopen_close_idは別のIDである必要があります: {self.rotation_id}"
        if self.closed_angle is None or self.open_angle is None:
            return None
        if error := open_close_span_error(self.closed_angle, self.open_angle):
            return f"closed_angle / open_angle: {error}"
        return None


@attrs.frozen
class PnpConfig:
    """部品実装の設定（[pnp]）.

    Attributes:
        servo: ピンセットのサーボ（[pnp.servo] が無ければ None）
    """

    servo: ServoConfig | None = None
