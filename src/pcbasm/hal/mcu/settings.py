"""Pnp-motion ファームで動かす XYZ ステージの設定（hal の値型。machine.toml は読まない）.

``pcbasm.machine.devices`` が ``[pnp.motion]`` の設定から組み立てて渡す。ピンの ``!`` と ``^`` は
解いたあとの値（ピンの名前と bool）で持つ。

ファームが受け付ける範囲は ``src/firmware/pnp-motion/README.md`` の「ホストが守る前提」に従い、
外れる値は作る時点で ``ValueError`` にする（外れた値を送ると MCU が shutdown する）。
"""

from __future__ import annotations

from typing import Literal

import attrs

from pcbasm.hal.mcu.tmc2209 import TmcSettings

type Kinematics = Literal["cartesian", "corexy", "inversed_corexy"]

# ファームの上限（README「ホストが守る前提」の「範囲」）
_MAX_STEP_RATE = 500_000  # [steps/s]。corexy では X と Y の和にも掛かる
_MAX_STEP_ACCEL = 1_000_000_000  # [steps/s²]
_MIN_PERIOD = 0.0005  # [s]
_MAX_LEAD = 0.1  # [s]
_MAX_SEGMENTS_AHEAD = 64
_MAX_TTL = 1.0  # [s]
_MAX_IDLE_TIMEOUT = 2**32 - 1  # [s]。ファームは 32 bit の秒数で数える


@attrs.frozen(kw_only=True)
class AxisSettings:
    """1 軸の設定.

    Attributes:
        step_pin: step のピン（``gpio11`` など）
        dir_pin: dir のピン
        invert_dir: 正の向きで dir を 0 にする（printer.cfg の ``!dir_pin``）
        enable_pin: enable のピン
        invert_enable: enable を 0 で励磁する（printer.cfg の ``!enable_pin``）
        endstop_pin: リミットスイッチのピン
        endstop_pull_up: リミットスイッチのピンをプルアップする
        endstop_invert: スイッチが押されたときにピンが 0 になる
        steps_per_mm: 1 mm あたりのステップ数（マイクロステップ単位）
        position_min: 可動域の下端 [mm]
        position_max: 可動域の上端 [mm]
        position_endstop: リミットスイッチが反応する位置 [mm]
        homing_positive: 原点出しで正の向きへ動かす
        homing_speed: 原点出しの 1 回目と、スイッチから離れる速さ [mm/s]
        second_homing_speed: 原点出しの 2 回目の速さ [mm/s]
        homing_retract: 1 回目のあとにスイッチから離れる距離 [mm]（0 なら 2 回目をしない）
        max_velocity: 最高速度 [mm/s]
        max_accel: 最大加速度 [mm/s²]
        tmc_address: TMC2209 の UART のアドレス
        tmc: TMC2209 のレジスタの設定
    """

    step_pin: str
    dir_pin: str
    invert_dir: bool
    enable_pin: str
    invert_enable: bool
    endstop_pin: str
    endstop_pull_up: bool
    endstop_invert: bool
    steps_per_mm: float
    position_min: float
    position_max: float
    position_endstop: float
    homing_positive: bool
    homing_speed: float
    second_homing_speed: float
    homing_retract: float
    max_velocity: float
    max_accel: float
    tmc_address: int
    tmc: TmcSettings

    def __attrs_post_init__(self) -> None:
        if not self.steps_per_mm > 0:
            raise ValueError(f"steps_per_mm は正にする: {self.steps_per_mm}")
        if not self.position_min < self.position_max:
            raise ValueError(
                f"position_min（{self.position_min}）は position_max（{self.position_max}）より小さくする"
            )
        if not self.position_min <= self.position_endstop <= self.position_max:
            raise ValueError(
                f"position_endstop（{self.position_endstop}）は可動域"
                f"（{self.position_min}〜{self.position_max}）の中に置く"
            )
        for name in (
            "homing_speed",
            "second_homing_speed",
            "max_velocity",
            "max_accel",
        ):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} は正にする: {getattr(self, name)}")
        if self.homing_retract < 0:
            raise ValueError(f"homing_retract は 0 以上にする: {self.homing_retract}")
        if self.to_steps(self.max_velocity) > _MAX_STEP_RATE:
            raise ValueError(
                f"max_velocity（{self.max_velocity} mm/s）がファームの上限 {_MAX_STEP_RATE} steps/s を超える"
            )
        if self.to_steps(self.max_accel) > _MAX_STEP_ACCEL:
            raise ValueError(
                f"max_accel（{self.max_accel} mm/s²）がファームの上限 {_MAX_STEP_ACCEL} steps/s² を超える"
            )

    @property
    def endstop_pressed_level(self) -> int:
        """リミットスイッチが押されている間、MCU が endstop のピンに読む値（``endstop_invert`` で 0）."""
        return int(not self.endstop_invert)

    def to_steps(self, mm: float) -> int:
        """Mm（または mm/s・mm/s²）をステップ（steps/s・steps/s²）に直す."""
        return round(mm * self.steps_per_mm)

    def to_mm(self, steps: float) -> float:
        """ステップを mm に直す."""
        return steps / self.steps_per_mm


