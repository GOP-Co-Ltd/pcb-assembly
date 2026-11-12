// SPDX-License-Identifier: GPL-3.0-only
//
// PnP 機の XYZ を速度指令で動かす、Klipper の MCU ファームのモジュール。
//
// ホストは直交座標の各軸の目標速度と有効期限を送り続ける。一定周期のタイマで task を起こし、
// task が vs_planner で区間ごとのステップを作って stepper のキューへ積む。
// 軸からモーターへの写し方（cartesian・corexy・inversed_corexy）は設定時に受け取る。
// 期限が切れたら目標を 0 にして減速する。trsync が引かれたら（接触）、stepper は
// trsync の側で即時に止まり、このモジュールは CONTACT になって位置を stepper に合わせ直す。
// モーターの励磁（enable のピン）もこのモジュールが持ち、励磁したまま流さずに置かれた時間が
// 設定した秒数を超えたら、自分で励磁を切る（ホストが落ちていても、止まったモーターを温め続けない）。

#include "autoconf.h"  // CONFIG_CLOCK_FREQ
#include "basecmd.h"  // oid_alloc
#include "board/gpio.h"  // gpio_out_write
#include "board/irq.h"  // irq_disable
#include "board/misc.h"  // timer_read_time
#include "command.h"  // DECL_COMMAND
#include "compiler.h"  // container_of
#include "pnp_version.h"  // PNP_MOTION_VERSION（build.sh が生成する）
#include "sched.h"  // struct timer
#include "stepper.h"  // stepper_queue_move
#include "trsync.h"  // trsync_add_signal
#include "vs_planner.h"  // vs_stage_plan

#define AXES VS_AXES
// 区間と先読みの範囲（短すぎる区間は task を詰まらせる）。
// MAX_VELOCITY と合わせて、1 区間のステップ数は 50000 以下（uint16_t に収まる）
#define MIN_PERIOD (CONFIG_CLOCK_FREQ / 2000)  // 0.5 ms
#define MAX_LEAD (CONFIG_CLOCK_FREQ / 10)  // 100 ms
// 先読みする区間の数の上限。1 区間・1 モーターあたり最大 3 つの move を積むので、
// 3 モーターで 9 × (64 + 1) = 585 個。Klipper の move のプール（最大 1024、全 stepper で共有）に収める
#define MAX_SEGMENTS_AHEAD 64
// 速度指令の有効期限の上限。ホストが固まってから止まり始めるまでの最大の時間
#define MAX_TTL CONFIG_CLOCK_FREQ  // 1 s
// ステップの間隔が区間の先頭の最小間隔を下回らない速さ、int64 があふれない加速度。
// corexy ではモーターが X と Y の速さの和で回るので、X と Y の max_velocity の和で守る
#define MAX_VELOCITY (CONFIG_CLOCK_FREQ / VS_MIN_GAP_TICKS(CONFIG_CLOCK_FREQ))
#define MAX_ACCEL 1000000000

DECL_CONSTANT("VELOCITY_STREAM_PROTOCOL", 2);
DECL_ENUMERATION("velocity_stream_kinematics", "cartesian", VS_CARTESIAN);
DECL_ENUMERATION("velocity_stream_kinematics", "corexy", VS_COREXY);
DECL_ENUMERATION("velocity_stream_kinematics", "inversed_corexy", VS_INVERSED_COREXY);
DECL_CONSTANT_STR("PNP_MOTION_VERSION", PNP_MOTION_VERSION);

enum { VS_IDLE, VS_STREAM, VS_STOPPING, VS_CONTACT };
DECL_ENUMERATION("velocity_stream_state", "idle", VS_IDLE);
DECL_ENUMERATION("velocity_stream_state", "stream", VS_STREAM);
DECL_ENUMERATION("velocity_stream_state", "stopping", VS_STOPPING);
DECL_ENUMERATION("velocity_stream_state", "contact", VS_CONTACT);

enum { VSF_TRIGGERED = 1<<0, VSF_IDLE_TIMEOUT = 1<<1 };

