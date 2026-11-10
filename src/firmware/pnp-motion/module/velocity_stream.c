// SPDX-License-Identifier: GPL-3.0-only
//
// PnP 機の XYZ を速度指令で動かす、Klipper の MCU ファームのモジュール。
//
// ホストは各軸の目標速度と有効期限を送り続けるか、軸ごとに目標位置を送る。一定周期のタイマで
// task を起こし、task が vs_planner で区間ごとのステップを作って stepper のキューへ積む。
// 速度指令の期限が切れたら目標を 0 にして減速する。trsync が引かれたら（接触やリミットスイッチ）、
// stepper は trsync の側で即時に止まり、このモジュールは TRIGGERED になって位置を stepper に合わせ直す。

#include "autoconf.h"  // CONFIG_CLOCK_FREQ
#include "basecmd.h"  // oid_alloc
#include "board/irq.h"  // irq_disable
#include "board/misc.h"  // timer_read_time
#include "command.h"  // DECL_COMMAND
#include "compiler.h"  // container_of
#include "pnp_version.h"  // PNP_MOTION_VERSION（build.sh が生成する）
#include "sched.h"  // struct timer
#include "stepper.h"  // stepper_queue_move
#include "trsync.h"  // trsync_add_signal
#include "vs_planner.h"  // vs_axis_plan

#define AXES 3
// 区間と先読みの範囲（短すぎる区間は task を詰まらせる）。
// MAX_VELOCITY と合わせて、1 区間のステップ数は 50000 以下（uint16_t に収まる）
#define MIN_PERIOD (CONFIG_CLOCK_FREQ / 2000)  // 0.5 ms
#define MAX_LEAD (CONFIG_CLOCK_FREQ / 10)  // 100 ms
// 先読みする区間の数の上限。1 区間・1 軸あたり最大 3 つの move を積むので、
// 3 軸で 9 × (64 + 1) = 585 個。Klipper の move のプール（最大 1024、全 stepper で共有）に収める
#define MAX_SEGMENTS_AHEAD 64
// 速度指令の有効期限の上限。ホストが固まってから止まり始めるまでの最大の時間
#define MAX_TTL CONFIG_CLOCK_FREQ  // 1 s
// ステップの間隔が区間の先頭の最小間隔を下回らない速さ、int64 があふれない加速度
#define MAX_VELOCITY (CONFIG_CLOCK_FREQ / VS_MIN_GAP_TICKS(CONFIG_CLOCK_FREQ))
#define MAX_ACCEL 1000000000

// コマンドや応答の形を変えたら上げる（ホストが接続時に確かめる）
DECL_CONSTANT("VELOCITY_STREAM_PROTOCOL", 2);
DECL_CONSTANT_STR("PNP_MOTION_VERSION", PNP_MOTION_VERSION);

enum { VS_IDLE, VS_STREAM, VS_STOPPING, VS_TRIGGERED };
DECL_ENUMERATION("velocity_stream_state", "idle", VS_IDLE);
DECL_ENUMERATION("velocity_stream_state", "stream", VS_STREAM);
DECL_ENUMERATION("velocity_stream_state", "stopping", VS_STOPPING);
DECL_ENUMERATION("velocity_stream_state", "triggered", VS_TRIGGERED);

enum { VSF_TRIGGERED = 1<<0 };

struct velocity_stream {
    struct timer timer;
    struct trsync_signal stop_signal;
    struct stepper *steppers[AXES];
    struct vs_axis axes[AXES];
    // 計画の位置 = 向き × stepper の位置 + offset
    int32_t offset[AXES];
    int32_t target[AXES];  // 速度指令の目標速度
    // moving の bit が立った軸は、速度指令ではなく move_target へ move_speed で向かう
    int32_t move_target[AXES];
    uint32_t move_speed;
    uint32_t period, lead, report_ticks, ttl;
    uint32_t seg_clock;  // 次に計画する区間の開始時刻
    uint32_t expire_clock, next_report;
    // STOPPING で全軸が止まったら、積んだステップが出尽くすこの時刻を待って IDLE にする
    uint32_t drain_clock;
    uint16_t seq;
    uint8_t invert_dir, state, limits_set, draining, moving;
    // VSF_TRIGGERED と trigger_reason は trsync の割り込みから書く。
    // trigger_reason はホストが trsync を張るときに決めた値（接触かリミットかを区別する）
    volatile uint8_t flags, trigger_reason;
};