@attrs.frozen(kw_only=True)
class McuMotionSettings:
    """MCU と 3 軸の設定.

    Attributes:
        device: MCU のシリアルポート
        kinematics: 運動学
        x: X 軸（printer.cfg の ``stepper_x``）
        y: Y 軸
        z: Z 軸
        tmc_rx_pin: TMC2209 の UART の受信ピン
        tmc_tx_pin: TMC2209 の UART の送信ピン
        tmc_rx_pull_up: 受信ピンをプルアップする
        tmc_baud: TMC2209 の UART の速さ [bit/s]
        stream_period: 区間の長さ [s]
        stream_lead: 先読みの長さ [s]
        stream_ttl: 速度指令の期限 [s]
        report_interval: 動いている間に状態を報告させる間隔 [s]
        idle_timeout: 励磁したまま止まっていたら、ファームが脱力するまでの時間 [s]（秒に丸めて送る）
    """

    device: str
    kinematics: Kinematics
    x: AxisSettings
    y: AxisSettings
    z: AxisSettings
    tmc_rx_pin: str
    tmc_tx_pin: str
    tmc_rx_pull_up: bool = False
    tmc_baud: int = 40000
    stream_period: float = 0.002
    stream_lead: float = 0.010
    stream_ttl: float = 0.200
    report_interval: float = 0.100
    idle_timeout: float = 300.0

    def __attrs_post_init__(self) -> None:
        if self.stream_period < _MIN_PERIOD:
            raise ValueError(f"stream_period は {_MIN_PERIOD} s 以上にする")
        if not self.stream_period <= self.stream_lead <= _MAX_LEAD:
            raise ValueError(
                f"stream_lead は stream_period 以上、{_MAX_LEAD} s 以下にする"
            )
        if self.stream_lead / self.stream_period > _MAX_SEGMENTS_AHEAD:
            raise ValueError(
                f"stream_lead は stream_period の {_MAX_SEGMENTS_AHEAD} 倍以下にする"
            )
        if not self.stream_lead + self.stream_period < self.stream_ttl <= _MAX_TTL:
            raise ValueError(
                f"stream_ttl は stream_lead + stream_period より長く、{_MAX_TTL} s 以下にする"
            )
        if not self.report_interval > 0:
            raise ValueError("report_interval は正にする")
        if not 1 <= self.idle_timeout <= _MAX_IDLE_TIMEOUT:
            raise ValueError(
                f"idle_timeout は 1 s 以上、{_MAX_IDLE_TIMEOUT} s 以下にする"
            )
        if self.kinematics != "cartesian":
            # corexy は X と Y の 1 ステップを、モーターの 1 ステップとして写す
            if self.x.steps_per_mm != self.y.steps_per_mm:
                raise ValueError(
                    f"{self.kinematics} では X と Y の steps_per_mm を同じにする"
                )
            # モーターは X と Y の速さの和で回る
            rate = self.x.to_steps(self.x.max_velocity) + self.y.to_steps(
                self.y.max_velocity
            )
            if rate > _MAX_STEP_RATE:
                raise ValueError(
                    f"{self.kinematics} では X と Y の max_velocity の和がファームの上限"
                    f" {_MAX_STEP_RATE} steps/s を超える"
                )

    @property
    def axes(self) -> tuple[AxisSettings, AxisSettings, AxisSettings]:
        """X・Y・Z の順の軸の設定."""
        return (self.x, self.y, self.z)
