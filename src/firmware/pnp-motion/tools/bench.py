#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Pnp-motion ファームを実機で試す、対話の試験ツール.

Klippy は動かさず、submodule の Klipper（ファームと同じ commit）の klippy の部品で MCU
に直接つなぐ。 ピン・ステップ数・電流・速度・可動域・原点出しは、機体の printer.cfg（既定は
config/printer.cfg）から読む。 Pi 側のクライアントができるまでの、ファームの確認用。

~/klippy-env/bin/python src/firmware/pnp-motion/tools/bench.py

手順と確かめる項目は README の「実機で試す」にある。
"""

import argparse
import configparser
import glob
import logging
import os
import sys
from dataclasses import dataclass

# klippy の部品。_load_klippy が --klipper の klippy/ から読み込む
reactor = serialhdl = clocksync = util = None
tmc = tmc2208 = tmc2209 = tmc_uart = None

PROTOCOL = 1  # VELOCITY_STREAM_PROTOCOL
REPO_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
)

# 原点出しの前の可動域とスイッチを探す距離（可動域の長さの倍数。Klipper の原点出しと同じ）
HOMING_SPAN = 1.5
# 対応する kinematics（ファームの列挙 velocity_stream_kinematics の名前）
KINEMATICS = ("cartesian", "corexy", "inversed_corexy")
TMC_BAUD = 40000
TMC_FREQUENCY = 12000000.0  # Klipper の tmc2209.TMC_FREQUENCY
MICROSTEP_MRES = {256: 0, 128: 1, 64: 2, 32: 3, 16: 4, 8: 5, 4: 6, 2: 7, 1: 8}


@dataclass(frozen=True)
class Pin:
    name: str
    invert: bool  # !
    pullup: bool  # ^


@dataclass(frozen=True)
class Axis:
    name: str
    step_pin: str
    dir_pin: Pin
    enable_pin: Pin
    endstop_pin: Pin
    steps_per_mm: float
    microsteps: int
    full_steps: int
    rotation_distance: float
    position_min: float
    position_max: float
    position_endstop: float
    home_dir: int  # スイッチの向き（-1 か +1）
    homing_speed: float  # mm/s
    second_homing_speed: float
    retract_speed: float
    retract_dist: float
    max_velocity: float  # mm/s
    accel: float  # mm/s²
    uart_address: int
    run_current: float
    hold_current: float
    sense_resistor: float
    stealthchop_threshold: float | None
    interpolate: bool

    def steps(self, mm):
        return int(round(mm * self.steps_per_mm))

    def mm(self, steps):
        return steps / self.steps_per_mm

    @property
    def endstop_trigger_value(self):
        """スイッチが押されたときのピンの値（endstop_pin が反転（!）なら 0）."""
        return 0 if self.endstop_pin.invert else 1


@dataclass(frozen=True)
class Machine:
    kinematics: str
    axes: tuple
    tmc_rx: Pin
    tmc_tx: str


# 読み込んだ機体。main が load_machine の結果を置く
AXES = ()
KINEMATICS_NAME = "cartesian"
TMC_RX = TMC_TX = None

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
            f"klippy が見つかりません: {klippy}"
            "（git submodule update --init src/firmware/pnp-motion/klipper）"
        )
    sys.path.insert(0, klippy)
    global reactor, serialhdl, clocksync, util, tmc, tmc2208, tmc2209, tmc_uart
    import clocksync
    import reactor
    import serialhdl
    import util
    from extras import tmc, tmc2208, tmc2209, tmc_uart


def _parse_pin(text):
    """Klipper のピンの書き方（前置きの ^ と !）を読む."""
    name = text.strip()
    invert = pullup = False
    while name and name[0] in "^~!":
        if name[0] == "!":
            invert = True
        elif name[0] == "^":
            pullup = True
        name = name[1:].strip()
    if ":" in name:
        raise BenchError(f"MCU 名つきのピンには対応していません: {text}")
    return Pin(name, invert, pullup)


def _read_printer_cfg(path):
    """printer.cfg を読む。末尾の SAVE_CONFIG（#*# の行）は Klipper と同じく上書きとして読む."""
    text = open(path, encoding="utf-8").read()
    marker = "#*# <---------------------- SAVE_CONFIG ---------------------->"
    body, _, saved = text.partition(marker)
    cfg = configparser.RawConfigParser(
        strict=False, inline_comment_prefixes=("#", ";"), delimiters=(":", "=")
    )
    cfg.read_string(body)
    lines = [line[3:].strip() for line in saved.splitlines() if line.startswith("#*#")]
    # 見出し（DO NOT EDIT ... の行）を飛ばし、最初の節から読む
    while lines and not lines[0].startswith("["):
        lines.pop(0)
    cfg.read_string("\n".join(lines))
    return cfg


