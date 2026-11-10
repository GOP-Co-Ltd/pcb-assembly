// SPDX-License-Identifier: GPL-3.0-only
//
// vs_planner をホストの cc でテストする（make test）。
//
// 区間を Klipper の stepper と同じ規則（先頭 1 歩を first_interval 後、残りを interval ごと）で
// 展開し、ステップの時刻と数を確かめる。

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "vs_planner.h"

#define CLOCK_FREQ 12000000
#define PERIOD 24000  // 2 ms
// 台形ネジ 1.5 mm/rev、200 steps/rev、64 マイクロステップ
#define STEPS_PER_MM (200 * 64 / 1.5)
#define MAX_POS ((int32_t)(70 * STEPS_PER_MM))
#define MAX_VELOCITY ((uint32_t)(10 * STEPS_PER_MM))
#define ACCEL ((uint32_t)(50 * STEPS_PER_MM))
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

// Klipper の stepper を模す。時刻は 64 bit の絶対時刻で追う。
// 実機では 32 bit の時刻を、いまから ±2^31 の範囲として解釈する（sched_add_timer）ので、
// ステップの時刻も区間の開始から ±2^31 の範囲として解く
struct sim {
    struct vs_axis axis;
    uint32_t period;
    uint64_t clock;       // 次の区間の開始時刻
    uint64_t step_clock;  // 最後のステップの時刻（ステップがなければ基準の時刻）
    int32_t position;     // dir=1 で増える
    int64_t steps;        // 打ったステップの総数
    uint64_t min_gap;     // ステップの最小間隔
    uint16_t max_count;   // 1 区間の最大のステップ数
    uint8_t has_step;
    // 立っていれば vs_axis_plan_to で move_target へ move_speed で向かう
    uint8_t moving;
    int32_t move_target;
    uint32_t move_speed;
};

static void
sim_init(struct sim *s, int32_t pos, uint32_t clock)
{
    *s = (struct sim){
        .axis.config = {.min_pos = 0, .max_pos = MAX_POS,
                        .max_velocity = MAX_VELOCITY, .accel = ACCEL},
        .period = PERIOD, .clock = clock, .step_clock = clock, .position = pos,
        .min_gap = UINT64_MAX,
    };
    vs_axis_reset(&s->axis, pos, clock);
}

static void
sim_step(struct sim *s, uint64_t time, uint8_t dir)
{
    if (s->has_step && time - s->step_clock < s->min_gap)
        s->min_gap = time - s->step_clock;
    s->step_clock = time;
    s->position += dir ? 1 : -1;
    s->steps++;
    s->has_step = 1;
}

// 1 区間を計画して展開する。ずれがあれば 0 を返す
static int
sim_segment(struct sim *s, int32_t target)
{
    struct vs_segment seg;
    uint64_t start = s->clock;
    if (s->moving)
        vs_axis_plan_to(&s->axis, s->move_target, s->move_speed, (uint32_t)start,
                        s->period, CLOCK_FREQ, &seg);
    else
        vs_axis_plan(&s->axis, target, (uint32_t)start, s->period, CLOCK_FREQ, &seg);
    s->clock += s->period;
    if (seg.count) {
        uint32_t time32 = (uint32_t)s->step_clock + seg.first_interval;
        uint64_t time = start + (int32_t)(time32 - (uint32_t)start);
        if (s->has_step && time <= s->step_clock) {
            printf("  ステップの時刻が戻った: first_interval=%u\n", seg.first_interval);
            return 0;
        }
        if (time < start) {
            printf("  区間の開始より前のステップ: %lld ticks\n",
                   (long long)(time - start));
            return 0;
        }
        sim_step(s, time, seg.dir);
        int short_count = seg.count - 1 - seg.long_count;
        for (int i = 1; i < seg.count; i++) {
            time += seg.interval + (i > short_count);
            sim_step(s, time, seg.dir);
        }
        if (time > start + s->period + LATE_TOLERANCE) {
            printf("  区間の終わりより後のステップ: %lld ticks\n",
                   (long long)(time - start - s->period));
            return 0;
        }
        if (seg.count > s->max_count)
            s->max_count = seg.count;
    }
    if (s->axis.last_step_clock != (uint32_t)s->step_clock) {
        printf("  last_step_clock=%u が stepper の %u と違う\n",
               s->axis.last_step_clock, (uint32_t)s->step_clock);
        return 0;
    }
    if (vs_axis_position(&s->axis) != s->position) {
        printf("  位置 %d が stepper の %d と違う\n",
               vs_axis_position(&s->axis), s->position);
        return 0;
    }
    return 1;
}

