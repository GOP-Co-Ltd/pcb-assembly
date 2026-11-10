// SPDX-License-Identifier: GPL-3.0-only
//
// 速度指令から 1 軸ぶんのステップ区間を作る計算（Klipper に依存しない）。

#include "vs_planner.h"

#define ONE ((int64_t)1 << VS_POS_SHIFT)

static int64_t
clamp64(int64_t value, int64_t low, int64_t high)
{
    return value < low ? low : (value > high ? high : value);
}

static int64_t
abs64(int64_t value)
{
    return value < 0 ? -value : value;
}

static uint64_t
isqrt64(uint64_t x)
{
    uint64_t root = 0, bit = (uint64_t)1 << 62;
    while (bit > x)
        bit >>= 2;
    while (bit) {
        if (x >= root + bit) {
            x -= root + bit;
            root = (root >> 1) + bit;
        } else {
            root >>= 1;
        }
        bit >>= 2;
    }
    return root;
}

// 残り room（小数部つき）で止まり切れる速さ sqrt(2·a·d)
static int64_t
brake_velocity(uint32_t accel, int64_t room)
{
    if (room <= 0)
        return 0;
    // 短い残り（256 歩未満）は小数部まで使う。1 歩未満の残りも詰め切って、目標でちょうど止まるように。
    // sqrt(2·a·room·2^16) / 2^8 = sqrt(2·a·room)。accel <= 1e9 なら int64 に収まる
    if (room < ((int64_t)256 << VS_POS_SHIFT))
        return (int64_t)(isqrt64(2 * (uint64_t)accel * (uint64_t)room) >> (VS_POS_SHIFT / 2));
    return (int64_t)isqrt64(2 * (uint64_t)accel * (uint64_t)(room >> VS_POS_SHIFT));
}

void
vs_axis_reset(struct vs_axis *a, int32_t pos, uint32_t clock)
{
    a->pos = (int64_t)pos * ONE;
    a->velocity = 0;
    a->last_step_clock = clock;
    // clock は基準の時刻で、実際のステップではない
    a->rested = 1;
}

int32_t
vs_axis_position(const struct vs_axis *a)
{
    // 算術シフトなので負の位置でも floor になる
    return (int32_t)(a->pos >> VS_POS_SHIFT);
}

// 目標速度を、速度上限と境界 [low, high]（小数部つき）の手前で止まれる速さに制限する
static int64_t
limit_target(const struct vs_axis *a, int32_t target, int64_t low, int64_t high,
             uint32_t period, uint32_t clock_freq)
{
    const struct vs_axis_config *c = &a->config;
    int64_t max_v = c->max_velocity;
    int64_t v = clamp64(target, -max_v, max_v);
    // 次の区間で進む分を見込んでから、残りの距離で制動する
    int64_t ahead = abs64(a->velocity) * period * ONE / clock_freq;
    int64_t room_pos = high - a->pos - ahead;
    int64_t room_neg = a->pos - low - ahead;
    return clamp64(v, -brake_velocity(c->accel, room_neg),
                   brake_velocity(c->accel, room_pos));
}