def _home_dir(name, section, position_endstop, position_min, position_max):
    """原点出しの向き。Klipper の stepper.PrinterRail と同じ規則で決める."""
    if section.get("homing_positive_dir") is not None:
        return 1 if section.getboolean("homing_positive_dir") else -1
    if position_endstop <= position_min:
        return -1
    if position_endstop >= position_max:
        return 1
    raise BenchError(f"stepper_{name}: homing_positive_dir を決められません")


def load_machine(path):
    """printer.cfg から、bench が使う 3 軸と TMC の値を読む."""
    if not os.path.exists(path):
        raise BenchError(
            f"printer.cfg が見つかりません: {path}（--printer-cfg で指定する）"
        )
    cfg = _read_printer_cfg(path)
    printer = cfg["printer"]
    kinematics = printer.get("kinematics")
    if kinematics not in KINEMATICS:
        raise BenchError(
            f"kinematics {kinematics} には対応していません（{'、'.join(KINEMATICS)}）"
        )
    axes = []
    uart = set()
    for name in "xyz":
        st = cfg[f"stepper_{name}"]
        drv_name = f"tmc2209 stepper_{name}"
        if not cfg.has_section(drv_name):
            raise BenchError(f"[{drv_name}] がありません（TMC2209 の UART だけに対応）")
        drv = cfg[drv_name]
        microsteps = st.getint("microsteps")
        if microsteps not in MICROSTEP_MRES:
            raise BenchError(f"stepper_{name}: microsteps が不正です: {microsteps}")
        full_steps = st.getint("full_steps_per_rotation", 200)
        rotation_distance = st.getfloat("rotation_distance")
        if "gear_ratio" in st:
            raise BenchError(f"stepper_{name}: gear_ratio には対応していません")
        position_endstop = st.getfloat("position_endstop")
        position_min = st.getfloat("position_min", 0.0)
        position_max = st.getfloat("position_max")
        home_dir = _home_dir(name, st, position_endstop, position_min, position_max)
        # 既定値は Klipper の stepper.PrinterRail と同じ
        homing_speed = st.getfloat("homing_speed", 5.0)
        if name == "z":
            max_velocity = printer.getfloat(
                "max_z_velocity", printer.getfloat("max_velocity")
            )
            accel = printer.getfloat("max_z_accel", printer.getfloat("max_accel"))
        else:
            max_velocity = printer.getfloat("max_velocity")
            accel = printer.getfloat("max_accel")
        steps_per_mm = full_steps * microsteps / rotation_distance
        run_current = drv.getfloat("run_current")
        threshold = drv.get("stealthchop_threshold")
        axes.append(
            Axis(
                name=name,
                step_pin=_parse_pin(st.get("step_pin")).name,
                dir_pin=_parse_pin(st.get("dir_pin")),
                enable_pin=_parse_pin(st.get("enable_pin")),
                endstop_pin=_parse_pin(st.get("endstop_pin")),
                steps_per_mm=steps_per_mm,
                microsteps=microsteps,
                full_steps=full_steps,
                rotation_distance=rotation_distance,
                position_min=position_min,
                position_max=position_max,
                position_endstop=position_endstop,
                home_dir=home_dir,
                homing_speed=homing_speed,
                second_homing_speed=st.getfloat(
                    "second_homing_speed", homing_speed / 2
                ),
                retract_speed=st.getfloat("homing_retract_speed", homing_speed),
                retract_dist=st.getfloat("homing_retract_dist", 5.0),
                max_velocity=max_velocity,
                accel=accel,
                uart_address=drv.getint("uart_address", 0),
                run_current=run_current,
                hold_current=min(drv.getfloat("hold_current", 2.0), run_current),
                sense_resistor=drv.getfloat("sense_resistor", 0.110),
                stealthchop_threshold=None if threshold is None else float(threshold),
                interpolate=drv.getboolean("interpolate", True),
            )
        )
        uart.add((drv.get("uart_pin"), drv.get("tx_pin", drv.get("uart_pin"))))
    if len(uart) != 1:
        raise BenchError("3 軸の TMC2209 が同じ UART のピンを使っていません")
    rx, tx = uart.pop()
    # corexy のファームは、直交座標の 1 ステップをモーターの 1 ステップとして写す
    if kinematics != "cartesian" and axes[0].steps_per_mm != axes[1].steps_per_mm:
        raise BenchError(
            f"{kinematics} では stepper_x と stepper_y の 1 mm あたりのステップ数を同じにする"
        )
    return Machine(kinematics, tuple(axes), _parse_pin(rx), _parse_pin(tx).name)


