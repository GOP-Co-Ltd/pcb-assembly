"""[pnp] 節：Pick and Place（部品実装）.

``[pnp]`` があれば、その機体は部品を実装できる（:attr:`pcbasm.config.MachineConfig.pnp`）。
空の表も受け付ける。下位表は装置の部品で分ける。

- ``servo``: ピンセットのサーボ（Feetech STS3032 ×2）のポートと役割
- ``motion``: XYZ ステージを動かす新ファーム（pnp-motion）の MCU。あれば Klipper を使わずに XYZ を動かす。
  下位表 ``motion.load_cell`` はピンセットの荷重を測るロードセル（HX711）
- ``gamepad``: コントローラのスティックのデッドゾーン
"""

from __future__ import annotations

from typing import Literal

import attrs

from pcbasm.config.fields import setting
from pcbasm.validators import (
    as_validator,
    validate_non_negative_number,
    validate_positive_int,
    validate_positive_number,
)

_SERVO = "ピンセットのサーボ"
_GAMEPAD = "コントローラ"

_MAX_SERVO_ID = 253
_FULL_TURN = 360.0
# 閉じ〜開きが半回転を超えるなら 360°/0° の境目をまたいでいる
_MAX_OPEN_CLOSE_SPAN = 180.0
# Rotation の速さの上限 [deg/s]（2 回転/秒。サーボはこれより遅い自身の上限で回る）
_MAX_ROTATION_SPEED = 720.0


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


def _validate_rotation_speed(name: str, value: object) -> str | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 < value <= _MAX_ROTATION_SPEED
    ):
        return f"{name}は0より大きく{_MAX_ROTATION_SPEED:g}以下の速さ[°/s]である必要があります: {value!r}"
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
        rotation_speed: Rotation を回す速さ [deg/s]（マシン操作パネルの ± とコントローラの両方）
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
    rotation_speed: float = attrs.field(
        default=45.0,
        validator=as_validator(_validate_rotation_speed),
        metadata=setting(
            label="Rotation の速さ", group=_SERVO, order=7, unit="°/s", minimum=0
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


type Kinematics = Literal["cartesian", "corexy", "inversed_corexy"]

# ファーム（src/firmware/pnp-motion/README.md「ホストが守る前提」）が受け付ける上限。超えると shutdown する
_MAX_STEP_RATE = 500_000  # [steps/s]。corexy では X と Y の和にも掛かる
_MAX_STEP_ACCEL = 1_000_000_000  # [steps/s²]
_MAX_MICROSTEPS = 256
# TMC2209 の UART のアドレスは MS1・MS2 のピンで 0〜3
_MAX_TMC_ADDRESS = 3
_MAX_DEADZONE = 0.5


def _validate_microsteps(name: str, value: object) -> str | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_MICROSTEPS
        or value & (value - 1)
    ):
        return f"{name}は{_MAX_MICROSTEPS}以下の2の累乗である必要があります: {value!r}"
    return None


def _validate_tmc_address(name: str, value: object) -> str | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_TMC_ADDRESS
    ):
        return f"{name}は0〜{_MAX_TMC_ADDRESS}の整数である必要があります: {value!r}"
    return None


def _validate_deadzone(name: str, value: object) -> str | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 <= value <= _MAX_DEADZONE
    ):
        return f"{name}は0以上{_MAX_DEADZONE:g}以下である必要があります: {value!r}"
    return None


_positive = as_validator(validate_positive_number)
_optional_positive = attrs.validators.optional(_positive)