static int
sim_run(struct sim *s, int32_t target, int segments)
{
    for (int i = 0; i < segments; i++)
        if (!sim_segment(s, target))
            return 0;
    return 1;
}

// 目標位置へ segments 区間だけ向かう。区間ごとに加速度と速さの上限も確かめる
static int
sim_move(struct sim *s, int32_t target_pos, uint32_t speed, int segments)
{
    s->moving = 1;
    s->move_target = target_pos;
    s->move_speed = speed;
    for (int i = 0; i < segments; i++) {
        int32_t prev = s->axis.velocity;
        if (!sim_segment(s, 0))
            return 0;
        int32_t v = s->axis.velocity, dv = v - prev;
        if (dv > DV || dv < -DV) {
            printf("  区間 %d で dv=%d（上限 %d）\n", i, dv, DV);
            return 0;
        }
        if (v > (int32_t)speed || v < -(int32_t)speed) {
            printf("  区間 %d で v=%d が speed=%u を超えた\n", i, v, speed);
            return 0;
        }
    }
    s->moving = 0;
    return 1;
}

static void
test_reset_sets_position_and_stops(void)
{
    struct vs_axis a = {.velocity = 1234};
    vs_axis_reset(&a, 5000, 777);
    CHECK(vs_axis_position(&a) == 5000, "pos=%d", vs_axis_position(&a));
    CHECK(a.velocity == 0, "v=%d", a.velocity);
    CHECK(a.last_step_clock == 777, "clock=%u", a.last_step_clock);
}

static void
test_zero_target_makes_no_steps(void)
{
    struct sim s;
    sim_init(&s, 1000, 0);
    CHECK(sim_run(&s, 0, 100), "展開がずれた");
    CHECK(s.steps == 0, "steps=%lld", (long long)s.steps);
}

static void
test_acceleration_is_limited_per_segment(void)
{
    struct sim s;
    sim_init(&s, 1000, 0);
    int32_t prev = 0;
    for (int i = 0; i < 200; i++) {
        CHECK(sim_segment(&s, MAX_VELOCITY), "展開がずれた（区間 %d）", i);
        int32_t dv = s.axis.velocity - prev;
        CHECK(dv >= 0 && dv <= DV, "区間 %d で dv=%d（上限 %d）", i, dv, DV);
        prev = s.axis.velocity;
    }
}

static void
test_velocity_is_clamped_to_max(void)
{
    struct sim s;
    sim_init(&s, MAX_POS / 2, 0);
    CHECK(sim_run(&s, 10 * MAX_VELOCITY, 100), "展開がずれた");
    CHECK(s.axis.velocity == (int32_t)MAX_VELOCITY, "v=%d", s.axis.velocity);
    sim_init(&s, MAX_POS / 2, 0);
    CHECK(sim_run(&s, -10 * (int32_t)MAX_VELOCITY, 100), "展開がずれた");
    CHECK(s.axis.velocity == -(int32_t)MAX_VELOCITY, "v=%d", s.axis.velocity);
}

static void
test_constant_velocity_spacing(void)
{
    struct sim s;
    sim_init(&s, 1000, 0);
    CHECK(sim_run(&s, MAX_VELOCITY, 100), "展開がずれた");
    int64_t before = s.steps;
    CHECK(sim_run(&s, MAX_VELOCITY, 50), "展開がずれた");
    int64_t expected = (int64_t)MAX_VELOCITY * 50 * PERIOD / CLOCK_FREQ;
    CHECK(llabs(s.steps - before - expected) <= 1, "steps=%lld expected=%lld",
          (long long)(s.steps - before), (long long)expected);
    uint32_t ideal = CLOCK_FREQ / MAX_VELOCITY;
    CHECK(s.min_gap + 2 >= ideal, "最小間隔 %llu（理想 %u）",
          (unsigned long long)s.min_gap, ideal);
}

