// SPDX-License-Identifier: GPL-3.0-only
//
// vs_planner をホストの cc でテストする（make test）。
//
// 区間を Klipper の stepper と同じ規則（先頭 1 歩を first_interval 後、残りを interval ごと）で
// モーターごとに展開し、ステップの時刻と数を確かめる。直交座標の位置は、展開したステップの数
// （stepper の位置）から求める。

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "vs_planner.h"

#define CLOCK_FREQ 12000000
#define PERIOD 24000  // 2 ms
// kurousagi: GT2 16T（32 mm/rev）、200 steps/rev、64 マイクロステップ。速さと加速度は printer.cfg の値
#define STEPS_PER_MM (200 * 64 / 32)
#define MAX_POS ((int32_t)(68 * STEPS_PER_MM))
#define MAX_VELOCITY ((uint32_t)(100 * STEPS_PER_MM))
#define ACCEL ((uint32_t)(500 * STEPS_PER_MM))
// 区間ごとの速度変化の上限（切り上げ）
#define DV ((int32_t)(((uint64_t)ACCEL * PERIOD + CLOCK_FREQ - 1) / CLOCK_FREQ))
// ステップが区間の終わりからはみ出してよい量（interval の丸めの累積）
#define LATE_TOLERANCE 200

static int failures;