struct velocity_stream {
    struct timer timer;
    // 励磁したまま止まっている秒数を数える 1 秒ごとのタイマ（32 bit の clock は数分を比べられない）
    struct timer idle_timer;
    struct gpio_out enables[AXES];
    struct trsync_signal stop_signal;
    struct stepper *steppers[AXES];
    struct vs_stage stage;
    int32_t target[AXES];
    uint32_t period, lead, report_ticks, ttl;
    uint32_t seg_clock;  // 次に計画する区間の開始時刻
    uint32_t expire_clock, next_report;
    // STOPPING で全軸が止まったら、積んだステップが出尽くすこの時刻を待って IDLE にする
    uint32_t drain_clock;
    // 励磁したまま止まっていたら励磁を切るまでの秒数（0 なら切らない）と、いま数えている秒数
    uint32_t idle_timeout, idle_seconds;
    uint16_t seq;
    uint8_t invert_dir, invert_enable, enabled, state, limits_set, draining;
    // 自分で脱力した回数（255 の次は 0）。ホストは報告の enabled ではなくこの数の変化で脱力を知る
    // （脱力の報告が届く前に励磁し直すと、enabled だけでは見落とす）
    uint8_t relaxes;
    // VSF_TRIGGERED は trsync の割り込みから、VSF_IDLE_TIMEOUT は idle_timer から立つ
    volatile uint8_t flags;
};

static struct task_wake velocity_stream_wake;

// stepper が実際に進んだ位置（モーターの座標。invert_dir の bit が立ったモーターは向きを反転する）
static void
motor_positions(struct velocity_stream *vs, int32_t motors[AXES])
{
    for (int i = 0; i < AXES; i++) {
        int32_t pos = stepper_position(vs->steppers[i]);
        motors[i] = (vs->invert_dir >> i) & 1 ? -pos : pos;
    }
}

// stepper が実際に進んだ位置から求めた、軸 axis の直交座標の位置
static int32_t
actual_position(struct velocity_stream *vs, int axis)
{
    int32_t motors[AXES];
    motor_positions(vs, motors);
    return vs_stage_position(&vs->stage, motors, axis);
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
    vs->flags |= VSF_TRIGGERED;
    sched_wake_task(&velocity_stream_wake);
}

static int
is_running(struct velocity_stream *vs)
{
    return vs->state == VS_STREAM || vs->state == VS_STOPPING;
}

static uint_fast8_t
velocity_stream_idle_event(struct timer *t)
{
    struct velocity_stream *vs = container_of(
        t, struct velocity_stream, idle_timer);
    if (!vs->enabled || !vs->idle_timeout || is_running(vs)) {
        vs->idle_seconds = 0;
    } else if (++vs->idle_seconds > vs->idle_timeout) {
        // 止まったのは tick と tick の間なので、超えたと言えるのは idle_timeout + 1 回目の tick
        vs->idle_seconds = 0;
        vs->flags |= VSF_IDLE_TIMEOUT;
        sched_wake_task(&velocity_stream_wake);
    }
    vs->idle_timer.waketime += CONFIG_CLOCK_FREQ;
    return SF_RESCHEDULE;
}

// enable のピンを書く（invert_enable の bit が立ったモーターは 0 で励磁）
static void
write_enables(struct velocity_stream *vs, uint8_t on)
{
    for (int i = 0; i < AXES; i++)
        gpio_out_write(vs->enables[i], on ^ ((vs->invert_enable >> i) & 1));
}

static void
set_enabled(struct velocity_stream *vs, uint8_t on)
{
    irq_disable();
    write_enables(vs, on);
    vs->enabled = on;
    // 数え直す（タイマが立てたばかりの印も捨て、入れ直した直後に切らない）
    vs->idle_seconds = 0;
    vs->flags &= ~VSF_IDLE_TIMEOUT;
    irq_enable();
}

