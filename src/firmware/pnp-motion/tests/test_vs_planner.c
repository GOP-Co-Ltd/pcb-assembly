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

// Klipper の stepper を模す。ステップの時刻と位置を追う
struct sim {
    struct vs_axis axis;
    uint32_t clock;       // 次の区間の開始時刻
    uint32_t step_clock;  // 最後のステップの時刻
    int32_t position;     // dir=1 で増える
    int64_t steps;        // 打ったステップの総数
    uint32_t min_gap;     // ステップの最小間隔
    uint8_t has_step;
};

static void
sim_init(struct sim *s, int32_t pos, uint32_t clock)
{
    *s = (struct sim){
        .axis.config = {.min_pos = 0, .max_pos = MAX_POS,
                        .max_velocity = MAX_VELOCITY, .accel = ACCEL},
        .clock = clock, .step_clock = clock, .position = pos,
        .min_gap = UINT32_MAX,
    };
    vs_axis_reset(&s->axis, pos, clock);
}

static void
sim_step(struct sim *s, uint32_t time, uint8_t dir)
{
    if (s->has_step) {
        uint32_t gap = time - s->step_clock;
        if (gap < s->min_gap)
            s->min_gap = gap;
    }
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
    uint32_t start = s->clock;
    vs_axis_plan(&s->axis, target, start, PERIOD, CLOCK_FREQ, &seg);
    s->clock += PERIOD;
    if (seg.count) {
        uint32_t time = s->step_clock + seg.first_interval;
        if ((int32_t)(time - s->step_clock) <= 0 && s->has_step) {
            printf("  ステップの時刻が戻った: first_interval=%u\n", seg.first_interval);
            return 0;
        }
        if ((int32_t)(time - start) < 0) {
            printf("  区間の開始より前のステップ: %d ticks\n", (int32_t)(time - start));
            return 0;
        }
        sim_step(s, time, seg.dir);
        int short_count = seg.count - 1 - seg.long_count;
        for (int i = 1; i < seg.count; i++) {
            time += seg.interval + (i > short_count);
            sim_step(s, time, seg.dir);
        }
        if ((int32_t)(time - (start + PERIOD)) > LATE_TOLERANCE) {
            printf("  区間の終わりより後のステップ: %d ticks\n",
                   (int32_t)(time - (start + PERIOD)));
            return 0;
        }
    }
    if (s->axis.last_step_clock != s->step_clock) {
        printf("  last_step_clock=%u が stepper の %u と違う\n",
               s->axis.last_step_clock, s->step_clock);
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
    CHECK(s.min_gap + 2 >= ideal, "最小間隔 %u（理想 %u）", s.min_gap, ideal);
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
    // 最高速度で可動域の端へ向かっても、手前で減速して越えない
    struct sim s;
    sim_init(&s, MAX_POS / 2, 0);
    CHECK(sim_run(&s, MAX_VELOCITY, 3000), "展開がずれた");
    CHECK(vs_axis_position(&s.axis) <= MAX_POS, "pos=%d", vs_axis_position(&s.axis));
    CHECK(s.axis.velocity == 0, "v=%d", s.axis.velocity);
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
    CHECK(s.min_gap >= VS_MIN_GAP_TICKS(CLOCK_FREQ), "最小間隔 %u", s.min_gap);
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
    CHECK(s.min_gap + 2 >= CLOCK_FREQ / 100, "最小間隔 %u", s.min_gap);
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
