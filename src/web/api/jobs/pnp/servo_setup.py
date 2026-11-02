"""ピンセットのサーボ設定ジョブ（2 台の役割を人が判別し、Open Close の閉じ・開きの位置を読む）."""

from __future__ import annotations

from pcbasm.hal import ServoBus, hold_position, wiggle
from pcbasm.pnp.servo_setup import (
    OpenCloseRange,
    ServoRole,
    exercise,
    find_pair,
    release,
    role_of_mode,
    roles_from_answers,
    servo_settings,
    verify_modes,
)
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import ApplyPayload, JobResult

AGAIN = "もう一度動かす"


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="servo_setup",
            label="ピンセットのサーボ設定（役割と開閉の範囲）",
            run=_run_servo_setup,
            params=(
                ParamSpec(
                    "max_id", "探す ID の上限", "int", 20, minimum=1, maximum=253
                ),
            ),
            # サーボだけを動かし、XYZ には触れない。終了時のノズルキャップ駐機をせず、
            # そのため機体スピーカーの完了音も鳴らない
            uses_machine=False,
        )
    )


def _run_servo_setup(ctx: JobContext) -> JobResult:
    """2 台の役割を判別し、Open Close の閉じ・開きの位置を [pnp.servo] への保存候補にする."""
    bus = ctx.devices.servo_bus()
    ctx.progress("サーボを探す")
    pair = find_pair(bus, range(int(ctx.params["max_id"]) + 1))
    ctx.log(f"応答したサーボ: ID {pair[0]}、ID {pair[1]}")
    # 位置モードのサーボを振るので、ピンセットが当たりにあると押し付けてしまう
    ctx.require(
        "サーボを 1 台ずつ少し振ります。ピンセットを中ほど（閉じ切り・開き切りでない位置）にしてから"
        "「始める」を押してください",
        true_label="始める",
    )

    ctx.progress("役割の判別")
    while True:
        answers = {servo_id: _ask_role(ctx, bus, servo_id) for servo_id in pair}
        if (roles := roles_from_answers(answers)) is not None:
            break
        ctx.log(
            "2 台に同じ役割を選びました。Rotation と Open Close を 1 台ずつ選んでください"
        )
    verify_modes(bus, roles)

    ctx.progress("開閉の範囲")
    while True:
        span = _read_range(ctx, bus, roles.open_close_id)
        if span is None:
            continue
        ctx.require(
            "閉じ切り → 開き切りと動かして確かめます。ピンセットから手を離してから「動かす」を押してください",
            true_label="動かす",
        )
        if not exercise(bus, roles.open_close_id, span):
            ctx.log(
                "確認の開閉が止まりきらない（当たりに押し付けている）のでトルクを切りました。"
                "閉じ切り・開き切りを読み直します"
            )
            continue
        if ctx.confirm(
            "閉じ切り → 開き切りと動かしました。当たって唸らずに開閉しましたか?",
            true_label="はい",
            false_label="やり直す",
        ):
            break

    ctx.progress("完了", 100.0)
    return JobResult(
        summary=(
            f"Rotation: ID {roles.rotation_id} / Open Close: ID {roles.open_close_id}"
            f" / 閉じ: {span.closed_position} / 開き: {span.open_position}"
            f"（開き量 {span.span_degrees:.1f}°）"
        ),
        apply=ApplyPayload(
            label="[pnp.servo] に役割と開閉の範囲を設定",
            values=servo_settings(roles, span),
        ),
    )


def _ask_role(ctx: JobContext, bus: ServoBus, servo_id: int) -> ServoRole:
    """サーボを動かして見せ、役割を選ばれるまで尋ねる."""
    default = role_of_mode(bus.mode(servo_id)).label
    roles = {role.label: role for role in ServoRole}
    while True:
        wiggle(bus, servo_id)
        answer = ctx.choose(
            f"ID {servo_id} のサーボを少し振りました。どちらのサーボですか?",
            (*roles, AGAIN),
            default=default,
        )
        if answer in roles:
            return roles[answer]


def _read_range(ctx: JobContext, bus: ServoBus, servo_id: int) -> OpenCloseRange | None:
    """トルクを切って人に閉じ切り・開き切りにしてもらい、その位置を読む（使えない範囲なら None）."""
    release(bus, servo_id)
    ctx.require(
        f"Open Close（ID {servo_id}）のトルクを切りました。手でピンセットを閉じ切りにして保持し、「読む」を押してください",
        true_label="読む",
    )
    closed = bus.position(servo_id)
    ctx.require(
        "手でピンセットを開き切りにして保持し、「読む」を押してください（読むとトルクが入ります）",
        true_label="読む",
    )
    opened = hold_position(bus, servo_id)
    span = OpenCloseRange(closed, opened)
    if error := span.validate():
        ctx.log(f"{error}。もう一度読みます")
        return None
    return span