def describe_machine(path, machine):
    """読んだ値を、動かす前に確かめられるように並べる."""
    lines = [f"printer.cfg: {path}（kinematics: {machine.kinematics}）"]
    if machine.kinematics != "cartesian":
        lines.append(
            "  X・Y は直交座標の軸。stepper_x・stepper_y の値（ピン・向き・電流）は各モーターのもの"
        )
    for a in machine.axes:
        lines.append(
            f"  {a.name}: {a.steps_per_mm:.3f} steps/mm（{a.full_steps}×{a.microsteps}/"
            f"{a.rotation_distance} mm）、dir {'反転' if a.dir_pin.invert else 'そのまま'}、"
            f"速さ ≤{a.max_velocity} mm/s、加速度 {a.accel} mm/s²"
        )
        lines.append(
            f"     原点 {a.position_endstop} mm（{'+' if a.home_dir > 0 else '-'} 向き、"
            f"{a.homing_speed}・2 回目 {a.second_homing_speed} mm/s、"
            f"退避 {a.retract_dist} mm を {a.retract_speed} mm/s）、"
            f"可動域 {a.position_min}〜{a.position_max} mm、"
            f"電流 {a.run_current} A（address {a.uart_address}）"
        )
    return "\n".join(lines)


def _current_bits(axis):
    """Klipper の tmc2130.TMCCurrentHelper と同じ電流の計算（vsense, irun, ihold）."""

    def bits(current, vsense):
        vref = 0.18 if vsense else 0.32
        cs = (
            int(32.0 * (axis.sense_resistor + 0.020) * current * 2**0.5 / vref + 0.5)
            - 1
        )
        return max(0, min(31, cs))

    def current_from(cs, vsense):
        vref = 0.18 if vsense else 0.32
        return (cs + 1) * vref / (32.0 * (axis.sense_resistor + 0.020) * 2**0.5)

    vsense = True
    irun = bits(axis.run_current, True)
    if irun == 31 and current_from(irun, True) < axis.run_current:
        irun2 = bits(axis.run_current, False)
        if abs(axis.run_current - current_from(irun2, False)) < abs(
            axis.run_current - current_from(irun, True)
        ):
            vsense, irun = False, irun2
    return vsense, irun, bits(axis.hold_current, vsense)


def _tpwmthrs(axis):
    """Klipper の tmc.TMCtstepHelper と同じ、stealthChop を切り替える速さの TSTEP."""
    velocity = axis.stealthchop_threshold
    if not velocity or velocity <= 0:
        return 0xFFFFF
    mres = MICROSTEP_MRES[axis.microsteps]
    step_dist = axis.rotation_distance / (axis.full_steps * axis.microsteps)
    threshold = int(TMC_FREQUENCY * step_dist / (1 << mres) / velocity + 0.5)
    return max(0, min(0xFFFFF, threshold))