static void
test_stops_at_max_pos(void)
{
    struct sim s;
    sim_init(&s, MAX_POS - 20000, 0);
    for (int i = 0; i < 2000; i++) {
        CHECK(sim_segment(&s, MAX_VELOCITY), "展開がずれた（区間 %d）", i);
        CHECK(vs_axis_position(&s.axis) <= MAX_POS, "pos=%d", vs_axis_position(&s.axis));
    }
    CHECK(s.axis.velocity == 0, "v=%d", s.axis.velocity);
    CHECK(vs_axis_position(&s.axis) >= MAX_POS - 1, "pos=%d", vs_axis_position(&s.axis));
}

static void
test_stops_at_min_pos(void)
{
    struct sim s;
    sim_init(&s, 20000, 0);
    for (int i = 0; i < 2000; i++) {
        CHECK(sim_segment(&s, -(int32_t)MAX_VELOCITY), "展開がずれた（区間 %d）", i);
        CHECK(vs_axis_position(&s.axis) >= 0, "pos=%d", vs_axis_position(&s.axis));
    }
    CHECK(s.axis.velocity == 0, "v=%d", s.axis.velocity);
    CHECK(vs_axis_position(&s.axis) <= 1, "pos=%d", vs_axis_position(&s.axis));
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
        int32_t dv = s.axis.velocity - prev;
        CHECK(dv >= -DV && dv <= DV, "区間 %d で dv=%d（上限 %d）", i, dv, DV);
        prev = s.axis.velocity;
    }
    CHECK(vs_axis_position(&s.axis) <= MAX_POS, "pos=%d", vs_axis_position(&s.axis));
    CHECK(s.axis.velocity == 0, "v=%d", s.axis.velocity);
}

static void
test_negative_range(void)
{
    // 可動域が負の側にもあるとき、負の端で止まる
    struct sim s;
    sim_init(&s, -40000, 0);
    s.axis.config.min_pos = -50000;
    s.axis.config.max_pos = 50000;
    CHECK(sim_run(&s, -(int32_t)MAX_VELOCITY, 2000), "展開がずれた");
    CHECK(vs_axis_position(&s.axis) >= -50000, "pos=%d", vs_axis_position(&s.axis));
    CHECK(vs_axis_position(&s.axis) <= -49999, "pos=%d", vs_axis_position(&s.axis));
    CHECK(s.axis.velocity == 0, "v=%d", s.axis.velocity);
}

static void
test_extreme_targets_are_clamped(void)
{
    struct sim s;
    sim_init(&s, MAX_POS / 2, 0);
    CHECK(sim_run(&s, INT32_MIN, 100), "展開がずれた");
    CHECK(s.axis.velocity == -(int32_t)MAX_VELOCITY, "v=%d", s.axis.velocity);
    CHECK(sim_run(&s, INT32_MAX, 300), "展開がずれた");
    CHECK(s.axis.velocity == (int32_t)MAX_VELOCITY, "v=%d", s.axis.velocity);
}

static void
test_outside_range_does_not_jump(void)
{
    // 位置が可動域の外にあっても境界へ飛ばない。外向きには動かず、内向きには加速度の範囲で戻る
    struct sim s;
    int32_t outside = MAX_POS + 100000;
    sim_init(&s, outside, 0);
    CHECK(sim_run(&s, 0, 10), "展開がずれた");
    CHECK(s.steps == 0, "目標 0 で steps=%lld", (long long)s.steps);
    CHECK(sim_run(&s, MAX_VELOCITY, 10), "展開がずれた");
    CHECK(vs_axis_position(&s.axis) == outside, "外向きに動いた: pos=%d",
          vs_axis_position(&s.axis));
    for (int i = 0; i < 100; i++) {
        int32_t prev = s.axis.velocity;
        CHECK(sim_segment(&s, -(int32_t)MAX_VELOCITY), "展開がずれた（区間 %d）", i);
        CHECK(prev - s.axis.velocity <= DV, "区間 %d で急に動いた: v=%d", i,
              s.axis.velocity);
    }
    CHECK(s.max_count <= MAX_VELOCITY * (uint64_t)PERIOD / CLOCK_FREQ + 1,
          "1 区間に %u 歩", s.max_count);
    CHECK(vs_axis_position(&s.axis) < outside, "戻らない: pos=%d",
          vs_axis_position(&s.axis));
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
    int64_t before = s.steps;
    CHECK(sim_run(&s, MAX_VELOCITY, 50), "展開がずれた");
    CHECK(s.steps > before, "動かない");
}