#define CHECK(cond, ...)                                                    \
    do {                                                                    \
        if (!(cond)) {                                                      \
            failures++;                                                     \
            printf("FAIL %s:%d: %s: ", __func__, __LINE__, #cond);          \
            printf(__VA_ARGS__);                                            \
            printf("\n");                                                   \
            return;                                                         \
        }                                                                   \
    } while (0)

// Klipper の stepper（モーター 1 つ）を模す。時刻は 64 bit の絶対時刻で追う。
// 実機では 32 bit の時刻を、いまから ±2^31 の範囲として解釈する（sched_add_timer）ので、
// ステップの時刻も区間の開始から ±2^31 の範囲として解く
struct motor_sim {
    uint64_t step_clock;  // 最後のステップの時刻（ステップがなければ基準の時刻）
    int32_t position;     // dir=1 で増える
    int64_t steps;        // 打ったステップの総数
    uint64_t min_gap;     // ステップの最小間隔
    uint16_t max_count;   // 1 区間の最大のステップ数
    uint8_t has_step;
};

struct sim {
    struct vs_stage stage;
    uint32_t period;
    uint64_t clock;  // 次の区間の開始時刻
    struct motor_sim m[VS_AXES];
};

static const struct vs_axis_config DEFAULT_CONFIG = {
    .min_pos = 0, .max_pos = MAX_POS, .max_velocity = MAX_VELOCITY, .accel = ACCEL};

static void
sim_motors(const struct sim *s, int32_t motors[VS_AXES])
{
    for (int i = 0; i < VS_AXES; i++)
        motors[i] = s->m[i].position;
}

// 直交座標の位置（stepper の位置から求めたもの）
static int32_t
sim_position(const struct sim *s, uint8_t axis)
{
    int32_t motors[VS_AXES];
    sim_motors(s, motors);
    return vs_stage_position(&s->stage, motors, axis);
}

// モーターをすべて 0 に置き、直交座標の位置を (x, y, z) にして止める
static void
sim_init_at(struct sim *s, uint8_t kinematics, int32_t x, int32_t y, int32_t z,
            uint32_t clock)
{
    *s = (struct sim){.period = PERIOD, .clock = clock};
    s->stage.kinematics = kinematics;
    for (int i = 0; i < VS_AXES; i++) {
        s->stage.axes[i].config = DEFAULT_CONFIG;
        s->m[i] = (struct motor_sim){.step_clock = clock, .min_gap = UINT64_MAX};
    }
    int32_t motors[VS_AXES] = {0, 0, 0}, pos[VS_AXES] = {x, y, z};
    for (uint8_t i = 0; i < VS_AXES; i++)
        vs_stage_set_position(&s->stage, motors, i, pos[i]);
    vs_stage_reset(&s->stage, motors, clock);
}

// 1 軸の試験。X を pos に置き、Y と Z は可動域の中ほどで止めておく
static void
sim_init(struct sim *s, int32_t pos, uint32_t clock)
{
    sim_init_at(s, VS_CARTESIAN, pos, MAX_POS / 2, MAX_POS / 2, clock);
}

static void
motor_step(struct motor_sim *m, uint64_t time, uint8_t dir)
{
    if (m->has_step && time - m->step_clock < m->min_gap)
        m->min_gap = time - m->step_clock;
    m->step_clock = time;
    m->position += dir ? 1 : -1;
    m->steps++;
    m->has_step = 1;
}

// 1 区間ぶんのステップを、Klipper の stepper と同じ規則で展開する。ずれがあれば 0 を返す
static int
motor_expand(struct motor_sim *m, const struct vs_segment *seg, uint64_t start,
             uint32_t period)
{
    if (!seg->count)
        return 1;
    uint32_t time32 = (uint32_t)m->step_clock + seg->first_interval;
    uint64_t time = start + (int32_t)(time32 - (uint32_t)start);
    if (m->has_step && time <= m->step_clock) {
        printf("  ステップの時刻が戻った: first_interval=%u\n", seg->first_interval);
        return 0;
    }
    if (time < start) {
        printf("  区間の開始より前のステップ: %lld ticks\n", (long long)(time - start));
        return 0;
    }
    motor_step(m, time, seg->dir);
    int short_count = seg->count - 1 - seg->long_count;
    for (int i = 1; i < seg->count; i++) {
        time += seg->interval + (i > short_count);
        motor_step(m, time, seg->dir);
    }
    if (time > start + period + LATE_TOLERANCE) {
        printf("  区間の終わりより後のステップ: %lld ticks\n",
               (long long)(time - start - period));
        return 0;
    }
    if (seg->count > m->max_count)
        m->max_count = seg->count;
    return 1;
}

// 3 軸の目標速度で 1 区間を計画して展開する。ずれがあれば 0 を返す
static int
sim_segment3(struct sim *s, int32_t vx, int32_t vy, int32_t vz)
{
    struct vs_segment seg[VS_AXES];
    int32_t target[VS_AXES] = {vx, vy, vz};
    uint64_t start = s->clock;
    vs_stage_plan(&s->stage, target, (uint32_t)start, s->period, CLOCK_FREQ, seg);
    s->clock += s->period;
    for (int i = 0; i < VS_AXES; i++) {
        struct motor_sim *m = &s->m[i];
        if (!motor_expand(m, &seg[i], start, s->period))
            return 0;
        const struct vs_motor *planned = &s->stage.motors[i];
        if (planned->last_step_clock != (uint32_t)m->step_clock) {
            printf("  モーター %d: last_step_clock=%u が stepper の %u と違う\n", i,
                   planned->last_step_clock, (uint32_t)m->step_clock);
            return 0;
        }
        if (vs_motor_position(planned) != m->position) {
            printf("  モーター %d: 位置 %d が stepper の %d と違う\n", i,
                   vs_motor_position(planned), m->position);
            return 0;
        }
    }
    return 1;
}

// X だけを動かす
static int
sim_segment(struct sim *s, int32_t target)
{
    return sim_segment3(s, target, 0, 0);
}

static int
sim_run(struct sim *s, int32_t target, int segments)
{
    for (int i = 0; i < segments; i++)
        if (!sim_segment(s, target))
            return 0;
    return 1;
}

static int
sim_run3(struct sim *s, int32_t vx, int32_t vy, int32_t vz, int segments)
{
    for (int i = 0; i < segments; i++)
        if (!sim_segment3(s, vx, vy, vz))
            return 0;
    return 1;
}

static void
test_reset_sets_position_and_stops(void)
{
    struct sim s;
    sim_init_at(&s, VS_CARTESIAN, 5000, 6000, 7000, 777);
    s.stage.axes[0].velocity = 1234;
    int32_t motors[VS_AXES] = {0, 0, 0};
    vs_stage_reset(&s.stage, motors, 777);
    CHECK(sim_position(&s, 0) == 5000, "x=%d", sim_position(&s, 0));
    CHECK(sim_position(&s, 1) == 6000, "y=%d", sim_position(&s, 1));
    CHECK(sim_position(&s, 2) == 7000, "z=%d", sim_position(&s, 2));
    CHECK(s.stage.axes[0].velocity == 0, "v=%d", s.stage.axes[0].velocity);
    CHECK(s.stage.motors[0].last_step_clock == 777, "clock=%u",
          s.stage.motors[0].last_step_clock);
}

static void
test_zero_target_makes_no_steps(void)
{
    struct sim s;
    sim_init(&s, 1000, 0);
    CHECK(sim_run(&s, 0, 100), "展開がずれた");
    CHECK(s.m[0].steps == 0, "steps=%lld", (long long)s.m[0].steps);
}

static void
test_acceleration_is_limited_per_segment(void)
{
    struct sim s;
    sim_init(&s, 1000, 0);
    int32_t prev = 0;
    for (int i = 0; i < 200; i++) {
        CHECK(sim_segment(&s, MAX_VELOCITY), "展開がずれた（区間 %d）", i);
        int32_t dv = s.stage.axes[0].velocity - prev;
        CHECK(dv >= 0 && dv <= DV, "区間 %d で dv=%d（上限 %d）", i, dv, DV);
        prev = s.stage.axes[0].velocity;
    }
}

static void
test_velocity_is_clamped_to_max(void)
{
    struct sim s;
    sim_init(&s, MAX_POS / 2, 0);
    CHECK(sim_run(&s, 10 * MAX_VELOCITY, 100), "展開がずれた");
    CHECK(s.stage.axes[0].velocity == (int32_t)MAX_VELOCITY, "v=%d", s.stage.axes[0].velocity);
    sim_init(&s, MAX_POS / 2, 0);
    CHECK(sim_run(&s, -10 * (int32_t)MAX_VELOCITY, 100), "展開がずれた");
    CHECK(s.stage.axes[0].velocity == -(int32_t)MAX_VELOCITY, "v=%d", s.stage.axes[0].velocity);
}

static void
test_constant_velocity_spacing(void)
{
    struct sim s;
    sim_init(&s, 1000, 0);
    CHECK(sim_run(&s, MAX_VELOCITY, 100), "展開がずれた");
    int64_t before = s.m[0].steps;
    CHECK(sim_run(&s, MAX_VELOCITY, 50), "展開がずれた");
    int64_t expected = (int64_t)MAX_VELOCITY * 50 * PERIOD / CLOCK_FREQ;
    CHECK(llabs(s.m[0].steps - before - expected) <= 1, "steps=%lld expected=%lld",
          (long long)(s.m[0].steps - before), (long long)expected);
    uint32_t ideal = CLOCK_FREQ / MAX_VELOCITY;
    CHECK(s.m[0].min_gap + 2 >= ideal, "最小間隔 %llu（理想 %u）",
          (unsigned long long)s.m[0].min_gap, ideal);
}

static void
test_stops_at_max_pos(void)
{
    struct sim s;
    sim_init(&s, MAX_POS - 20000, 0);
    for (int i = 0; i < 2000; i++) {
        CHECK(sim_segment(&s, MAX_VELOCITY), "展開がずれた（区間 %d）", i);
        CHECK(sim_position(&s, 0) <= MAX_POS, "pos=%d", sim_position(&s, 0));
    }
    CHECK(s.stage.axes[0].velocity == 0, "v=%d", s.stage.axes[0].velocity);
    CHECK(sim_position(&s, 0) >= MAX_POS - 1, "pos=%d", sim_position(&s, 0));
}

static void
test_stops_at_min_pos(void)
{
    struct sim s;
    sim_init(&s, 20000, 0);
    for (int i = 0; i < 2000; i++) {
        CHECK(sim_segment(&s, -(int32_t)MAX_VELOCITY), "展開がずれた（区間 %d）", i);
        CHECK(sim_position(&s, 0) >= 0, "pos=%d", sim_position(&s, 0));
    }
    CHECK(s.stage.axes[0].velocity == 0, "v=%d", s.stage.axes[0].velocity);
    CHECK(sim_position(&s, 0) <= 1, "pos=%d", sim_position(&s, 0));
}

static void
test_brakes_when_moving_fast_toward_limit(void)
{
    // 最高速度で可動域の端へ向かっても、加速度の範囲で手前から減速して越えない
    // （端の打ち切りで急に止まらない）
    struct sim s;
    sim_init(&s, MAX_POS / 2, 0);
    int32_t prev = 0;
    for (int i = 0; i < 3000; i++) {
        CHECK(sim_segment(&s, MAX_VELOCITY), "展開がずれた（区間 %d）", i);
        int32_t dv = s.stage.axes[0].velocity - prev;
        CHECK(dv >= -DV && dv <= DV, "区間 %d で dv=%d（上限 %d）", i, dv, DV);
        prev = s.stage.axes[0].velocity;
    }
    CHECK(sim_position(&s, 0) <= MAX_POS, "pos=%d", sim_position(&s, 0));
    CHECK(s.stage.axes[0].velocity == 0, "v=%d", s.stage.axes[0].velocity);
}

static void
test_negative_range(void)
{
    // 可動域が負の側にもあるとき、負の端で止まる
    struct sim s;
    sim_init(&s, -40000, 0);
    s.stage.axes[0].config.min_pos = -50000;
    s.stage.axes[0].config.max_pos = 50000;
    CHECK(sim_run(&s, -(int32_t)MAX_VELOCITY, 2000), "展開がずれた");
    CHECK(sim_position(&s, 0) >= -50000, "pos=%d", sim_position(&s, 0));
    CHECK(sim_position(&s, 0) <= -49999, "pos=%d", sim_position(&s, 0));
    CHECK(s.stage.axes[0].velocity == 0, "v=%d", s.stage.axes[0].velocity);
}

static void
test_extreme_targets_are_clamped(void)
{
    struct sim s;
    sim_init(&s, MAX_POS / 2, 0);
    CHECK(sim_run(&s, INT32_MIN, 100), "展開がずれた");
    CHECK(s.stage.axes[0].velocity == -(int32_t)MAX_VELOCITY, "v=%d", s.stage.axes[0].velocity);
    CHECK(sim_run(&s, INT32_MAX, 300), "展開がずれた");
    CHECK(s.stage.axes[0].velocity == (int32_t)MAX_VELOCITY, "v=%d", s.stage.axes[0].velocity);
}

static void
test_outside_range_does_not_jump(void)
{
    // 位置が可動域の外にあっても境界へ飛ばない。外向きには動かず、内向きには加速度の範囲で戻る
    struct sim s;
    int32_t outside = MAX_POS + 100000;
    sim_init(&s, outside, 0);
    CHECK(sim_run(&s, 0, 10), "展開がずれた");
    CHECK(s.m[0].steps == 0, "目標 0 で steps=%lld", (long long)s.m[0].steps);
    CHECK(sim_run(&s, MAX_VELOCITY, 10), "展開がずれた");
    CHECK(sim_position(&s, 0) == outside, "外向きに動いた: pos=%d",
          sim_position(&s, 0));
    for (int i = 0; i < 100; i++) {
        int32_t prev = s.stage.axes[0].velocity;
        CHECK(sim_segment(&s, -(int32_t)MAX_VELOCITY), "展開がずれた（区間 %d）", i);
        CHECK(prev - s.stage.axes[0].velocity <= DV, "区間 %d で急に動いた: v=%d", i,
              s.stage.axes[0].velocity);
    }
    CHECK(s.m[0].max_count <= MAX_VELOCITY * (uint64_t)PERIOD / CLOCK_FREQ + 1,
          "1 区間に %u 歩", s.m[0].max_count);
    CHECK(sim_position(&s, 0) < outside, "戻らない: pos=%d",
          sim_position(&s, 0));
}

static void
test_long_idle_then_move(void)
{
    // 2^31 tick（12 MHz で約 179 秒）より長く止まってから動いても、ステップが区間の中に置かれる
    struct sim s;
    sim_init(&s, 1000, 0);
    CHECK(sim_run(&s, MAX_VELOCITY, 50), "展開がずれた");
    CHECK(sim_run(&s, 0, 100), "展開がずれた");
    uint64_t idle_segments = ((uint64_t)1 << 31) / PERIOD + 1000;
    for (uint64_t i = 0; i < idle_segments; i++)
        CHECK(sim_segment(&s, 0), "展開がずれた（停止中の区間 %llu）",
              (unsigned long long)i);
    int64_t before = s.m[0].steps;
    CHECK(sim_run(&s, MAX_VELOCITY, 50), "展開がずれた");
    CHECK(s.m[0].steps > before, "動かない");
}

static void
test_large_segment(void)
{
    // 長い区間（100 ms）と速い上限（500000 steps/s）で、1 区間に約 50000 歩
    struct sim s;
    sim_init(&s, 1000, 0);
    s.period = CLOCK_FREQ / 10;
    s.stage.axes[0].config.max_pos = INT32_MAX;
    s.stage.axes[0].config.max_velocity = 500000;
    s.stage.axes[0].config.accel = 1000000000;
    CHECK(sim_run(&s, 500000, 20), "展開がずれた");
    CHECK(s.m[0].max_count >= 49999 && s.m[0].max_count <= 50001, "max_count=%u", s.m[0].max_count);
}

static void
test_reversal_keeps_steps_in_order(void)
{
    struct sim s;
    sim_init(&s, MAX_POS / 2, 0);
    CHECK(sim_run(&s, MAX_VELOCITY, 150), "展開がずれた");
    int32_t turn = sim_position(&s, 0);
    CHECK(sim_run(&s, -(int32_t)MAX_VELOCITY, 300), "展開がずれた");
    CHECK(s.stage.axes[0].velocity == -(int32_t)MAX_VELOCITY, "v=%d", s.stage.axes[0].velocity);
    CHECK(sim_position(&s, 0) < turn, "pos=%d turn=%d",
          sim_position(&s, 0), turn);
}

static void
test_rapid_reversals_keep_a_gap(void)
{
    // 目標速度を区間ごとにばらばらに変えて（決まった乱数列）向きを何度も変えても、
    // ステップの間隔が方向切り替えの最小間隔を下回らない
    struct sim s;
    sim_init(&s, MAX_POS / 2, 0);
    s.stage.axes[0].config.accel = 1000000000;
    uint32_t seed = 12345;
    for (int i = 0; i < 100000; i++) {
        seed = seed * 1103515245 + 12345;
        int32_t target = (int32_t)(seed >> 8) % 20000 - 10000;
        CHECK(sim_segment(&s, target), "展開がずれた（区間 %d）", i);
    }
    CHECK(s.m[0].steps > 0, "steps=%lld", (long long)s.m[0].steps);
    CHECK(s.m[0].min_gap >= VS_MIN_GAP_TICKS(CLOCK_FREQ), "最小間隔 %llu",
          (unsigned long long)s.m[0].min_gap);
}

static void
test_slow_steps_span_segments(void)
{
    // 区間より長い間隔のステップ（100 steps/s = 120000 ticks ごと）
    struct sim s;
    sim_init(&s, 1000, 0);
    s.stage.axes[0].config.accel = 100000000;
    CHECK(sim_run(&s, 100, 500), "展開がずれた");
    // 500 区間 = 1 秒で約 100 歩
    CHECK(s.m[0].steps >= 99 && s.m[0].steps <= 101, "steps=%lld", (long long)s.m[0].steps);
    CHECK(s.m[0].min_gap + 2 >= CLOCK_FREQ / 100, "最小間隔 %llu",
          (unsigned long long)s.m[0].min_gap);
}

static void
test_clock_wraparound(void)
{
    // 32 bit の時刻が一周しても、間隔と位置がずれない
    struct sim s;
    sim_init(&s, 1000, UINT32_MAX - 50 * PERIOD);
    CHECK(sim_run(&s, MAX_VELOCITY, 200), "展開がずれた");
    CHECK(s.m[0].steps > 0, "steps=%lld", (long long)s.m[0].steps);
}

// Klipper の corexy（klippy/kinematics/corexy.py）と、このリポジトリの inversed_corexy
// （klipper/inversed_corexy.py）の stepper の位置
static void
test_kinematics_match_klipper(void)
{
    struct {
        uint8_t kin;
        int64_t x, y, a, b;
    } cases[] = {
        {VS_CARTESIAN, 3, 5, 3, 5},
        {VS_COREXY, 3, 5, 8, -2},        // a = x + y, b = x - y
        {VS_INVERSED_COREXY, 3, 5, -2, 8},  // a = x - y, b = x + y
    };
    for (unsigned i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        int64_t axes[VS_AXES] = {cases[i].x, cases[i].y, 7}, motors[VS_AXES];
        vs_kin_to_motors(cases[i].kin, axes, motors);
        CHECK(motors[0] == cases[i].a && motors[1] == cases[i].b && motors[2] == 7,
              "kin=%u: motors=(%lld, %lld, %lld)", cases[i].kin, (long long)motors[0],
              (long long)motors[1], (long long)motors[2]);
    }
}

static void
test_kinematics_round_trip(void)
{
    // stepper の整数の位置から直交座標へ戻し、またモーターへ写すと元に戻る（奇数の和でも）
    uint8_t kins[] = {VS_CARTESIAN, VS_COREXY, VS_INVERSED_COREXY};
    uint32_t seed = 99;
    for (unsigned k = 0; k < sizeof(kins); k++) {
        for (int i = 0; i < 1000; i++) {
            int64_t motors[VS_AXES], axes[VS_AXES], back[VS_AXES];
            for (int j = 0; j < VS_AXES; j++) {
                seed = seed * 1103515245 + 12345;
                motors[j] = ((int64_t)(int32_t)seed >> 4) * ((int64_t)1 << VS_POS_SHIFT);
            }
            vs_kin_to_axes(kins[k], motors, axes);
            vs_kin_to_motors(kins[k], axes, back);
            for (int j = 0; j < VS_AXES; j++)
                CHECK(back[j] == motors[j], "kin=%u: %lld -> %lld", kins[k],
                      (long long)motors[j], (long long)back[j]);
        }
    }
}

static void
test_corexy_x_moves_both_motors_the_same_way(void)
{
    struct sim s;
    sim_init_at(&s, VS_COREXY, MAX_POS / 2, MAX_POS / 2, MAX_POS / 2, 0);
    CHECK(sim_run3(&s, MAX_VELOCITY / 2, 0, 0, 200), "展開がずれた");
    CHECK(s.m[0].steps > 1000, "a: steps=%lld", (long long)s.m[0].steps);
    CHECK(s.m[0].position == s.m[1].position, "a=%d b=%d", s.m[0].position,
          s.m[1].position);
    CHECK(sim_position(&s, 1) == MAX_POS / 2, "y=%d", sim_position(&s, 1));
}

static void
test_corexy_y_moves_motors_opposite(void)
{
    struct sim s;
    sim_init_at(&s, VS_COREXY, MAX_POS / 2, MAX_POS / 2, MAX_POS / 2, 0);
    CHECK(sim_run3(&s, 0, MAX_VELOCITY / 2, 0, 200), "展開がずれた");
    CHECK(s.m[0].position > 1000, "a=%d", s.m[0].position);
    // 小数部の floor の分だけ、片方が 1 歩ずれることがある
    CHECK(abs(s.m[0].position + s.m[1].position) <= 1, "a=%d b=%d", s.m[0].position,
          s.m[1].position);
    // stepper の位置から求めると、半歩の floor の分だけずれることがある
    CHECK(abs(sim_position(&s, 0) - MAX_POS / 2) <= 1, "x=%d", sim_position(&s, 0));

    sim_init_at(&s, VS_INVERSED_COREXY, MAX_POS / 2, MAX_POS / 2, MAX_POS / 2, 0);
    CHECK(sim_run3(&s, 0, MAX_VELOCITY / 2, 0, 200), "展開がずれた");
    CHECK(s.m[1].position > 1000, "b=%d", s.m[1].position);
    // 小数部の floor の分だけ、片方が 1 歩ずれることがある
    CHECK(abs(s.m[0].position + s.m[1].position) <= 1, "a=%d b=%d", s.m[0].position,
          s.m[1].position);
}

static void
test_corexy_diagonal_keeps_cartesian_limits(void)
{
    // 斜めに動かし続けても、X と Y はそれぞれの可動域の中で止まる。
    // X が端で止まったあとも Y は動き続ける（モーターごとの可動域では表せない）
    struct sim s;
    sim_init_at(&s, VS_INVERSED_COREXY, MAX_POS - 30000, 1000, MAX_POS / 2, 0);
    for (int i = 0; i < 6000; i++) {
        CHECK(sim_segment3(&s, MAX_VELOCITY, MAX_VELOCITY, 0), "展開がずれた（区間 %d）",
              i);
        CHECK(sim_position(&s, 0) <= MAX_POS, "x=%d", sim_position(&s, 0));
        CHECK(sim_position(&s, 1) <= MAX_POS, "y=%d", sim_position(&s, 1));
    }
    CHECK(sim_position(&s, 0) >= MAX_POS - 1, "x=%d", sim_position(&s, 0));
    CHECK(sim_position(&s, 1) >= MAX_POS - 1, "y=%d", sim_position(&s, 1));
    CHECK(s.stage.axes[0].velocity == 0 && s.stage.axes[1].velocity == 0,
          "vx=%d vy=%d", s.stage.axes[0].velocity, s.stage.axes[1].velocity);
}

static void
test_corexy_diagonal_moves_one_motor(void)
{
    // x と y を同じ速さで動かすと、片方のモーターだけが倍の速さで回る
    struct sim s;
    sim_init_at(&s, VS_COREXY, MAX_POS / 2, MAX_POS / 2, MAX_POS / 2, 0);
    CHECK(sim_run3(&s, MAX_VELOCITY / 4, MAX_VELOCITY / 4, 0, 300), "展開がずれた");
    CHECK(s.m[1].steps == 0, "b: steps=%lld", (long long)s.m[1].steps);
    int64_t moved = sim_position(&s, 0) - MAX_POS / 2;
    CHECK(llabs(s.m[0].position - 2 * moved) <= 1, "a=%d x の移動=%lld",
          s.m[0].position, (long long)moved);
}

static void
test_set_position_changes_only_that_axis(void)
{
    // corexy で X の位置を置き直しても、Y の位置は変わらない（モーターは動かさない）
    struct sim s;
    sim_init_at(&s, VS_COREXY, 1000, 2000, 3000, 0);
    CHECK(sim_run3(&s, 5000, -7000, 0, 100), "展開がずれた");
    CHECK(sim_run3(&s, 0, 0, 0, 100), "展開がずれた");
    int32_t y = sim_position(&s, 1), motors[VS_AXES];
    sim_motors(&s, motors);
    vs_stage_set_position(&s.stage, motors, 0, 12345);
    CHECK(sim_position(&s, 0) == 12345, "x=%d", sim_position(&s, 0));
    CHECK(sim_position(&s, 1) == y, "y=%d（前 %d）", sim_position(&s, 1), y);
}

static void
test_reset_on_a_half_step_does_not_step(void)
{
    // stepper の位置の和が奇数（直交座標で半歩）でも、止まったままならステップを作らない
    struct sim s;
    sim_init_at(&s, VS_COREXY, 1000, 1000, 1000, 0);
    s.m[0].position = 2001;
    s.m[1].position = 0;
    int32_t motors[VS_AXES];
    sim_motors(&s, motors);
    vs_stage_reset(&s.stage, motors, (uint32_t)s.clock);
    CHECK(sim_run3(&s, 0, 0, 0, 100), "展開がずれた");
    CHECK(s.m[0].steps == 0 && s.m[1].steps == 0, "a=%lld b=%lld",
          (long long)s.m[0].steps, (long long)s.m[1].steps);
    CHECK(sim_run3(&s, 3000, -2000, 0, 100), "展開がずれた");
}

// 止めて開始し直す。モジュールの start と同じく、stepper の時刻の基準を置き直し
// （stepper_reset_clock）、stepper の位置から合わせ直す
static void
sim_restart(struct sim *s)
{
    int32_t motors[VS_AXES];
    sim_motors(s, motors);
    for (int i = 0; i < VS_AXES; i++)
        s->m[i].step_clock = s->clock;
    vs_stage_reset(&s->stage, motors, (uint32_t)s->clock);
}

static void
test_restarts_do_not_drift(void)
{
    // corexy で Y だけを半端な位置まで動かして止め、開始し直すのを繰り返しても、
    // 指令していない X はずれず、可動域の端に置いた X が外へ出ない
    uint8_t kins[] = {VS_COREXY, VS_INVERSED_COREXY};
    int32_t starts[] = {0, 10000};
    for (unsigned k = 0; k < sizeof(kins); k++) {
        for (unsigned j = 0; j < 2; j++) {
            struct sim s;
            sim_init_at(&s, kins[k], starts[j], MAX_POS / 2, MAX_POS / 2, 0);
            for (int cycle = 0; cycle < 20; cycle++) {
                CHECK(sim_run3(&s, 0, 1234, 0, 37), "展開がずれた");
                CHECK(sim_run3(&s, 0, 0, 0, 40), "展開がずれた");
                sim_restart(&s);
                CHECK(sim_position(&s, 0) == starts[j], "kin=%u cycle=%d: x=%d（初め %d）",
                      kins[k], cycle, sim_position(&s, 0), starts[j]);
            }
        }
    }
}

int
main(void)
{
    test_reset_sets_position_and_stops();
    test_zero_target_makes_no_steps();
    test_acceleration_is_limited_per_segment();
    test_velocity_is_clamped_to_max();
    test_constant_velocity_spacing();
    test_stops_at_max_pos();
    test_stops_at_min_pos();
    test_brakes_when_moving_fast_toward_limit();
    test_negative_range();
    test_extreme_targets_are_clamped();
    test_outside_range_does_not_jump();
    test_long_idle_then_move();
    test_large_segment();
    test_reversal_keeps_steps_in_order();
    test_rapid_reversals_keep_a_gap();
    test_slow_steps_span_segments();
    test_clock_wraparound();
    test_kinematics_match_klipper();
    test_kinematics_round_trip();
    test_corexy_x_moves_both_motors_the_same_way();
    test_corexy_y_moves_motors_opposite();
    test_corexy_diagonal_keeps_cartesian_limits();
    test_corexy_diagonal_moves_one_motor();
    test_set_position_changes_only_that_axis();
    test_reset_on_a_half_step_does_not_step();
    test_restarts_do_not_drift();
    if (failures) {
        printf("%d failure(s)\n", failures);
        return 1;
    }
    printf("ok\n");
    return 0;
}