static struct task_wake velocity_stream_wake;

static int32_t
axis_sign(struct velocity_stream *vs, int axis)
{
    return (vs->invert_dir >> axis) & 1 ? -1 : 1;
}

// stepper が実際に進んだ位置（計画の座標）
static int32_t
actual_position(struct velocity_stream *vs, int axis)
{
    return axis_sign(vs, axis) * stepper_position(vs->steppers[axis])
        + vs->offset[axis];
}

static uint_fast8_t
velocity_stream_event(struct timer *t)
{
    struct velocity_stream *vs = container_of(t, struct velocity_stream, timer);
    sched_wake_task(&velocity_stream_wake);
    vs->timer.waketime += vs->period;
    return SF_RESCHEDULE;
}

// trsync のトリガ（割り込みを止めた状態で呼ばれる）
static void
velocity_stream_stop_signal(struct trsync_signal *tss, uint8_t reason)
{
    struct velocity_stream *vs = container_of(
        tss, struct velocity_stream, stop_signal);
    vs->trigger_reason = reason;
    vs->flags |= VSF_TRIGGERED;
    sched_wake_task(&velocity_stream_wake);
}

void
command_config_velocity_stream(uint32_t *args)
{
    struct velocity_stream *vs = oid_alloc(
        args[0], command_config_velocity_stream, sizeof(*vs));
    for (int i = 0; i < AXES; i++)
        vs->steppers[i] = stepper_oid_lookup(args[1 + i]);
    vs->invert_dir = args[4];
    vs->timer.func = velocity_stream_event;
}
DECL_COMMAND(command_config_velocity_stream,
             "config_velocity_stream oid=%c stepper_x_oid=%c stepper_y_oid=%c"
             " stepper_z_oid=%c invert_dir=%c");

static struct velocity_stream *
velocity_stream_oid_lookup(uint8_t oid)
{
    return oid_lookup(oid, command_config_velocity_stream);
}

static int
is_running(struct velocity_stream *vs)
{
    return vs->state == VS_STREAM || vs->state == VS_STOPPING;
}

static int
in_limits(const struct vs_axis_config *c, int32_t pos)
{
    return pos >= c->min_pos && pos <= c->max_pos;
}

void
command_velocity_stream_set_limits(uint32_t *args)
{
    struct velocity_stream *vs = velocity_stream_oid_lookup(args[0]);
    uint8_t axis = args[1];
    int32_t min_pos = args[2], max_pos = args[3];
    uint32_t max_velocity = args[4], accel = args[5];
    if (axis >= AXES || min_pos >= max_pos || !max_velocity || !accel
        || max_velocity > MAX_VELOCITY || accel > MAX_ACCEL)
        shutdown("Invalid velocity_stream limits");
    if (is_running(vs))
        shutdown("Can't set velocity_stream limits while running");
    vs->axes[axis].config = (struct vs_axis_config){
        .min_pos = min_pos, .max_pos = max_pos,
        .max_velocity = max_velocity, .accel = accel};
    vs->limits_set |= 1 << axis;
}
DECL_COMMAND(command_velocity_stream_set_limits,
             "velocity_stream_set_limits oid=%c axis=%c min_pos=%i max_pos=%i"
             " max_velocity=%u accel=%u");

