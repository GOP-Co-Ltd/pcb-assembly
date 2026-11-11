#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Pnp-motion ファームを実機で試す、対話の試験ツール.

Klippy は動かさず、ファームと同じ commit の Klipper の klippy の部品（serialhdl、clocksync、
TMC のレジスタ定義）で MCU に直接つなぐ。printer.cfg の代わりに、PnP 機（usaremino_pnp）の
ピンと値をこのファイルに持つ。Pi 側のクライアントができるまでの、ファームの確認用。

~/klippy-env/bin/python src/firmware/pnp-motion/tools/bench.py

手順と確かめる項目は README の「実機で試す」にある。
"""

import argparse
import glob
import logging
import os
import sys
from dataclasses import dataclass

# klippy の部品。_load_klippy が --klipper の klippy/ から読み込む
reactor = serialhdl = clocksync = util = None
tmc = tmc2208 = tmc2209 = tmc_uart = None

PROTOCOL = 1  # VELOCITY_STREAM_PROTOCOL

# 機体（deploy/templates/usaremino/printer.cfg と同じ値）
STEPS_PER_MM = (
    200 * 64 / 1.5
)  # 1.8° のモーター、64 microsteps、rotation_distance 1.5 mm
RUN_CURRENT = 0.54  # A
SENSE_RESISTOR = 0.110  # Ω（Klipper の既定）
TMC_RX_PIN = "gpio9"
TMC_TX_PIN = "gpio8"
TMC_BAUD = 40000


@dataclass(frozen=True)
class Axis:
    name: str
    step_pin: str
    dir_pin: str
    invert_dir: bool  # printer.cfg の dir_pin に ! がある
    enable_pin: str  # 0 で有効（printer.cfg の !）
    endstop_pin: str  # pull up。押されると 1
    uart_address: int
    endstop_mm: float  # スイッチが押される位置
    min_mm: float  # 原点出しの後の可動域。スイッチより少し内側にする
    max_mm: float
    home_dir: int  # スイッチの向き（-1 か +1）
    max_velocity: float  # mm/s
    accel: float  # mm/s²


AXES = (
    Axis(
        "x",
        "gpio11",
        "gpio10",
        False,
        "gpio12",
        "gpio4",
        0,
        0.0,
        0.5,
        70.0,
        -1,
        10.0,
        50.0,
    ),
    Axis(
        "y",
        "gpio6",
        "gpio5",
        False,
        "gpio7",
        "gpio3",
        2,
        0.0,
        0.5,
        70.0,
        -1,
        10.0,
        50.0,
    ),
    Axis(
        "z",
        "gpio19",
        "gpio28",
        True,
        "gpio2",
        "gpio25",
        1,
        0.0,
        -45.0,
        -0.5,
        1,
        5.0,
        10.0,
    ),
)

# 原点出しの前の可動域（いまの位置を 0 として前後に）
UNHOMED_RANGE_MM = 100.0
HOMING_SPEED = 5.0  # mm/s
RETRACT_SPEED = 2.0  # mm/s
RETRACT_MM = 1.0
NUDGE_SPEED = 2.0  # mm/s

# 速度の流し方（秒）
PERIOD = 0.002
LEAD = 0.010
REPORT = 0.1
TTL = 0.2
REFRESH = 0.05  # 速度指令を送り直す間隔（TTL より十分短く）
START_MARGIN = 0.05  # 開始の時刻を、先読みよりさらに先に置く分

# oid
STEPPER = 0  # 0..2
ENDSTOP = 3  # 3..5
ENABLE = 6  # 6..8
TRSYNC = 9
TMCUART = 10
VS = 11
OID_COUNT = 12

# trsync の理由。軸ごとに分けて、どのスイッチで止まったかを知る
REASON_ENDSTOP = 10  # 10..12
ENDSTOP_SAMPLE = 0.000015
ENDSTOP_SAMPLE_COUNT = 4
ENDSTOP_REST = 0.0005


class BenchError(Exception):
    pass


class Aborted(BenchError):
    def __init__(self):
        super().__init__("s で中断しました")


def _load_klippy(klipper_dir):
    klippy = os.path.join(klipper_dir, "klippy")
    if not os.path.isdir(klippy):
        raise BenchError(
            f"klippy が見つかりません: {klippy}（先に build.sh を実行する）"
        )
    sys.path.insert(0, klippy)
    global reactor, serialhdl, clocksync, util, tmc, tmc2208, tmc2209, tmc_uart
    import clocksync
    import reactor
    import serialhdl
    import util
    from extras import tmc, tmc2208, tmc2209, tmc_uart


def _steps(mm):
    return int(round(mm * STEPS_PER_MM))


def _mm(steps):
    return steps / STEPS_PER_MM


def tmc_registers():
    """Klipper の tmc2209 が printer.cfg の値で書くのと同じレジスタの値を返す.

    Klipper と同じく、ステップは両方のエッジで打つ（Klipper はこの MCU で invert_step=-1、
    step_pulse_ticks は 100 ns、TMC の dedge=1 にする）。
    """
    fields = tmc.FieldHelper(
        tmc2209.Fields, tmc2208.SignedFields, tmc2209.FieldFormatters
    )
    # Klipper の tmc2130.TMCCurrentHelper と同じ計算（vsense=1、hold_current は run_current）
    cs = int(32.0 * (SENSE_RESISTOR + 0.020) * RUN_CURRENT * 2**0.5 / 0.18 + 0.5) - 1
    if not 0 <= cs < 31:
        raise BenchError("RUN_CURRENT が vsense=1 の範囲を外れています")
    values = [
        ("pdn_disable", 1),
        ("senddelay", 2),
        ("vsense", 1),
        ("ihold", cs),
        ("irun", cs),
        ("mres", 2),  # 64 microsteps
        ("intpol", 1),
        (
            "dedge",
            1,
        ),  # ステップを両方のエッジで打つ（config_stepper の invert_step=-1）
        ("mstep_reg_select", 1),
        # stealthchop_threshold: 999999 → 常に stealthChop
        ("tpwmthrs", 0),
        ("en_spreadcycle", 0),
        ("tcoolthrs", 0),
        ("multistep_filt", 1),
        ("toff", 3),
        ("hstrt", 5),
        ("hend", 0),
        ("tbl", 2),
        ("semin", 0),
        ("seup", 0),
        ("semax", 0),
        ("sedn", 0),
        ("seimin", 0),
        ("iholddelay", 8),
        ("pwm_ofs", 36),
        ("pwm_grad", 14),
        ("pwm_freq", 1),
        ("pwm_autoscale", 1),
        ("pwm_autograd", 1),
        ("freewheel", 0),
        ("pwm_reg", 8),
        ("pwm_lim", 12),
        ("tpowerdown", 20),
        ("sgthrs", 0),
    ]
    for name, value in values:
        fields.set_field(name, value)
    return dict(fields.registers)


class Bench:
    def __init__(self, react, device):
        self._reactor = react
        self._device = device
        self._ser = None
        self._clocksync = None
        self._freq = 0
        self._states = {}
        self._state = None  # 最後の velocity_stream_state
        self._velocity = [0.0, 0.0, 0.0]  # mm/s
        self._seq = 0
        self._streaming = False
        self._homed = [False, False, False]
        self._last_trigger = None
        self._busy = False
        self._abort = False
        self._shutdown = None
        self._stdin = ""
        self._stdin_handle = None
        self._quit_after = False
        # Klipper の tmc_uart の符号化を使う（インスタンスの状態は使わない関数）
        self._uart = object.__new__(tmc_uart.MCU_TMC_uart_bitbang)

    # ---- 接続と設定

    def connect(self):
        self._open()
        config = self._ser.send_with_response("get_config", "config")
        if config["is_config"] or config["is_shutdown"]:
            print("MCU が設定済みなので再起動します")
            self._ser.send("reset")
            self._pause(0.5)
            self._ser.disconnect()
            self._pause(3.0)
            self._open()
        self._clocksync = clocksync.ClockSync(self._reactor)
        self._clocksync.connect(self._ser)
        self._configure()
        self._init_tmc()
        for i in range(3):
            self._set_limits(i, homed=False)
            self._ser.send(f"velocity_stream_set_position oid={VS} axis={i} pos=0")
        self._query()
        print("準備ができました。help でコマンドの一覧")

    def _open(self):
        self._ser = serialhdl.SerialReader(self._reactor)
        self._ser.connect_uart(self._device, 250000)
        parser = self._ser.get_msgparser()
        protocol = parser.get_constant_int("VELOCITY_STREAM_PROTOCOL", None)
        if protocol != PROTOCOL:
            raise BenchError(
                f"pnp-motion ファーム（protocol {PROTOCOL}）ではありません: {protocol}"
            )
        version = parser.get_constant("PNP_MOTION_VERSION", "?")
        print(f"接続: {self._device}（pnp-motion {version}）")
        self._freq = parser.get_constant_float("CLOCK_FREQ")
        enums = parser.get_enumerations().get("velocity_stream_state", {})
        self._states = {v: k for k, v in enums.items()}
        self._ser.register_response(self._handle_state, "velocity_stream_state", VS)
        self._ser.register_response(self._handle_trsync, "trsync_state", TRSYNC)
        self._ser.register_response(self._handle_shutdown, "shutdown")
        self._ser.register_response(self._handle_shutdown, "is_shutdown")

    def _configure(self):
        cmds = [f"allocate_oids count={OID_COUNT}"]
        for i, a in enumerate(AXES):
            cmds += [
                f"config_stepper oid={STEPPER + i} step_pin={a.step_pin}"
                f" dir_pin={a.dir_pin} invert_step=-1 step_pulse_ticks={self._ticks(1e-7)}",
                f"config_endstop oid={ENDSTOP + i} pin={a.endstop_pin} pull_up=1",
                f"config_digital_out oid={ENABLE + i} pin={a.enable_pin}"
                " value=1 default_value=1 max_duration=0",
            ]
        invert = sum(1 << i for i, a in enumerate(AXES) if a.invert_dir)
        cmds += [
            f"config_trsync oid={TRSYNC}",
            f"config_tmcuart oid={TMCUART} rx_pin={TMC_RX_PIN} pull_up=0"
            f" tx_pin={TMC_TX_PIN} bit_time={self._ticks(1.0 / TMC_BAUD)}",
            f"config_velocity_stream oid={VS} stepper_x_oid={STEPPER}"
            f" stepper_y_oid={STEPPER + 1} stepper_z_oid={STEPPER + 2} invert_dir={invert}",
            "finalize_config crc=0",
        ]
        for cmd in cmds:
            self._ser.send(cmd)
        config = self._ser.send_with_response("get_config", "config")
        if not config["is_config"]:
            raise BenchError("MCU の設定に失敗しました")

    def _init_tmc(self):
        registers = tmc_registers()
        for a in AXES:
            before = self._tmc_read(a.uart_address, "IFCNT")
            for name, value in registers.items():
                self._tmc_write(a.uart_address, name, value)
            after = self._tmc_read(a.uart_address, "IFCNT")
            gconf = self._tmc_read(a.uart_address, "GCONF")
            if (after - before) & 0xFF != len(registers) or gconf != registers["GCONF"]:
                raise BenchError(f"{a.name}: TMC2209 への書き込みを確かめられません")
            print(f"{a.name}: TMC2209 を設定しました（{RUN_CURRENT} A、64 microsteps）")

    def _tmc_send(self, data, read):
        cmd = self._ser.get_msgparser().lookup_command(
            "tmcuart_send oid=%c write=%*s read=%c"
        )
        retry = serialhdl.SerialRetryCommand(self._ser, "tmcuart_response", TMCUART)
        queue = self._ser.get_default_command_queue()
        return retry.get_response([cmd.encode([TMCUART, data, read])], queue)

    def _tmc_read(self, addr, name):
        reg = tmc2209.Registers[name]
        # Klipper と同じく、読み出しの失敗（CRC の不一致など）は 5 回まで試す
        for _ in range(5):
            params = self._tmc_send(self._uart._encode_read(0xF5, addr, reg), 10)
            value = self._uart._decode_read(reg, params["read"])
            if value is not None:
                break
        if value is None:
            raise BenchError(
                f"TMC2209（address {addr}）が応答しません（モーター電源が入っているか）"
            )
        return value

    def _tmc_write(self, addr, name, value):
        reg = tmc2209.Registers[name]
        self._tmc_send(self._uart._encode_write(0xF5, addr, reg | 0x80, value), 0)

    # ---- 応答

    def _handle_state(self, params):
        old = self._state
        self._state = params
        if old is None or old["state"] != params["state"]:
            print(f"\n[{self._state_name()}] {self._position_text()}")

    def _handle_trsync(self, params):
        if params["can_trigger"]:
            return
        axis = params["trigger_reason"] - REASON_ENDSTOP
        self._last_trigger = axis
        name = AXES[axis].name if 0 <= axis < 3 else f"理由 {params['trigger_reason']}"
        print(f"\n[trsync] {name} のリミットスイッチで止まりました")

    def _handle_shutdown(self, params):
        self._shutdown = params.get("static_string_id", params)
        self._streaming = False
        print(f"\n[MCU shutdown] {self._shutdown}（bench を起動し直す）")

    # ---- 小物

    def _ticks(self, seconds):
        return int(seconds * self._freq)

    def _now(self):
        return self._clocksync.get_clock(self._reactor.monotonic())

    def _pause(self, seconds):
        self._reactor.pause(self._reactor.monotonic() + seconds)

    def _wait(self, cond, timeout, abortable=True):
        end = self._reactor.monotonic() + timeout
        while not cond():
            if self._reactor.monotonic() > end or self._shutdown:
                return False
            if abortable and self._abort:
                raise Aborted()
            self._pause(0.02)
        return True

    def _state_name(self):
        if self._state is None:
            return "?"
        return self._states.get(self._state["state"], str(self._state["state"]))

    def _position_text(self):
        s = self._state
        if s is None:
            return ""
        pos = " ".join(f"{a.name}={_mm(s[a.name]):8.3f}" for a in AXES)
        vel = " ".join(f"v{a.name}={_mm(s['v' + a.name]):6.2f}" for a in AXES)
        homed = "".join(a.name if h else "-" for a, h in zip(AXES, self._homed))
        return f"{pos} mm  {vel} mm/s  原点:{homed}"

    def _query(self):
        self._ser.send(f"velocity_stream_query oid={VS}")
        self._pause(0.05)

    def _request(self, msg, response, oid):
        """応答を待つコマンド（応答は oid ごとに振り分けられる）."""
        cmd = self._ser.get_msgparser().create_command(msg)
        retry = serialhdl.SerialRetryCommand(self._ser, response, oid)
        return retry.get_response([cmd], self._ser.get_default_command_queue())

    def _pressed(self):
        pressed = set()
        for i in range(3):
            oid = ENDSTOP + i
            params = self._request(
                f"endstop_query_state oid={oid}", "endstop_state", oid
            )
            if params["pin_value"]:
                pressed.add(i)
        return pressed

    def _set_limits(self, i, homed):
        a = AXES[i]
        if homed:
            low, high = a.min_mm, a.max_mm
        else:
            low, high = -UNHOMED_RANGE_MM, UNHOMED_RANGE_MM
        self._ser.send(
            f"velocity_stream_set_limits oid={VS} axis={i} min_pos={_steps(low)}"
            f" max_pos={_steps(high)} max_velocity={_steps(a.max_velocity)}"
            f" accel={_steps(a.accel)}"
        )

    def _enable(self, on):
        for i in range(3):
            self._ser.send(
                f"update_digital_out oid={ENABLE + i} value={0 if on else 1}"
            )

    # ---- 流す・止める

    def _arm(self, skip):
        """Trsync に 3 軸の stepper と velocity_stream を登録し、押されていないスイッチを張る."""
        # 前回張ったままの endstop が、登録の途中で trsync を消費しないように
        self._disarm()
        self._ser.send(
            f"trsync_start oid={TRSYNC} report_clock=0 report_ticks=0 expire_reason=0"
        )
        for i in range(3):
            self._ser.send(
                f"stepper_stop_on_trigger oid={STEPPER + i} trsync_oid={TRSYNC}"
            )
        self._ser.send(f"velocity_stream_stop_on_trigger oid={VS} trsync_oid={TRSYNC}")
        clock = (self._now() + self._ticks(START_MARGIN)) & 0xFFFFFFFF
        for i in range(3):
            if i in skip:
                continue
            self._ser.send(
                f"endstop_home oid={ENDSTOP + i} clock={clock}"
                f" sample_ticks={self._ticks(ENDSTOP_SAMPLE)}"
                f" sample_count={ENDSTOP_SAMPLE_COUNT}"
                f" rest_ticks={self._ticks(ENDSTOP_REST)} pin_value=1"
                f" trsync_oid={TRSYNC} trigger_reason={REASON_ENDSTOP + i}"
            )

    def _disarm(self):
        for i in range(3):
            self._ser.send(
                f"endstop_home oid={ENDSTOP + i} clock=0 sample_ticks=0 sample_count=0"
                f" rest_ticks=0 pin_value=0 trsync_oid={TRSYNC} trigger_reason=0"
            )

    def _start(self, skip=()):
        pressed = self._pressed()
        for i in pressed - set(skip):
            print(
                f"{AXES[i].name} のリミットスイッチが押されたままなので、張らずに動かします"
            )
        self._enable(True)
        self._last_trigger = None
        self._arm(set(skip) | pressed)
        clock = (self._now() + self._ticks(LEAD + START_MARGIN)) & 0xFFFFFFFF
        self._ser.send(
            f"velocity_stream_start oid={VS} clock={clock}"
            f" period_ticks={self._ticks(PERIOD)} lead_ticks={self._ticks(LEAD)}"
            f" report_ticks={self._ticks(REPORT)} ttl_ticks={self._ticks(TTL)}"
        )
        self._streaming = True
        self._send_velocity()

        def streaming():
            self._query()
            return self._state_name() == "stream"

        if not self._wait(streaming, 1.0, abortable=False):
            raise BenchError("開始できません")

    def _send_velocity(self):
        self._seq = (self._seq + 1) & 0xFFFF
        vx, vy, vz = (_steps(v) for v in self._velocity)
        expire = (self._now() + self._ticks(TTL)) & 0xFFFFFFFF
        self._ser.send(
            f"velocity_stream_set_velocity oid={VS} seq={self._seq}"
            f" vx={vx} vy={vy} vz={vz} expire_clock={expire}"
        )

    def _refresh(self, eventtime):
        if self._streaming and self._state_name() == "stream":
            self._send_velocity()
        return eventtime + REFRESH

    def _stop(self):
        """減速して止め、IDLE か CONTACT になるまで待つ."""
        self._velocity = [0.0, 0.0, 0.0]
        self._streaming = False
        self._ser.send(f"velocity_stream_stop oid={VS}")

        def stopped():
            self._query()
            return self._state_name() in ("idle", "contact")

        if not self._wait(stopped, 5.0, abortable=False) and not self._shutdown:
            raise BenchError("止まりません")
        self._disarm()

    def _run_for(self, i, velocity, distance, skip=()):
        """軸 i を velocity で distance だけ動かす（時間で測るので目安）."""
        self._stop()
        self._velocity = [0.0, 0.0, 0.0]
        self._velocity[i] = velocity
        self._start(skip)
        self._send_velocity()
        hit = self._wait(
            lambda: self._state_name() == "contact", abs(distance / velocity)
        )
        self._stop()
        if hit:
            raise BenchError("リミットスイッチで止まりました")

    # ---- コマンド

    def cmd_v(self, args):
        """v VX VY VZ: 速度（mm/s）で動かし続ける。原点出しの前の軸は 0 だけ."""
        velocity = [float(x) for x in args]
        if len(velocity) != 3:
            raise BenchError("v VX VY VZ")
        for a, v, homed in zip(AXES, velocity, self._homed):
            if not abs(v) <= a.max_velocity:
                raise BenchError(f"{a.name} の速度は ±{a.max_velocity} mm/s まで")
            if v and not homed:
                raise BenchError(f"{a.name} は原点出しの前です（home か nudge を使う）")
        # 開始より先に設定する（接触のあとに古い速度で流し始めないように）
        self._velocity = velocity
        if self._state_name() != "stream" or not self._streaming:
            self._start()
        self._send_velocity()

    def cmd_s(self, args):
        """s: 減速して止める."""
        self._stop()

    def cmd_q(self, args):
        """q: 状態と位置を表示する."""
        self._query()
        pressed = self._pressed()
        names = ",".join(AXES[i].name for i in sorted(pressed)) or "なし"
        print(
            f"[{self._state_name()}] {self._position_text()}  押されたスイッチ:{names}"
        )

    def cmd_nudge(self, args):
        """nudge AXIS MM: 軸を MM だけ 2 mm/s で動かす（時間で測る目安。向きの確認に）."""
        i = self._axis(args[0])
        distance = float(args[1])
        if not 0 < abs(distance) <= 10:
            raise BenchError("nudge は 0 より大きく 10 mm まで")
        self._run_for(i, NUDGE_SPEED if distance > 0 else -NUDGE_SPEED, distance)

    def cmd_home(self, args):
        """Home [AXIS...]: 原点出し（既定は z x y の順）."""
        for i in [self._axis(x) for x in args] or [2, 0, 1]:
            self._home(i)

    def _home(self, i):
        a = AXES[i]
        self._stop()
        self._homed[i] = False
        # 可動域を広げ、いまの位置を 0 にしてスイッチへ向かう
        self._set_limits(i, homed=False)
        self._ser.send(f"velocity_stream_set_position oid={VS} axis={i} pos=0")
        if i in self._pressed():
            print(f"{a.name}: スイッチが押されているので、先に離れます")
            self._run_for(i, -a.home_dir * RETRACT_SPEED, 3 * RETRACT_MM, skip={i})
            if i in self._pressed():
                raise BenchError(f"{a.name}: スイッチから離れられません")
        print(f"{a.name}: スイッチへ向かいます（{HOMING_SPEED} mm/s）")
        self._velocity = [0.0, 0.0, 0.0]
        self._velocity[i] = a.home_dir * HOMING_SPEED
        self._start()
        timeout = (abs(a.max_mm - a.min_mm) + 10) / HOMING_SPEED
        if not self._wait(lambda: self._state_name() == "contact", timeout):
            self._stop()
            raise BenchError(f"{a.name}: スイッチに届きません（向きと配線を確かめる）")
        self._streaming = False
        # trsync_state は velocity_stream_state と別に届く
        self._wait(lambda: self._last_trigger is not None, 0.5, abortable=False)
        if self._last_trigger != i:
            raise BenchError(f"{a.name}: 別のスイッチで止まりました")
        self._ser.send(
            f"velocity_stream_set_position oid={VS} axis={i} pos={_steps(a.endstop_mm)}"
        )
        # 可動域を広げたまま退避してから、可動域を戻す
        self._run_for(i, -a.home_dir * RETRACT_SPEED, RETRACT_MM, skip={i})
        self._query()
        pos = _mm(self._state[a.name])
        if i in self._pressed() or not a.min_mm <= pos <= a.max_mm:
            raise BenchError(f"{a.name}: 退避が足りません（{pos:.3f} mm）")
        self._set_limits(i, homed=True)
        self._homed[i] = True
        self._query()
        print(f"{a.name}: 原点出しを終えました  {self._position_text()}")

    def cmd_on(self, args):
        """on: モーターを励磁する."""
        self._enable(True)

    def cmd_off(self, args):
        """off: 止めてからモーターの励磁を切る（原点は失う）."""
        self._stop()
        self._enable(False)
        self._homed = [False, False, False]
        for i in range(3):
            self._set_limits(i, homed=False)
            self._ser.send(f"velocity_stream_set_position oid={VS} axis={i} pos=0")

    def cmd_help(self, args):
        """help: この一覧."""
        for name in sorted(n for n in dir(self) if n.startswith("cmd_")):
            doc = getattr(self, name).__doc__
            print("  " + doc[0].lower() + doc[1:])
        print("  quit: 止めて励磁を切り、終わる")
        print("  動いている最中は s だけを受け付ける（他のコマンドは終わってから）")

    def _axis(self, name):
        for i, a in enumerate(AXES):
            if a.name == name.lower():
                return i
        raise BenchError(f"軸は x・y・z: {name}")

    # ---- 入力

    def start_input(self):
        self._reactor.register_timer(self._refresh, self._reactor.NOW)
        fd = sys.stdin.fileno()
        util.set_nonblock(fd)
        self._stdin_handle = self._reactor.register_fd(fd, self._on_stdin)
        print("> ", end="", flush=True)

    def _on_stdin(self, eventtime):
        data = os.read(sys.stdin.fileno(), 4096).decode(errors="replace")
        if not data:
            # 入力の終わり。実行中のコマンドがあれば中断してから終わる
            self._reactor.unregister_fd(self._stdin_handle)
            if self._busy:
                self._abort = self._quit_after = True
            else:
                self._quit()
            return
        self._stdin += data
        *lines, self._stdin = self._stdin.split("\n")
        for line in lines:
            self._run_line(line.split())

    def _run_line(self, parts):
        if not parts:
            return
        name, args = parts[0].lower(), parts[1:]
        if self._busy:
            if name == "s":
                self._abort = True
            else:
                print("実行中です（s で止める）")
            return
        if self._shutdown:
            print("MCU が shutdown しています。quit して起動し直してください")
            if name != "quit":
                return
        if name == "quit":
            self._quit()
            return
        handler = getattr(self, "cmd_" + name, None)
        if handler is None:
            print(f"知らないコマンド: {name}（help）")
            return
        self._busy = True
        self._abort = False
        try:
            handler(args)
        except (BenchError, serialhdl.error, ValueError, IndexError) as e:
            print(f"エラー: {e}")
            # 流れたまま _refresh が速度を送り続けないように止める
            try:
                self._abort = False
                if not self._shutdown:
                    self._stop()
            except (BenchError, serialhdl.error) as e2:
                print(f"エラー: {e2}")
        finally:
            self._busy = False
            self._abort = False
            if self._quit_after:
                self._quit()
            else:
                print("> ", end="", flush=True)

    def _quit(self):
        self._busy = True
        try:
            if not self._shutdown:
                self._stop()
                self._enable(False)
                self._pause(0.1)
        except (BenchError, serialhdl.error) as e:
            print(f"エラー: {e}")
        finally:
            self.close()
            self._reactor.end()

    def close(self):
        """シリアルを切る（受信のスレッドが残ってプロセスが終わらなくならないように）."""
        if self._ser is not None:
            self._ser.disconnect()
            self._ser = None


def _default_device():
    devices = glob.glob("/dev/serial/by-id/usb-Klipper_rp2040_*")
    if len(devices) != 1:
        raise BenchError(
            f"SKR Pico を 1 台に絞れません（--device で指定する）: {devices}"
        )
    return devices[0]


def main():
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--klipper",
        default=os.path.join(cache, "pcbasm", "pnp-motion", "klipper"),
        help="build.sh が取得した Klipper（既定: build.sh の既定の場所）",
    )
    parser.add_argument(
        "--device", help="MCU のシリアル（既定: /dev/serial/by-id から探す）"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    try:
        _load_klippy(args.klipper)
        react = reactor.Reactor()
        bench = Bench(react, args.device or _default_device())

        def startup(eventtime):
            try:
                bench.connect()
            except BenchError as e:
                print(f"エラー: {e}")
                react.end()
                return
            bench.start_input()

        react.register_callback(startup)
        try:
            react.run()
        except KeyboardInterrupt:
            # 止める命令は送らない。速度指令の期限が切れて MCU が自分で止まる（ウォッチドッグ）
            print("\n中断: 速度指令を送るのをやめました。MCU は期限が切れて止まります")
        finally:
            bench.close()
    except BenchError as e:
        sys.exit(f"エラー: {e}")


if __name__ == "__main__":
    main()