@attrs.frozen(kw_only=True)
class MotionAxisConfig:
    """新ファームで動かす 1 軸（[pnp.motion.x] など）.

    項目の名前と意味は Klipper の printer.cfg の ``[stepper_*]`` と ``[tmc2209 stepper_*]`` に合わせる。
    ピンも Klipper と同じ表記で、先頭の ``!`` は論理の反転、``^`` はプルアップ（``!gpio10``、``^gpio4``）。
    ``!`` と ``^`` は、HAL に渡す設定に直すとき（:mod:`pcbasm.machine.devices`）に解く。

    Attributes:
        step_pin: step のピン
        dir_pin: dir のピン
        enable_pin: enable のピン
        endstop_pin: リミットスイッチのピン
        microsteps: マイクロステップ数（TMC2209 に書く。256 以下の 2 の累乗）
        full_steps_per_rotation: モーター 1 回転のフルステップ数
        rotation_distance: モーター 1 回転で進む距離 [mm]
        position_min: 可動域の下端 [mm]
        position_max: 可動域の上端 [mm]
        position_endstop: リミットスイッチが反応する位置 [mm]（可動域の中）
        homing_positive: 原点出しで正の向きへ動かす
        homing_speed: 原点出しの 1 回目の速さ [mm/s]
        second_homing_speed: 原点出しの 2 回目（離れてから当て直す）の速さ [mm/s]。None なら 1 回目の半分
        homing_retract: 原点出しで 1 回目のあとに離れる距離 [mm]
        max_velocity: 最高速度 [mm/s]（コントローラのスティックを倒し切ったときの速さ）
        max_accel: 最大加速度 [mm/s²]
        tmc_address: TMC2209 の UART のアドレス（0〜3）
        run_current: 動かすときの電流 [A RMS]
        hold_current: 止まっているときの電流 [A RMS]。None なら run_current と同じ
        sense_resistor: TMC2209 の検出抵抗 [Ω]
    """

    step_pin: str
    dir_pin: str
    enable_pin: str
    endstop_pin: str
    microsteps: int = attrs.field(
        default=64, validator=as_validator(_validate_microsteps)
    )
    full_steps_per_rotation: int = attrs.field(
        default=200, validator=as_validator(validate_positive_int)
    )
    rotation_distance: float = attrs.field(validator=_positive)
    position_min: float
    position_max: float
    position_endstop: float
    homing_positive: bool = False
    homing_speed: float = attrs.field(default=5.0, validator=_positive)
    second_homing_speed: float | None = attrs.field(
        default=None, validator=_optional_positive
    )
    homing_retract: float = attrs.field(
        default=5.0, validator=as_validator(validate_non_negative_number)
    )
    max_velocity: float = attrs.field(validator=_positive)
    max_accel: float = attrs.field(validator=_positive)
    tmc_address: int = attrs.field(validator=as_validator(_validate_tmc_address))
    run_current: float = attrs.field(validator=_positive)
    hold_current: float | None = attrs.field(default=None, validator=_optional_positive)
    sense_resistor: float = attrs.field(default=0.110, validator=_positive)

    def __attrs_post_init__(self) -> None:
        if error := self._validate_relations():
            raise ValueError(error)

    @property
    def steps_per_mm(self) -> float:
        """1 mm あたりのステップ数（マイクロステップ単位）."""
        return self.full_steps_per_rotation * self.microsteps / self.rotation_distance

    def _validate_relations(self) -> str | None:
        if self.position_min >= self.position_max:
            return (
                f"position_min（{self.position_min:g}）は position_max"
                f"（{self.position_max:g}）より小さい必要があります"
            )
        if not self.position_min <= self.position_endstop <= self.position_max:
            return (
                f"position_endstop（{self.position_endstop:g}）は可動域"
                f"（{self.position_min:g}〜{self.position_max:g}）の中に置く必要があります"
            )
        if self.max_velocity * self.steps_per_mm > _MAX_STEP_RATE:
            return (
                f"max_velocity（{self.max_velocity:g} mm/s）がファームの上限"
                f"（{_MAX_STEP_RATE} steps/s = {_MAX_STEP_RATE / self.steps_per_mm:g} mm/s）を超えます"
            )
        if self.max_accel * self.steps_per_mm > _MAX_STEP_ACCEL:
            return (
                f"max_accel（{self.max_accel:g} mm/s²）がファームの上限"
                f"（{_MAX_STEP_ACCEL:g} steps/s² = {_MAX_STEP_ACCEL / self.steps_per_mm:g} mm/s²）を超えます"
            )
        return None


type LoadCellGain = Literal["A-128", "A-64", "B-32"]
type SensorOrientation = Literal["normal", "inverted"]

# HX711 の出力レートは RATE ピンで 10 か 80 SPS（HX711 データシート）
_HX711_RATES = (10, 80)


def _validate_sample_rate(name: str, value: object) -> str | None:
    if isinstance(value, bool) or value not in _HX711_RATES:
        rates = " か ".join(str(rate) for rate in _HX711_RATES)
        return f"{name}は{rates}（HX711 の RATE ピンに合わせる）である必要があります: {value!r}"
    return None


@attrs.frozen(kw_only=True)
class LoadCellConfig:
    """ピンセットの荷重を測るロードセルの HX711（[pnp.motion.load_cell]）.

    HX711 は [pnp.motion] の MCU につなぐ。項目の名前と意味は Klipper の ``[load_cell]`` の hx711 に合わせる
    （``klipper/docs/Config_Reference.md``）。ピンには ``!`` と ``^`` を付けられない（データ出力のピンのプルアップは
    ファームが常に掛ける）。

    Attributes:
        sclk_pin: HX711 の SCK のピン
        dout_pin: HX711 のデータ出力のピン
        gain: 入力と増幅率（``A-128`` / ``A-64`` / ``B-32``）
        sample_rate: 出力レート [SPS]（HX711 の RATE ピンで決まる 10 か 80）
        counts_per_gram: 1 g あたりの HX711 の値（機体ごとに校正する）
        sensor_orientation: 荷重が掛かると値が減る向きにつないだなら ``inverted``
    """

    sclk_pin: str
    dout_pin: str
    gain: LoadCellGain = "A-128"
    sample_rate: int = attrs.field(
        default=80, validator=as_validator(_validate_sample_rate)
    )
    counts_per_gram: float = attrs.field(validator=_positive)
    sensor_orientation: SensorOrientation = "normal"