void
command_velocity_stream_set_position(uint32_t *args)
{
    struct velocity_stream *vs = velocity_stream_oid_lookup(args[0]);
    uint8_t axis = args[1];
    int32_t pos = args[2];
    if (axis >= AXES)
        shutdown("Invalid velocity_stream axis");
    if (!(vs->limits_set & (1 << axis)))
        shutdown("velocity_stream limits not set");
    if (is_running(vs))
        shutdown("Can't set velocity_stream position while running");
    if (!in_limits(&vs->axes[axis].config, pos))
        shutdown("velocity_stream position outside limits");
    vs->offset[axis] = pos
        - axis_sign(vs, axis) * stepper_position(vs->steppers[axis]);
    vs_axis_reset(&vs->axes[axis], pos, timer_read_time());
}
DECL_COMMAND(command_velocity_stream_set_position,
             "velocity_stream_set_position oid=%c axis=%c pos=%i");

static void
send_state(uint8_t oid, struct velocity_stream *vs, uint32_t clock)
{
    sendf("velocity_stream_state oid=%c clock=%u seq=%hu state=%c reason=%c"
          " moving=%c x=%i y=%i z=%i vx=%i vy=%i vz=%i"
          , oid, clock, vs->seq, vs->state, vs->trigger_reason, vs->moving
          , actual_position(vs, 0), actual_position(vs, 1)
          , actual_position(vs, 2)
          , vs->axes[0].velocity, vs->axes[1].velocity, vs->axes[2].velocity);
}

void
command_velocity_stream_start(uint32_t *args)
{
    struct velocity_stream *vs = velocity_stream_oid_lookup(args[0]);
    uint32_t clock = args[1], period = args[2], lead = args[3], ttl = args[5];
    if (is_running(vs))
        shutdown("velocity_stream already running");
    if (vs->limits_set != (1 << AXES) - 1)
        shutdown("velocity_stream limits not set");
    if (period < MIN_PERIOD || lead < period || lead > MAX_LEAD
        || lead / period > MAX_SEGMENTS_AHEAD)
        shutdown("Invalid velocity_stream period");
    // 期限は「受け取った時刻 + ttl」で切り、先読みの分だけ先の区間と比べるので、
    // ttl が lead + period 以下だとどの指令も当てる前に切れる
    if (ttl <= lead + period || ttl > MAX_TTL)
        shutdown("Invalid velocity_stream ttl");
    for (int i = 0; i < AXES; i++) {
        if (!in_limits(&vs->axes[i].config, actual_position(vs, i)))
            shutdown("velocity_stream position outside limits");
    }
    uint32_t now = timer_read_time();
    if (timer_is_before(clock, now + lead))
        shutdown("velocity_stream start clock too close");

    vs->period = period;
    vs->lead = lead;
    vs->report_ticks = args[4];
    vs->ttl = ttl;
    vs->draining = vs->moving = 0;
    vs->seg_clock = vs->expire_clock = vs->next_report = clock;
    for (int i = 0; i < AXES; i++) {
        stepper_reset_clock(vs->steppers[i], clock);
        vs_axis_reset(&vs->axes[i], actual_position(vs, i), clock);
        vs->target[i] = 0;
    }
    irq_disable();
    vs->flags &= ~VSF_TRIGGERED;
    vs->trigger_reason = 0;
    vs->state = VS_STREAM;
    sched_del_timer(&vs->timer);
    // すぐに最初の計画を走らせ、以降は period ごと
    vs->timer.waketime = timer_read_time() + timer_from_us(100);
    sched_add_timer(&vs->timer);
    irq_enable();
}
DECL_COMMAND(command_velocity_stream_start,
             "velocity_stream_start oid=%c clock=%u period_ticks=%u"
             " lead_ticks=%u report_ticks=%u ttl_ticks=%u");

