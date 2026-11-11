"""1 軸の原点出し（``velocity_stream`` で流し、trsync でリミットスイッチに止めさせる）.

仕様として読んだもの（コードは写していない。pcbasm は MIT、出典は GPLv3）:

- ``src/firmware/pnp-motion/README.md`` の「ホストが守る前提」（接触で止める: 同じ trsync に 3 軸の
  ``stepper_stop_on_trigger`` と ``velocity_stream_stop_on_trigger`` を登録する。``contact`` の後は
  ``velocity_stream_start`` で開始し直す）と「実機で試す」の ``home`` の手順の説明
- ``src/firmware/pnp-motion/module/velocity_stream.c``（``contact`` になると位置を stepper に合わせ直して
  ``velocity_stream_state`` を送る。``contact`` は動いていない状態なので ``set_position`` を受け付ける）
- ``klipper/src/trsync.c``（引かれると、``velocity_stream_state`` とは別に ``trsync_state`` で理由を知らせる）

``tools/bench.py``（GPLv3）は、ホストが送るコマンドの順を確かめるためにだけ読んだ。名前・構造・
コメント・式の形は写していない。

手順（Klipper の原点出しと同じ考え方。1 回目で速く見つけ、離れてから 2 回目にゆっくり当てて精度を出す）:

1. 止めて、いまの位置を 0 とし、可動域を前後に広げる（:meth:`PnpMotionFirmware.forget_origin`）
2. スイッチが押されていれば、先に ``homing_retract`` だけ離れる
3. その軸のスイッチだけを張り、``homing_speed`` で向かう。押されると全軸が止まり、その位置を 0 にする
4. ``homing_retract`` だけ離れ（スイッチは張らない）、``second_homing_speed`` でもう一度向かう
5. 可動域を ``position_min``〜``position_max`` に戻し、位置を ``position_endstop`` にする。軸はスイッチが押された
   位置にいて、MCU は ``contact`` のまま残る（次の ``velocity_stream_start`` で開始できる）

止まったのがスイッチのせいかは、``contact`` の報告と、同じ trsync の ``trsync_state`` が知らせる理由の
両方が揃うのを、速度を送り直す同じ待ちの中で待って確かめる。
軸を出す順（Z を先に出す）は :func:`pcbasm.hal.motion.homing_order` が決める。
"""

from __future__ import annotations

from pcbasm.hal.mcu.firmware import (
    AXES,
    PnpMotionFirmware,
    ProcedureError,
    StreamReport,
    StreamState,
)
from pcbasm.hal.motion import Axis

# スイッチを探す待ちの余裕 [s]。trsync_state が contact の報告より遅れて届く分と、問い合わせの間隔の分
_SEEK_SLACK = 0.5


def home_axis(firmware: PnpMotionFirmware, axis: Axis) -> None:
    """軸 ``axis`` の原点を出す（終わるまで待つ）.

    Raises:
        ProcedureError: スイッチに届かない、スイッチ以外の理由で止まった、離れてもスイッチが押されたまま
        McuError: MCU と話せない、shutdown した
    """
    settings = firmware.axis(axis)
    toward = 1 if settings.homing_positive else -1
    if firmware.streaming:
        firmware.stop()
    firmware.forget_origin(axis)
    if settings.homing_retract > 0 and firmware.endstop_pressed(axis):
        _leave_switch(firmware, axis, toward)
    _seek_switch(firmware, axis, toward * settings.homing_speed)
    if settings.homing_retract > 0:
        _leave_switch(firmware, axis, toward)
        _seek_switch(firmware, axis, toward * settings.second_homing_speed)
    firmware.set_limits(axis, *firmware.homed_limits(axis))
    firmware.set_position(axis, settings.to_steps(settings.position_endstop))
    firmware.query()  # 報告の位置を、付け替えた座標にする


def _seek_switch(firmware: PnpMotionFirmware, axis: Axis, speed: float) -> None:
    """その軸のスイッチだけを張って ``speed`` [mm/s]（符号は向き）で流し、スイッチが止めた位置を 0 とする."""
    settings = firmware.axis(axis)
    reach = settings.to_mm(
        firmware.search_limits(axis)[1]
    )  # 0 から広げた可動域の端まで [mm]
    # 端まで一定の速さで進む時間に、加速と減速の時間と余裕を足す
    limit = reach / abs(speed) + 2 * abs(speed) / settings.max_accel + _SEEK_SLACK
    velocity = [0, 0, 0]
    velocity[AXES.index(axis)] = settings.to_steps(speed)

    def stopped(report: StreamReport) -> bool:
        return (
            report.state is StreamState.CONTACT and firmware.trigger_reason is not None
        )

    firmware.arm_homing(axis)
    try:
        firmware.start()
        report = firmware.drive((velocity[0], velocity[1], velocity[2]), stopped, limit)
    finally:
        firmware.disarm_homing()
    if report is None:
        firmware.stop()
        raise ProcedureError(
            f"{axis.upper()}: {reach:.0f} mm 動いてもリミットスイッチが反応しない。"
            "モーターの向き（dir_pin の !）とスイッチの配線を確かめてください"
        )
    stopped_by = firmware.switch_axis(firmware.trigger_reason)
    if stopped_by != axis:
        raise ProcedureError(
            f"{axis.upper()}: 原点出しの途中で止まったが、止めたのはこの軸のリミットスイッチではない"
            f"（trsync の理由 {firmware.trigger_reason}）"
        )
    firmware.set_position(axis, 0)


def _leave_switch(firmware: PnpMotionFirmware, axis: Axis, toward: int) -> None:
    """スイッチと逆の向きへ ``homing_retract`` だけ動き、スイッチが離れたことを確かめる."""
    settings = firmware.axis(axis)
    here = firmware.query().position[AXES.index(axis)]
    firmware.travel(
        {axis: here - toward * settings.to_steps(settings.homing_retract)},
        speed=settings.homing_speed,
    )
    if firmware.endstop_pressed(axis):
        raise ProcedureError(
            f"{axis.upper()}: {settings.homing_retract} mm 離れても、リミットスイッチが押されたまま"
            "（スイッチの故障か配線の短絡を確かめてください）"
        )