def tmc_registers(axis):
    """Klipper の tmc2209 が printer.cfg の値で書くのと同じレジスタの値を返す.

    Klipper と同じく、ステップは両方のエッジで打つ（Klipper はこの MCU で invert_step=-1、
    step_pulse_ticks は 100 ns、TMC の dedge=1 にする）。
    """
    fields = tmc.FieldHelper(
        tmc2209.Fields, tmc2208.SignedFields, tmc2209.FieldFormatters
    )
    vsense, irun, ihold = _current_bits(axis)
    stealth = axis.stealthchop_threshold is not None
    values = [
        ("pdn_disable", 1),
        ("senddelay", 2),
        ("vsense", int(vsense)),
        ("ihold", ihold),
        ("irun", irun),
        ("mres", MICROSTEP_MRES[axis.microsteps]),
        ("intpol", int(axis.interpolate)),
        ("dedge", 1),
        ("mstep_reg_select", 1),
        ("tpwmthrs", _tpwmthrs(axis) if stealth else 0xFFFFF),
        ("en_spreadcycle", 0 if stealth else 1),
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
        # MCU が数秒ごとに送る統計。受け口がないと klippy が警告を出し、入力の途中に割り込む
        self._ser.register_response(lambda params: None, "stats")

    def _configure(self):
        cmds = [f"allocate_oids count={OID_COUNT}"]
        for i, a in enumerate(AXES):
            cmds += [
                f"config_stepper oid={STEPPER + i} step_pin={a.step_pin}"
                f" dir_pin={a.dir_pin.name} invert_step=-1 step_pulse_ticks={self._ticks(1e-7)}",
                f"config_endstop oid={ENDSTOP + i} pin={a.endstop_pin.name}"
                f" pull_up={int(a.endstop_pin.pullup)}",
                # 励磁を切った値で始め、shutdown でも切る
                f"config_digital_out oid={ENABLE + i} pin={a.enable_pin.name}"
                f" value={self._enable_value(a, False)}"
                f" default_value={self._enable_value(a, False)} max_duration=0",
            ]
        invert = sum(1 << i for i, a in enumerate(AXES) if a.dir_pin.invert)
        kinematics = self._ser.get_msgparser().get_enumerations()[
            "velocity_stream_kinematics"
        ][KINEMATICS_NAME]
        cmds += [
            f"config_trsync oid={TRSYNC}",
            f"config_tmcuart oid={TMCUART} rx_pin={TMC_RX.name}"
            f" pull_up={int(TMC_RX.pullup)} tx_pin={TMC_TX}"
            f" bit_time={self._ticks(1.0 / TMC_BAUD)}",
            f"config_velocity_stream oid={VS} stepper_x_oid={STEPPER}"
            f" stepper_y_oid={STEPPER + 1} stepper_z_oid={STEPPER + 2} invert_dir={invert}"
            f" kinematics={kinematics}",
            "finalize_config crc=0",
        ]
        for cmd in cmds:
            self._ser.send(cmd)
        config = self._ser.send_with_response("get_config", "config")
        if not config["is_config"]:
            raise BenchError("MCU の設定に失敗しました")

    def _init_tmc(self):
        for a in AXES:
            registers = tmc_registers(a)
            before = self._tmc_read(a.uart_address, "IFCNT")
            for name, value in registers.items():
                self._tmc_write(a.uart_address, name, value)
            after = self._tmc_read(a.uart_address, "IFCNT")
            gconf = self._tmc_read(a.uart_address, "GCONF")
            if (after - before) & 0xFF != len(registers) or gconf != registers["GCONF"]:
                raise BenchError(f"{a.name}: TMC2209 への書き込みを確かめられません")
            print(
                f"{a.name}: TMC2209 を設定しました（{a.run_current} A、{a.microsteps} microsteps）"
            )

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
        pos = " ".join(f"{a.name}={a.mm(s[a.name]):8.3f}" for a in AXES)
        vel = " ".join(f"v{a.name}={a.mm(s['v' + a.name]):6.2f}" for a in AXES)
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
            if params["pin_value"] == AXES[i].endstop_trigger_value:
                pressed.add(i)
        return pressed

    def _set_limits(self, i, homed):
        a = AXES[i]
        if homed:
            low, high = a.position_min, a.position_max
        else:
            # 原点出しの前は位置が分からないので、いまの位置を 0 として、スイッチを探す距離だけ前後に
            low, high = -self._homing_dist(a), self._homing_dist(a)
        self._ser.send(
            f"velocity_stream_set_limits oid={VS} axis={i} min_pos={a.steps(low)}"
            f" max_pos={a.steps(high)} max_velocity={a.steps(a.max_velocity)}"
            f" accel={a.steps(a.accel)}"
        )

    @staticmethod
    def _homing_dist(axis):
        return HOMING_SPAN * (axis.position_max - axis.position_min)

    @staticmethod
    def _enable_value(axis, on):
        """励磁のピンの値（enable_pin が反転（!）なら 0 で励磁）."""
        return int(on) ^ int(axis.enable_pin.invert)

    def _enable(self, on):
        for i, a in enumerate(AXES):
            self._ser.send(
                f"update_digital_out oid={ENABLE + i} value={self._enable_value(a, on)}"
            )

    # ---- 流す・止める

    def _arm(self, i):
        """Trsync に 3 軸の stepper と velocity_stream を登録し、軸 i のスイッチだけを張る."""
        # 前回張ったままの endstop が、登録の途中で trsync を消費しないように
        self._disarm()
        self._ser.send(
            f"trsync_start oid={TRSYNC} report_clock=0 report_ticks=0 expire_reason=0"
        )
        for stepper in range(3):
            self._ser.send(
                f"stepper_stop_on_trigger oid={STEPPER + stepper} trsync_oid={TRSYNC}"
            )
        self._ser.send(f"velocity_stream_stop_on_trigger oid={VS} trsync_oid={TRSYNC}")
        clock = (self._now() + self._ticks(START_MARGIN)) & 0xFFFFFFFF
        self._ser.send(
            f"endstop_home oid={ENDSTOP + i} clock={clock}"
            f" sample_ticks={self._ticks(ENDSTOP_SAMPLE)}"
            f" sample_count={ENDSTOP_SAMPLE_COUNT}"
            f" rest_ticks={self._ticks(ENDSTOP_REST)}"
            f" pin_value={AXES[i].endstop_trigger_value}"
            f" trsync_oid={TRSYNC} trigger_reason={REASON_ENDSTOP + i}"
        )

    def _disarm(self):
        for i in range(3):
            self._ser.send(
                f"endstop_home oid={ENDSTOP + i} clock=0 sample_ticks=0 sample_count=0"
                f" rest_ticks=0 pin_value=0 trsync_oid={TRSYNC} trigger_reason=0"
            )

    def _start(self, arm=None):
        """流し始める。arm は原点出しで張るスイッチの軸（None なら張らず、可動域の端でだけ止まる）."""
        self._enable(True)
        self._last_trigger = None
        if arm is not None:
            self._arm(arm)
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
        vx, vy, vz = (a.steps(v) for a, v in zip(AXES, self._velocity))
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

    def _run_for(self, i, velocity, distance):
        """軸 i を velocity で distance だけ動かす（スイッチは張らない。時間で測るので目安）."""
        self._stop()
        self._velocity = [0.0, 0.0, 0.0]
        self._velocity[i] = velocity
        self._start()
        self._wait(lambda: False, abs(distance / velocity))
        self._stop()

    # ---- コマンド

    def cmd_v(self, args):
        """v VX VY VZ: 速度（mm/s）で動かし続け、可動域の端で止まる。原点出しの前の軸は 0 だけ."""
        velocity = [float(x) for x in args]
        if len(velocity) != 3:
            raise BenchError("v VX VY VZ")
        for a, v, homed in zip(AXES, velocity, self._homed):
            if not abs(v) <= a.max_velocity:
                raise BenchError(f"{a.name} の速度は ±{a.max_velocity} mm/s まで")
            if v and not homed:
                raise BenchError(f"{a.name} は原点出しの前です（home を使う）")
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

    def cmd_home(self, args):
        """Home [AXIS...]: 原点出し（既定は z x y の順）."""
        for i in [self._axis(x) for x in args] or [2, 0, 1]:
            self._home(i)

    def _home(self, i):
        """Klipper と同じ手順: スイッチへ向かい、退避して、2 回目はゆっくり向かう."""
        a = AXES[i]
        self._stop()
        self._homed[i] = False
        # 可動域を広げ、いまの位置を 0 にする
        self._set_limits(i, homed=False)
        self._ser.send(f"velocity_stream_set_position oid={VS} axis={i} pos=0")
        if i in self._pressed():
            print(f"{a.name}: スイッチが押されているので、先に離れます")
            self._retract(i)
        self._approach(i, a.homing_speed)
        if a.retract_dist:
            self._retract(i)
            self._approach(i, a.second_homing_speed)
        self._ser.send(
            f"velocity_stream_set_position oid={VS} axis={i} pos={a.steps(a.position_endstop)}"
        )
        self._set_limits(i, homed=True)
        self._homed[i] = True
        self._query()
        print(f"{a.name}: 原点出しを終えました  {self._position_text()}")

    def _retract(self, i):
        a = AXES[i]
        self._run_for(i, -a.home_dir * a.retract_speed, a.retract_dist)
        if i in self._pressed():
            raise BenchError(f"{a.name}: 退避してもスイッチが押されたままです")

    def _approach(self, i, speed):
        """軸 i のスイッチだけを張って向かい、押された位置を 0 にする（広げた可動域の中ほど）."""
        a = AXES[i]
        print(f"{a.name}: スイッチへ向かいます（{speed} mm/s）")
        self._stop()
        self._velocity = [0.0, 0.0, 0.0]
        self._velocity[i] = a.home_dir * speed
        self._start(arm=i)
        # 加速と減速の分の余裕を 1 秒とる
        timeout = self._homing_dist(a) / speed + 1.0
        if not self._wait(lambda: self._state_name() == "contact", timeout):
            self._stop()
            raise BenchError(f"{a.name}: スイッチに届きません（向きと配線を確かめる）")
        self._streaming = False
        # trsync_state は velocity_stream_state と別に届く
        self._wait(lambda: self._last_trigger is not None, 0.5, abortable=False)
        if self._last_trigger != i:
            raise BenchError(f"{a.name}: スイッチ以外の理由で止まりました")
        self._ser.send(f"velocity_stream_set_position oid={VS} axis={i} pos=0")

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
            print("  " + (doc[0].lower() + doc[1:]).rstrip("."))
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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--klipper",
        default=os.path.join(os.path.dirname(os.path.dirname(__file__)), "klipper"),
        help="Klipper のソース（既定: submodule の klipper/）",
    )
    parser.add_argument(
        "--device", help="MCU のシリアル（既定: /dev/serial/by-id から探す）"
    )
    config_dir = os.environ.get("PCBASM_CONFIG_DIR") or os.path.join(
        REPO_ROOT, "config"
    )
    parser.add_argument(
        "--printer-cfg",
        default=os.path.join(config_dir, "printer.cfg"),
        help="機体の printer.cfg（既定: config/printer.cfg）",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    global AXES, KINEMATICS_NAME, TMC_RX, TMC_TX
    try:
        machine = load_machine(args.printer_cfg)
        print(describe_machine(args.printer_cfg, machine))
        # 値を取り違えたまま動かさないように、目で確かめてから進める
        if input("この値で動かしますか？（yes で続ける）: ").strip() != "yes":
            sys.exit("中止しました")
        AXES, TMC_RX, TMC_TX = machine.axes, machine.tmc_rx, machine.tmc_tx
        KINEMATICS_NAME = machine.kinematics
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