void
command_velocity_stream_set_velocity(uint32_t *args)
{
    struct velocity_stream *vs = velocity_stream_oid_lookup(args[0]);
    // TRIGGERED のあとは velocity_stream_start で明示に再開するまで受け付けない
    if (vs->state != VS_STREAM)
        return;
    vs->seq = args[1];
    for (int i = 0; i < AXES; i++)
        vs->target[i] = args[2 + i];
    // 速度指令は、目標位置への移動を取り消す
    vs->moving = 0;
    // 期限は受け取った時点から ttl までに切る（遠すぎる期限や、一周した古い期限で
    // ウォッチドッグが効かなくならないように）
    uint32_t expire = args[5], latest = timer_read_time() + vs->ttl;
    vs->expire_clock = timer_is_before(latest, expire) ? latest : expire;
}
DECL_COMMAND(command_velocity_stream_set_velocity,
             "velocity_stream_set_velocity oid=%c seq=%hu vx=%i vy=%i vz=%i"
             " expire_clock=%u");

void
command_velocity_stream_move(uint32_t *args)
{
    struct velocity_stream *vs = velocity_stream_oid_lookup(args[0]);
    if (vs->state != VS_STREAM)
        return;
    uint8_t axes = args[2] & ((1 << AXES) - 1);
    uint32_t speed = args[6];
    if (!speed)
        shutdown("Invalid velocity_stream move speed");
    vs->seq = args[1];
    vs->move_speed = speed;
    for (int i = 0; i < AXES; i++) {
        if (!(axes & (1 << i)))
            continue;
        // 可動域の外の目標は、可動域の端で止まる（vs_axis_plan_to が切り詰める）
        vs->move_target[i] = args[3 + i];
        vs->target[i] = 0;
    }
    // 移動しない軸の速度指令はそのまま。移動には期限がない（有限の距離で止まるため）
    vs->moving |= axes;
}
DECL_COMMAND(command_velocity_stream_move,
             "velocity_stream_move oid=%c seq=%hu axes=%c x=%i y=%i z=%i"
             " speed=%u");

void
command_velocity_stream_stop(uint32_t *args)
{
    struct velocity_stream *vs = velocity_stream_oid_lookup(args[0]);
    if (vs->state == VS_STREAM)
        vs->state = VS_STOPPING;
}
DECL_COMMAND(command_velocity_stream_stop, "velocity_stream_stop oid=%c");

void
command_velocity_stream_stop_on_trigger(uint32_t *args)
{
    struct velocity_stream *vs = velocity_stream_oid_lookup(args[0]);
    struct trsync *ts = trsync_oid_lookup(args[1]);
    trsync_add_signal(ts, &vs->stop_signal, velocity_stream_stop_signal);
}
DECL_COMMAND(command_velocity_stream_stop_on_trigger,
             "velocity_stream_stop_on_trigger oid=%c trsync_oid=%c");

void
command_velocity_stream_query(uint32_t *args)
{
    uint8_t oid = args[0];
    send_state(oid, velocity_stream_oid_lookup(oid), timer_read_time());
}
DECL_COMMAND(command_velocity_stream_query, "velocity_stream_query oid=%c");

static void
stop_timer(struct velocity_stream *vs, uint8_t state)
{
    irq_disable();
    sched_del_timer(&vs->timer);
    vs->state = state;
    irq_enable();
}

// 1 区間ぶんのステップを stepper のキューへ積む
static void
queue_segment(struct velocity_stream *vs, int axis, struct vs_segment *seg)
{
    if (!seg->count)
        return;
    struct stepper *s = vs->steppers[axis];
    stepper_set_next_dir(s, seg->dir ^ ((vs->invert_dir >> axis) & 1));
    stepper_queue_move(s, seg->first_interval, 1, 0);
    uint16_t short_count = seg->count - 1 - seg->long_count;
    if (short_count)
        stepper_queue_move(s, seg->interval, short_count, 0);
    if (seg->long_count)
        stepper_queue_move(s, seg->interval + 1, seg->long_count, 0);
}