void
command_config_velocity_stream(uint32_t *args)
{
    struct velocity_stream *vs = oid_alloc(
        args[0], command_config_velocity_stream, sizeof(*vs));
    // shutdown のハンドラが書くので、shutdown しうる検査より先に設定する。設定した直後は脱力させておく
    vs->invert_enable = args[9];
    for (int i = 0; i < AXES; i++)
        vs->enables[i] = gpio_out_setup(
            args[6 + i], (vs->invert_enable >> i) & 1);
    for (int i = 0; i < AXES; i++)
        vs->steppers[i] = stepper_oid_lookup(args[1 + i]);
    vs->invert_dir = args[4];
    if (args[5] >= VS_KINEMATICS_COUNT)
        shutdown("Invalid velocity_stream kinematics");
    vs->stage.kinematics = args[5];
    vs->timer.func = velocity_stream_event;
    vs->idle_timer.func = velocity_stream_idle_event;
}
DECL_COMMAND(command_config_velocity_stream,
             "config_velocity_stream oid=%c stepper_x_oid=%c stepper_y_oid=%c"
             " stepper_z_oid=%c invert_dir=%c kinematics=%c enable_x_pin=%u"
             " enable_y_pin=%u enable_z_pin=%u invert_enable=%c");

static struct velocity_stream *
velocity_stream_oid_lookup(uint8_t oid)
{
    return oid_lookup(oid, command_config_velocity_stream);
}

