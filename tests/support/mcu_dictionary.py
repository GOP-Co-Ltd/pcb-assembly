"""テスト用の合成のデータ辞書（MCU の identify が返すもの）を組み立てる.

本物の辞書はファーム（GPLv3）のビルド出力なので、リポジトリ（MIT）には置かない。ここでは JSON を
tests の中で組み立て、zlib で圧縮して使う。標準ライブラリだけを使う（疑似端末の向こうで MCU を演じる
子プロセスからも使えるように）。

コマンドと応答の書式は、ファームの C ソースの ``DECL_COMMAND`` と ``sendf`` の書式どおりに書く
（``src/firmware/pnp-motion/module/velocity_stream.c`` と ``klipper/src`` の ``basecmd.c``・``sched.c``・
``stepper.c``・``endstop.c``・``trsync.c``・``tmcuart.c``・``gpiocmds.c``・``sensor_hx71x.c``・``sensor_bulk.c``・
``sos_filter.c``・``trigger_analog.c``）。ID は辞書ごとにビルドが振るものなので、ここでは
負の ID と 128 以上の ID を含むように選んだ。``identify`` と ``identify_response`` の ID（1 と 0）だけは
プロトコルで固定（``klipper/docs/Protocol.md``）。

使い方:

- :func:`dictionary_json` で辞書の JSON（dict）を作り、要るならテストの中で書き換える
- :func:`dictionary_blob` で zlib に圧縮する（MCU が flash に持つ形）
- :func:`identify_chunks` で、identify に答える chunk（offset とデータ）に分ける
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Mapping
from typing import Any

# コマンドの書式 → ID
COMMANDS: dict[str, int] = {
    "identify offset=%u count=%c": 1,
    "clear_shutdown": 2,
    "emergency_stop": 3,
    "get_uptime": 4,
    "get_clock": 5,
    "finalize_config crc=%u": 6,
    "get_config": 7,
    "allocate_oids count=%c": 8,
    "reset": -4,
    "velocity_stream_query oid=%c": 13,
    "velocity_stream_stop_on_trigger oid=%c trsync_oid=%c": 14,
    "velocity_stream_stop oid=%c": 15,
    "velocity_stream_set_velocity oid=%c seq=%hu vx=%i vy=%i vz=%i expire_clock=%u": 16,
    (
        "velocity_stream_start oid=%c clock=%u period_ticks=%u lead_ticks=%u"
        " report_ticks=%u ttl_ticks=%u"
    ): 17,
    "velocity_stream_set_position oid=%c axis=%c pos=%i": 18,
    (
        "velocity_stream_set_limits oid=%c axis=%c min_pos=%i max_pos=%i"
        " max_velocity=%u accel=%u"
    ): 19,
    (
        "config_velocity_stream oid=%c stepper_x_oid=%c stepper_y_oid=%c"
        " stepper_z_oid=%c invert_dir=%c kinematics=%c enable_x_pin=%u"
        " enable_y_pin=%u enable_z_pin=%u invert_enable=%c"
    ): 20,
    "velocity_stream_set_enable oid=%c enable=%c": 21,
    "velocity_stream_set_idle_timeout oid=%c seconds=%u": 22,
    "config_stepper oid=%c step_pin=%c dir_pin=%c invert_step=%c step_pulse_ticks=%u": 31,
    "stepper_stop_on_trigger oid=%c trsync_oid=%c": 26,
    "config_endstop oid=%c pin=%c pull_up=%c": 34,
    (
        "endstop_home oid=%c clock=%u sample_ticks=%u sample_count=%c"
        " rest_ticks=%u pin_value=%c trsync_oid=%c trigger_reason=%c"
    ): 33,
    "endstop_query_state oid=%c": 32,
    "config_trsync oid=%c": 38,
    "trsync_start oid=%c report_clock=%u report_ticks=%u expire_reason=%c": 37,
    "trsync_set_timeout oid=%c clock=%u": 36,
    "trsync_trigger oid=%c reason=%c": 35,
    "config_tmcuart oid=%c rx_pin=%u pull_up=%c tx_pin=%u bit_time=%u": 59,
    "tmcuart_send oid=%c write=%*s read=%c": 58,
    "config_digital_out oid=%c pin=%u value=%c default_value=%c max_duration=%u": 40,
    "update_digital_out oid=%c value=%c": 41,
    "config_hx71x oid=%c gain_channel=%c dout_pin=%u sclk_pin=%u": 66,
    "query_hx71x oid=%c rest_ticks=%u": 67,
    "hx71x_attach_trigger_analog oid=%c trigger_analog_oid=%c": 68,
    "config_sos_filter oid=%c max_sections=%c": 70,
    (
        "sos_filter_set_offset_scale oid=%c offset=%i scale=%i scale_frac_bits=%c"
        " auto_offset=%c"
    ): 71,
    "config_trigger_analog oid=%c sos_filter_oid=%c": 72,
    "trigger_analog_set_raw_range oid=%c raw_min=%i raw_max=%i": 73,
    "trigger_analog_set_trigger oid=%c trigger_analog_type=%c trigger_value=%i": 74,
    (
        "trigger_analog_home oid=%c trsync_oid=%c trigger_reason=%c"
        " error_reason=%c clock=%u monitor_ticks=%u monitor_max=%u"
    ): 75,
}

# 応答の書式 → ID
RESPONSES: dict[str, int] = {
    "identify_response offset=%u data=%.*s": 0,
    "shutdown clock=%u static_string_id=%hu": -1,
    "is_shutdown static_string_id=%hu": -2,
    "starting": -3,
    "stats count=%u sum=%u sumsq=%u": 128,
    "uptime high=%u clock=%u": 129,
    "clock clock=%u": 130,
    "config is_config=%c crc=%u is_shutdown=%c move_count=%hu": 131,
    (
        "velocity_stream_state oid=%c clock=%u seq=%hu state=%c"
        " x=%i y=%i z=%i vx=%i vy=%i vz=%i enabled=%c relaxes=%c"
    ): 134,
    "stepper_position oid=%c pos=%i": 135,
    "endstop_state oid=%c homing=%c next_clock=%u pin_value=%c": 136,
    "trsync_state oid=%c can_trigger=%c trigger_reason=%c clock=%u": 137,
    "tmcuart_response oid=%c read=%*s": 142,
    "sensor_bulk_data oid=%c sequence=%hu data=%*s": 145,
}

# 列挙の名前 → (列挙子 → 値)
ENUMERATIONS: dict[str, dict[str, Any]] = {
    # 範囲の列挙は [先頭の値, 個数]。gpio0〜gpio29 が 0〜29 になる
    "pin": {"ADC_TEMPERATURE": 254, "gpio0": [0, 30]},
    "velocity_stream_kinematics": {"cartesian": 0, "corexy": 1, "inversed_corexy": 2},
    "velocity_stream_state": {"idle": 0, "stream": 1, "stopping": 2, "contact": 3},
    "trigger_analog_type": {"abs_ge": 0, "gt": 1, "diff_peak_gt": 2},
    "static_string_id": {
        "Command request": 5,
        "Can't set velocity_stream limits while running": 70,
        "Can't set velocity_stream position while running": 71,
        "velocity_stream position outside limits": 72,
        "velocity_stream already running": 73,
        "velocity_stream limits not set": 74,
        "Invalid velocity_stream period": 75,
        "Invalid velocity_stream ttl": 76,
        "Can't add signal that is already active": 77,
        "oids already allocated": 78,
        "Invalid velocity_stream limits": 79,
        "Can't disable velocity_stream while running": 80,
    },
}

# 定数（辞書の ``config``）
CONSTANTS: dict[str, int | str] = {
    "CLOCK_FREQ": 12000000,
    "MCU": "rp2040",
    "STATS_SUMSQ_BASE": 256,
    "VELOCITY_STREAM_PROTOCOL": 2,
    "PNP_MOTION_VERSION": "0000000",
}


def dictionary_json() -> dict[str, Any]:
    """合成の辞書の JSON を、書き換えてよい新しい dict で返す."""
    return {
        "app": "test",
        "version": "test-dictionary",
        "commands": dict(COMMANDS),
        "responses": dict(RESPONSES),
        "enumerations": {name: dict(values) for name, values in ENUMERATIONS.items()},
        "config": dict(CONSTANTS),
    }


def dictionary_blob(data: Mapping[str, Any] | None = None) -> bytes:
    """辞書の JSON を zlib で圧縮する。``data`` を省けば :func:`dictionary_json` の辞書を使う."""
    return zlib.compress(
        json.dumps(dictionary_json() if data is None else data).encode()
    )


def identify_chunks(blob: bytes, size: int = 40) -> list[tuple[int, bytes]]:
    """``blob`` を identify に答える chunk（offset とデータ）に分ける.

    MCU は blob の末尾を過ぎた offset に空のデータで答える。その空の chunk も最後に付ける。
    """
    chunks = [
        (offset, blob[offset : offset + size]) for offset in range(0, len(blob), size)
    ]
    return [*chunks, (len(blob), b"")]