// trsync で止まった。stepper は trsync の側で止まっているので、位置を合わせ直す
static void
handle_trigger(uint8_t oid, struct velocity_stream *vs, uint32_t now)
{
    stop_timer(vs, VS_TRIGGERED);
    vs->moving = 0;
    for (int i = 0; i < AXES; i++) {
        vs_axis_reset(&vs->axes[i], actual_position(vs, i), now);
        vs->target[i] = 0;
    }
    send_state(oid, vs, now);
}

// now + lead までの区間を計画する
static void
plan_segments(struct velocity_stream *vs, uint32_t now)
{
    // 過去の時刻のステップを積むと Klipper が理由の分かりにくい shutdown になるので、先に止める
    if (timer_is_before(vs->seg_clock, now))
        shutdown("velocity_stream fell behind");
    while (timer_is_before(vs->seg_clock, now + vs->lead)) {
        int expired = timer_is_before(vs->expire_clock, vs->seg_clock);
        if (expired || vs->state == VS_STOPPING)
            for (int i = 0; i < AXES; i++)
                vs->target[i] = 0;
        // 停止の指示は、目標位置への移動も取り消して減速させる
        if (vs->state == VS_STOPPING)
            vs->moving = 0;
        for (int i = 0; i < AXES; i++) {
            struct vs_axis *a = &vs->axes[i];
            struct vs_segment seg;
            if (vs->moving & (1 << i)) {
                vs_axis_plan_to(a, vs->move_target[i], vs->move_speed
                                , vs->seg_clock, vs->period, CONFIG_CLOCK_FREQ
                                , &seg);
                if (vs_axis_arrived(a, vs->move_target[i]))
                    vs->moving &= ~(1 << i);
            } else {
                vs_axis_plan(a, vs->target[i], vs->seg_clock
                             , vs->period, CONFIG_CLOCK_FREQ, &seg);
            }
            queue_segment(vs, i, &seg);
        }
        vs->seg_clock += vs->period;
    }
    if (vs->state != VS_STOPPING)
        return;
    // 全軸の速度が 0 になった区間より後にはステップを作らない。積んだステップが
    // 出尽くしてから IDLE にする（その前の start は stepper_reset_clock で shutdown になり、
    // set_position は動いている途中の位置を読んでしまう）
    if (!vs->draining && !vs->axes[0].velocity && !vs->axes[1].velocity
        && !vs->axes[2].velocity) {
        vs->draining = 1;
        vs->drain_clock = vs->seg_clock + vs->period;
    }
    if (vs->draining && !timer_is_before(now, vs->drain_clock))
        stop_timer(vs, VS_IDLE);
}

void
velocity_stream_task(void)
{
    if (!sched_check_wake(&velocity_stream_wake))
        return;
    uint8_t oid;
    struct velocity_stream *vs;
    foreach_oid(oid, vs, command_config_velocity_stream) {
        if (!is_running(vs))
            continue;
        uint32_t now = timer_read_time();
        irq_disable();
        uint8_t flags = vs->flags;
        vs->flags = flags & ~VSF_TRIGGERED;
        irq_enable();
        if (flags & VSF_TRIGGERED) {
            handle_trigger(oid, vs, now);
            continue;
        }
        plan_segments(vs, now);
        if (vs->report_ticks && !timer_is_before(now, vs->next_report)) {
            send_state(oid, vs, now);
            vs->next_report += vs->report_ticks;
            if (timer_is_before(vs->next_report, now))
                vs->next_report = now + vs->report_ticks;
        }
    }
}
DECL_TASK(velocity_stream_task);

void
velocity_stream_shutdown(void)
{
    uint8_t oid;
    struct velocity_stream *vs;
    foreach_oid(oid, vs, command_config_velocity_stream) {
        vs->state = VS_IDLE;
        vs->flags = 0;
    }
}
DECL_SHUTDOWN(velocity_stream_shutdown);