static void
test_large_segment(void)
{
    // 長い区間（100 ms）と速い上限（500000 steps/s）で、1 区間に約 50000 歩
    struct sim s;
    sim_init(&s, 1000, 0);
    s.period = CLOCK_FREQ / 10;
    s.axis.config.max_pos = INT32_MAX;
    s.axis.config.max_velocity = 500000;
    s.axis.config.accel = 1000000000;
    CHECK(sim_run(&s, 500000, 20), "展開がずれた");
    CHECK(s.max_count >= 49999 && s.max_count <= 50001, "max_count=%u", s.max_count);
}

static void
test_move_stops_exactly_at_target(void)
{
    // 正の向きにも負の向きにも、目標位置でちょうど止まる（ジョグの 0.1 mm ≒ 853 歩、10 mm ≒ 85333 歩）
    struct sim s;
    sim_init(&s, MAX_POS / 2, 0);
    int32_t targets[] = {MAX_POS / 2 + 853, MAX_POS / 2 + 853 + 85333, MAX_POS / 2 - 1, MAX_POS / 2 - 2};
    for (unsigned i = 0; i < sizeof(targets) / sizeof(targets[0]); i++) {
        CHECK(sim_move(&s, targets[i], MAX_VELOCITY, 2000), "展開がずれた（目標 %u）", i);
        CHECK(vs_axis_position(&s.axis) == targets[i], "目標 %d に対して pos=%d",
              targets[i], vs_axis_position(&s.axis));
        CHECK(s.axis.velocity == 0, "v=%d", s.axis.velocity);
        CHECK(vs_axis_arrived(&s.axis, targets[i]), "着いたと判定しない（目標 %u）", i);
    }
}

static void
test_arrived_needs_target_and_rest(void)
{
    struct sim s;
    sim_init(&s, 5000, 0);
    CHECK(vs_axis_arrived(&s.axis, 5000), "止まって目標にいる");
    CHECK(!vs_axis_arrived(&s.axis, 5001), "目標にいない");
    // 可動域の外の目標は、可動域の端に着いたら着いたとみなす
    sim_init(&s, MAX_POS, 0);
    CHECK(vs_axis_arrived(&s.axis, MAX_POS + 1000), "端に着いている");
    sim_init(&s, 5000, 0);
    CHECK(sim_run(&s, MAX_VELOCITY, 5), "展開がずれた");
    CHECK(!vs_axis_arrived(&s.axis, vs_axis_position(&s.axis)), "動いている");
}

static void
test_move_respects_speed(void)
{
    // 遅い speed では、その速さで頭打ちになる
    struct sim s;
    sim_init(&s, 1000, 0);
    uint32_t slow = MAX_VELOCITY / 10;
    CHECK(sim_move(&s, 1000 + 40000, slow, 300), "展開がずれた");
    CHECK(s.axis.velocity == (int32_t)slow, "v=%d", s.axis.velocity);
    CHECK(sim_move(&s, 1000 + 40000, slow, 3000), "展開がずれた");
    CHECK(vs_axis_position(&s.axis) == 1000 + 40000, "pos=%d", vs_axis_position(&s.axis));
}

static void
test_move_reverses_toward_a_target_behind(void)
{
    // 正の向きに最高速度で動いている最中に、後ろの目標を指示されても、減速・反転してちょうど止まる
    struct sim s;
    sim_init(&s, MAX_POS / 2, 0);
    CHECK(sim_run(&s, MAX_VELOCITY, 150), "展開がずれた");
    int32_t behind = MAX_POS / 2;
    CHECK(sim_move(&s, behind, MAX_VELOCITY, 3000), "展開がずれた");
    CHECK(vs_axis_position(&s.axis) == behind, "pos=%d", vs_axis_position(&s.axis));
    CHECK(s.axis.velocity == 0, "v=%d", s.axis.velocity);
}