@attrs.frozen(kw_only=True)
class MotionConfig:
    """XYZ ステージを動かす新ファーム（pnp-motion）の MCU（[pnp.motion]）.

    この表があれば、Klipper を使わずに XYZ を動かす機体になる（[klipper] は無くてよい）。
    値は Klipper の printer.cfg と同じ意味で、kurousagi では printer.cfg から移した。

    Attributes:
        serial: MCU のシリアルポート（``/dev/serial/by-id/...`` で固定する）
        kinematics: 運動学（``cartesian`` / ``corexy`` / ``inversed_corexy``。printer.cfg の ``[printer]``）
        tmc_rx_pin: TMC2209 の UART の受信ピン（printer.cfg の ``uart_pin``）
        tmc_tx_pin: TMC2209 の UART の送信ピン（printer.cfg の ``tx_pin``）
        x: X 軸
        y: Y 軸
        z: Z 軸
        led_pin: 照明の LED のピン（MCU が設定したときから常に点け、shutdown しても消さない。MCU の reset の間だけ消える）。None なら無い
        load_cell: ピンセットの荷重を測るロードセル（[pnp.motion.load_cell] が無ければ None）
    """

    serial: str
    kinematics: Kinematics
    tmc_rx_pin: str
    tmc_tx_pin: str
    x: MotionAxisConfig
    y: MotionAxisConfig
    z: MotionAxisConfig
    led_pin: str | None = None
    load_cell: LoadCellConfig | None = None

    def __attrs_post_init__(self) -> None:
        if error := self._validate_relations():
            raise ValueError(error)

    def _validate_relations(self) -> str | None:
        if self.kinematics == "cartesian":
            return None
        # corexy は X と Y の 1 ステップを、モーターの 1 ステップとして写す
        if self.x.steps_per_mm != self.y.steps_per_mm:
            return (
                f"{self.kinematics} では X と Y の steps/mm を同じにする必要があります"
                f"（X {self.x.steps_per_mm:g}、Y {self.y.steps_per_mm:g}）"
            )
        # モーターは X と Y の速さの和で回る
        rate = (self.x.max_velocity + self.y.max_velocity) * self.x.steps_per_mm
        if rate > _MAX_STEP_RATE:
            return (
                f"{self.kinematics} では X と Y の max_velocity の和"
                f"（{self.x.max_velocity + self.y.max_velocity:g} mm/s）がファームの上限"
                f"（{_MAX_STEP_RATE} steps/s = {_MAX_STEP_RATE / self.x.steps_per_mm:g} mm/s）を超えます"
            )
        return None


_deadzone = as_validator(_validate_deadzone)


@attrs.frozen(kw_only=True)
class GamepadConfig:
    """コントローラのスティック（[pnp.gamepad]）.

    デッドゾーンより内側は 0 とみなし、外側を 0〜1 に伸ばし直す（境目で速さが飛ばない）。
    スティックは静止していても中心からわずかにずれるので、0 にすると止めたつもりで漂う。

    Attributes:
        deadzone_xy: 左スティック（XY）のデッドゾーン（中心からの距離。0〜0.5）
        deadzone_z: 右スティック（Z）のデッドゾーン（0〜0.5）
    """

    deadzone_xy: float = attrs.field(
        default=0.15,
        validator=_deadzone,
        metadata=setting(label="XY のデッドゾーン", group=_GAMEPAD, order=8, minimum=0),
    )
    deadzone_z: float = attrs.field(
        default=0.15,
        validator=_deadzone,
        metadata=setting(label="Z のデッドゾーン", group=_GAMEPAD, order=9, minimum=0),
    )


@attrs.frozen
class PnpConfig:
    """部品実装の設定（[pnp]）.

    Attributes:
        servo: ピンセットのサーボ（[pnp.servo] が無ければ None）
        motion: 新ファームの MCU と XYZ ステージ（[pnp.motion] が無ければ None で、XYZ は Klipper で動かす）
        gamepad: コントローラのスティック（[pnp.gamepad] が無ければ既定値）
    """

    servo: ServoConfig | None = None
    motion: MotionConfig | None = None
    gamepad: GamepadConfig = attrs.field(factory=GamepadConfig)