static int
in_limits(const struct vs_axis_config *c, int32_t pos, int32_t slack)
{
    // ホストが送った境界が int32 の端でもあふれないよう int64 で比べる
    return (int64_t)pos >= (int64_t)c->min_pos - slack
        && (int64_t)pos <= (int64_t)c->max_pos + slack;
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
    vs->stage.axes[axis].config = (struct vs_axis_config){
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
    if (!in_limits(&vs->stage.axes[axis].config, pos, 0))
        shutdown("velocity_stream position outside limits");
    int32_t motors[AXES];
    motor_positions(vs, motors);
    vs_stage_set_position(&vs->stage, motors, axis, pos);
}
DECL_COMMAND(command_velocity_stream_set_position,
             "velocity_stream_set_position oid=%c axis=%c pos=%i");

static void
send_state(uint8_t oid, struct velocity_stream *vs, uint32_t clock)
{
    sendf("velocity_stream_state oid=%c clock=%u seq=%hu state=%c"
          " x=%i y=%i z=%i vx=%i vy=%i vz=%i enabled=%c relaxes=%c"
          , oid, clock, vs->seq, vs->state
          , actual_position(vs, 0), actual_position(vs, 1)
          , actual_position(vs, 2)
          , vs->stage.axes[0].velocity, vs->stage.axes[1].velocity
          , vs->stage.axes[2].velocity, vs->enabled, vs->relaxes);
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
    struct vs_axis *axes = vs->stage.axes;
    if (vs->stage.kinematics != VS_CARTESIAN
        && axes[0].config.max_velocity + axes[1].config.max_velocity > MAX_VELOCITY)
        shutdown("Invalid velocity_stream limits");
    // corexy では stepper の位置から求めた位置が半歩（floor で 1 ステップ）ずれるので、
    // 可動域の端で止まった直後の開始を受け付けるよう 1 ステップの余裕をみる
    for (int i = 0; i < AXES; i++) {
        if (!in_limits(&axes[i].config, actual_position(vs, i), 1))
            shutdown("velocity_stream position outside limits");
    }
    uint32_t now = timer_read_time();
    if (timer_is_before(clock, now + lead))
        shutdown("velocity_stream start clock too close");

    vs->period = period;
    vs->lead = lead;
    vs->report_ticks = args[4];
    vs->ttl = ttl;
    vs->draining = 0;
    vs->seg_clock = vs->expire_clock = vs->next_report = clock;
    int32_t motors[AXES];
    motor_positions(vs, motors);
    vs_stage_reset(&vs->stage, motors, clock);
    for (int i = 0; i < AXES; i++) {
        stepper_reset_clock(vs->steppers[i], clock);
        vs->target[i] = 0;
    }
    irq_disable();
    vs->flags &= ~(VSF_TRIGGERED | VSF_IDLE_TIMEOUT);
    vs->idle_seconds = 0;
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
    // CONTACT のあとは velocity_stream_start で明示に再開するまで受け付けない
    if (vs->state != VS_STREAM)
        return;
    vs->seq = args[1];
    for (int i = 0; i < AXES; i++)
        vs->target[i] = args[2 + i];
    // 期限は受け取った時点から ttl までに切る（遠すぎる期限や、一周した古い期限で
    // ウォッチドッグが効かなくならないように）
    uint32_t expire = args[5], latest = timer_read_time() + vs->ttl;
    vs->expire_clock = timer_is_before(latest, expire) ? latest : expire;
}
DECL_COMMAND(command_velocity_stream_set_velocity,
             "velocity_stream_set_velocity oid=%c seq=%hu vx=%i vy=%i vz=%i"
             " expire_clock=%u");

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
command_velocity_stream_set_enable(uint32_t *args)
{
    struct velocity_stream *vs = velocity_stream_oid_lookup(args[0]);
    uint8_t on = !!args[1];
    // 流している間に切ると、積んだステップを励磁なしで出してしまう
    if (!on && is_running(vs))
        shutdown("Can't disable velocity_stream while running");
    set_enabled(vs, on);
}
DECL_COMMAND(command_velocity_stream_set_enable,
             "velocity_stream_set_enable oid=%c enable=%c");

void
command_velocity_stream_set_idle_timeout(uint32_t *args)
{
    struct velocity_stream *vs = velocity_stream_oid_lookup(args[0]);
    irq_disable();
    vs->idle_timeout = args[1];
    vs->idle_seconds = 0;
    sched_del_timer(&vs->idle_timer);
    vs->idle_timer.waketime = timer_read_time() + CONFIG_CLOCK_FREQ;
    sched_add_timer(&vs->idle_timer);
    irq_enable();
}
DECL_COMMAND(command_velocity_stream_set_idle_timeout,
             "velocity_stream_set_idle_timeout oid=%c seconds=%u");

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
queue_segment(struct velocity_stream *vs, int motor, struct vs_segment *seg)
{
    if (!seg->count)
        return;
    struct stepper *s = vs->steppers[motor];
    stepper_set_next_dir(s, seg->dir ^ ((vs->invert_dir >> motor) & 1));
    stepper_queue_move(s, seg->first_interval, 1, 0);
    uint16_t short_count = seg->count - 1 - seg->long_count;
    if (short_count)
        stepper_queue_move(s, seg->interval, short_count, 0);
    if (seg->long_count)
        stepper_queue_move(s, seg->interval + 1, seg->long_count, 0);
}

// 接触で止まった。stepper は trsync の側で止まっているので、位置を合わせ直す
static void
handle_contact(uint8_t oid, struct velocity_stream *vs, uint32_t now)
{
    stop_timer(vs, VS_CONTACT);
    int32_t motors[AXES];
    motor_positions(vs, motors);
    vs_stage_reset(&vs->stage, motors, now);
    for (int i = 0; i < AXES; i++)
        vs->target[i] = 0;
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
        struct vs_segment seg[AXES];
        vs_stage_plan(&vs->stage, vs->target, vs->seg_clock, vs->period
                      , CONFIG_CLOCK_FREQ, seg);
        for (int i = 0; i < AXES; i++)
            queue_segment(vs, i, &seg[i]);
        vs->seg_clock += vs->period;
    }
    if (vs->state != VS_STOPPING)
        return;
    // 全軸の速度が 0 になった区間より後にはステップを作らない。積んだステップが
    // 出尽くしてから IDLE にする（その前の start は stepper_reset_clock で shutdown になり、
    // set_position は動いている途中の位置を読んでしまう）
    struct vs_axis *axes = vs->stage.axes;
    if (!vs->draining && !axes[0].velocity && !axes[1].velocity
        && !axes[2].velocity) {
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
        if (vs->flags & VSF_IDLE_TIMEOUT) {
            irq_disable();
            vs->flags &= ~VSF_IDLE_TIMEOUT;
            irq_enable();
            // タイマが立ててから流し始めていなければ切る
            if (vs->enabled && !is_running(vs)) {
                set_enabled(vs, 0);
                vs->relaxes++;
                send_state(oid, vs, timer_read_time());
            }
        }
        if (!is_running(vs))
            continue;
        uint32_t now = timer_read_time();
        irq_disable();
        uint8_t flags = vs->flags;
        vs->flags = flags & ~VSF_TRIGGERED;
        irq_enable();
        if (flags & VSF_TRIGGERED) {
            handle_contact(oid, vs, now);
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
        write_enables(vs, 0);
        vs->enabled = 0;
        vs->state = VS_IDLE;
        vs->flags = 0;
    }
}
DECL_SHUTDOWN(velocity_stream_shutdown);