static void
test_move_to_a_near_target_ahead_while_moving(void)
{
    // 最高速度で動いている最中に、制動距離（約 8533 歩）より近い前方の目標（0.1 mm = 853 歩）を指示しても、
    // 加速度の範囲で減速し、行き過ぎてから戻って目標にちょうど止まる
    struct sim s;
    sim_init(&s, MAX_POS / 2, 0);
    CHECK(sim_run(&s, MAX_VELOCITY, 150), "展開がずれた");
    int32_t target = vs_axis_position(&s.axis) + 853;
    CHECK(sim_move(&s, target, MAX_VELOCITY, 3000), "展開がずれた");
    CHECK(vs_axis_position(&s.axis) == target, "pos=%d target=%d",
          vs_axis_position(&s.axis), target);
    CHECK(vs_axis_arrived(&s.axis, target), "着いたと判定しない");
}

static void
test_move_to_targets_while_moving_keeps_acceleration(void)
{
    // 動いている最中に、いろいろな近さ・向きの目標を指示しても、加速度を守って目標に止まる（決まった乱数列）
    uint32_t seed = 777;
    for (int trial = 0; trial < 300; trial++) {
        struct sim s;
        sim_init(&s, MAX_POS / 2, 0);
        seed = seed * 1103515245 + 12345;
        int32_t v = (int32_t)(seed >> 8) % (2 * (int32_t)MAX_VELOCITY) - (int32_t)MAX_VELOCITY;
        CHECK(sim_run(&s, v, 100), "展開がずれた（試行 %d）", trial);
        seed = seed * 1103515245 + 12345;
        int32_t target = vs_axis_position(&s.axis) + (int32_t)(seed >> 8) % 40000 - 20000;
        CHECK(sim_move(&s, target, MAX_VELOCITY, 3000), "展開がずれた（試行 %d）", trial);
        CHECK(vs_axis_arrived(&s.axis, target), "試行 %d で着かない: pos=%d target=%d",
              trial, vs_axis_position(&s.axis), target);
    }
}

static void
test_move_arrives_with_the_minimum_accel(void)
{
    // 加速度が下限（VS_MIN_ACCEL）でも、1 歩未満の端数を詰め切って目標に着く
    uint32_t seed = 4242;
    for (int trial = 0; trial < 100; trial++) {
        struct sim s;
        sim_init(&s, MAX_POS / 2, 0);
        s.axis.config.accel = VS_MIN_ACCEL;
        seed = seed * 1103515245 + 12345;
        int32_t target = MAX_POS / 2 + (int32_t)(seed >> 8) % 4000 - 2000;
        CHECK(sim_move(&s, target, MAX_VELOCITY / 20, 20000), "展開がずれた（試行 %d）", trial);
        CHECK(vs_axis_arrived(&s.axis, target), "試行 %d で着かない: pos=%d target=%d",
              trial, vs_axis_position(&s.axis), target);
    }
}

static void
test_move_target_outside_range_while_moving_away(void)
{
    // 負の向きに動いている最中に、可動域の外の正の目標を指示しても、反転して可動域の端で止まる
    struct sim s;
    sim_init(&s, MAX_POS - 50000, 0);
    CHECK(sim_run(&s, -(int32_t)MAX_VELOCITY, 150), "展開がずれた");
    CHECK(sim_move(&s, MAX_POS + 100000, MAX_VELOCITY, 3000), "展開がずれた");
    CHECK(vs_axis_position(&s.axis) == MAX_POS, "pos=%d", vs_axis_position(&s.axis));
    CHECK(vs_axis_arrived(&s.axis, MAX_POS + 100000), "着いたと判定しない");
}