// 境界 [low, high]（ステップ）の中で、目標速度へ向かって 1 区間進める。
// 可動域なら可動域の端で、目標位置なら目標でちょうど止まる
static void
plan_within(struct vs_axis *a, int32_t target_velocity, int32_t low_pos,
            int32_t high_pos, uint32_t seg_start, uint32_t period,
            uint32_t clock_freq, struct vs_segment *out)
{
    const struct vs_axis_config *c = &a->config;
    uint32_t min_gap = VS_MIN_GAP_TICKS(clock_freq);
    if ((int32_t)(seg_start - a->last_step_clock) >= (int32_t)min_gap)
        a->rested = 1;
    int64_t low = (int64_t)low_pos * ONE, high = (int64_t)high_pos * ONE;

    // 速度を目標へ、区間あたり dv だけ近づける
    int64_t target = limit_target(a, target_velocity, low, high, period, clock_freq);
    int64_t dv = ((uint64_t)c->accel * period + clock_freq - 1) / clock_freq;
    int64_t v0 = a->velocity;
    int64_t v1 = clamp64(target, v0 - dv, v0 + dv);

    // 区間の中は平均速度の等速。境界で打ち切ったら止める。
    // 位置が境界の外なら、いまの位置を境界にする（外向きに動かさず、境界へも飛ばない）
    int64_t old_pos = a->pos;
    int64_t new_pos = old_pos + (v0 + v1) * period * ONE / (2 * (int64_t)clock_freq);
    if (old_pos < low)
        low = old_pos;
    if (old_pos > high)
        high = old_pos;
    if (new_pos <= low || new_pos >= high) {
        new_pos = clamp64(new_pos, low, high);
        v1 = 0;
    }
    a->pos = new_pos;
    a->velocity = v1;

    int64_t n_old = old_pos >> VS_POS_SHIFT, n_new = new_pos >> VS_POS_SHIFT;
    *out = (struct vs_segment){0};
    if (n_new == n_old)
        return;

    // 整数ステップを横切る時刻。区間の中は等速なので、最初と最後の時刻から間隔が決まる
    int64_t travel = abs64(new_pos - old_pos);
    uint8_t dir = n_new > n_old;
    uint16_t count = (uint16_t)abs64(n_new - n_old);
    int64_t first_frac = dir ? (n_old + 1) * ONE - old_pos : old_pos - n_old * ONE;
    int64_t last_frac = first_frac + (int64_t)(count - 1) * ONE;
    uint32_t first = seg_start + (uint32_t)(first_frac * period / travel);
    uint32_t span = (uint32_t)(last_frac * period / travel) - (first - seg_start);
    // 前の区間の最後のステップに近すぎるときは、最小の間隔だけ後ろへ回す（区間の中の間隔は保つ）
    uint32_t min_first = a->last_step_clock + min_gap;
    if (!a->rested && (int32_t)(first - min_first) < 0)
        first = min_first;
    a->rested = 0;

    out->first_interval = first - a->last_step_clock;
    out->count = count;
    out->dir = dir;
    if (count > 1) {
        out->interval = span / (count - 1);
        out->long_count = span % (count - 1);
    }
    a->last_step_clock = first + span;
}

void
vs_axis_plan(struct vs_axis *a, int32_t target_velocity, uint32_t seg_start,
             uint32_t period, uint32_t clock_freq, struct vs_segment *out)
{
    plan_within(a, target_velocity, a->config.min_pos, a->config.max_pos,
                seg_start, period, clock_freq, out);
}

void
vs_axis_plan_to(struct vs_axis *a, int32_t target_pos, uint32_t speed,
                uint32_t seg_start, uint32_t period, uint32_t clock_freq,
                struct vs_segment *out)
{
    // 目標の側の境界を目標に縮め、目標へ向かう速度を目標速度にする
    const struct vs_axis_config *c = &a->config;
    int32_t low = c->min_pos, high = c->max_pos;
    int32_t target = (int32_t)clamp64(target_pos, low, high);
    int64_t goal = (int64_t)target * ONE;
    int64_t dist = goal - a->pos, v = a->velocity;
    if ((dist > 0 && v > 0) || (dist < 0 && v < 0)) {
        // 目標へ向かって動いていて、目標の手前で止まりきれない（制動距離に 1 区間の先読みと
        // 1 区間ぶんの速度の余裕を足す）なら、境界を縮めずに 0 まで減速する。目標で打ち切ると
        // 加速度を破って急停止するため。行き過ぎて止まったら、次の区間から目標へ戻る
        int64_t dv = ((uint64_t)c->accel * period + clock_freq - 1) / clock_freq;
        int64_t speed = abs64(v) + dv;
        // ステップ単位で割ってから小数部を付ける（先に ONE を掛けると、受け付ける範囲の端
        // （accel 1e9、period 100 ms）で int64 があふれる）。切り捨てた端数は (+1) で補う
        int64_t stop = (speed * speed / (2 * (int64_t)c->accel)
                        + speed * period / clock_freq + 2) * ONE;
        if (stop >= abs64(dist)) {
            plan_within(a, 0, low, high, seg_start, period, clock_freq, out);
            return;
        }
    }
    int32_t velocity = 0;
    if (a->pos < goal) {
        high = target;
        velocity = (int32_t)clamp64(speed, 0, INT32_MAX);
    } else if (a->pos > goal) {
        low = target;
        velocity = -(int32_t)clamp64(speed, 0, INT32_MAX);
    }
    plan_within(a, velocity, low, high, seg_start, period, clock_freq, out);
}

int
vs_axis_arrived(const struct vs_axis *a, int32_t target_pos)
{
    int64_t goal = clamp64(target_pos, a->config.min_pos, a->config.max_pos);
    return !a->velocity && a->pos == goal * ONE;
}