static void
test_move_target_outside_range_stops_at_the_limit(void)
{
    struct sim s;
    sim_init(&s, MAX_POS - 5000, 0);
    CHECK(sim_move(&s, MAX_POS + 100000, MAX_VELOCITY, 2000), "展開がずれた");
    CHECK(vs_axis_position(&s.axis) == MAX_POS, "pos=%d", vs_axis_position(&s.axis));
    CHECK(s.axis.velocity == 0, "v=%d", s.axis.velocity);
}

static void
test_move_to_current_position_makes_no_steps(void)
{
    struct sim s;
    sim_init(&s, 12345, 0);
    CHECK(sim_move(&s, 12345, MAX_VELOCITY, 100), "展開がずれた");
    CHECK(s.steps == 0, "steps=%lld", (long long)s.steps);
}

static void
test_reversal_keeps_steps_in_order(void)
{
    struct sim s;
    sim_init(&s, MAX_POS / 2, 0);
    CHECK(sim_run(&s, MAX_VELOCITY, 150), "展開がずれた");
    int32_t turn = vs_axis_position(&s.axis);
    CHECK(sim_run(&s, -(int32_t)MAX_VELOCITY, 300), "展開がずれた");
    CHECK(s.axis.velocity == -(int32_t)MAX_VELOCITY, "v=%d", s.axis.velocity);
    CHECK(vs_axis_position(&s.axis) < turn, "pos=%d turn=%d",
          vs_axis_position(&s.axis), turn);
}

static void
test_rapid_reversals_keep_a_gap(void)
{
    // 目標速度を区間ごとにばらばらに変えて（決まった乱数列）向きを何度も変えても、
    // ステップの間隔が方向切り替えの最小間隔を下回らない
    struct sim s;
    sim_init(&s, MAX_POS / 2, 0);
    s.axis.config.accel = 1000000000;
    uint32_t seed = 12345;
    for (int i = 0; i < 100000; i++) {
        seed = seed * 1103515245 + 12345;
        int32_t target = (int32_t)(seed >> 8) % 20000 - 10000;
        CHECK(sim_segment(&s, target), "展開がずれた（区間 %d）", i);
    }
    CHECK(s.steps > 0, "steps=%lld", (long long)s.steps);
    CHECK(s.min_gap >= VS_MIN_GAP_TICKS(CLOCK_FREQ), "最小間隔 %llu",
          (unsigned long long)s.min_gap);
}

static void
test_slow_steps_span_segments(void)
{
    // 区間より長い間隔のステップ（100 steps/s = 120000 ticks ごと）
    struct sim s;
    sim_init(&s, 1000, 0);
    s.axis.config.accel = 100000000;
    CHECK(sim_run(&s, 100, 500), "展開がずれた");
    // 500 区間 = 1 秒で約 100 歩
    CHECK(s.steps >= 99 && s.steps <= 101, "steps=%lld", (long long)s.steps);
    CHECK(s.min_gap + 2 >= CLOCK_FREQ / 100, "最小間隔 %llu",
          (unsigned long long)s.min_gap);
}

static void
test_clock_wraparound(void)
{
    // 32 bit の時刻が一周しても、間隔と位置がずれない
    struct sim s;
    sim_init(&s, 1000, UINT32_MAX - 50 * PERIOD);
    CHECK(sim_run(&s, MAX_VELOCITY, 200), "展開がずれた");
    CHECK(s.steps > 0, "steps=%lld", (long long)s.steps);
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
    test_move_stops_exactly_at_target();
    test_arrived_needs_target_and_rest();
    test_move_respects_speed();
    test_move_reverses_toward_a_target_behind();
    test_move_to_a_near_target_ahead_while_moving();
    test_move_to_targets_while_moving_keeps_acceleration();
    test_move_target_outside_range_while_moving_away();
    test_move_arrives_with_the_minimum_accel();
    test_move_target_outside_range_stops_at_the_limit();
    test_move_to_current_position_makes_no_steps();
    test_reversal_keeps_steps_in_order();
    test_rapid_reversals_keep_a_gap();
    test_slow_steps_span_segments();
    test_clock_wraparound();
    if (failures) {
        printf("%d failure(s)\n", failures);
        return 1;
    }
    printf("ok\n");
    return 0;
}
