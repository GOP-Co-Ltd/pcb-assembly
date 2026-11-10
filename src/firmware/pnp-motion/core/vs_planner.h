// SPDX-License-Identifier: GPL-3.0-only
//
// 速度指令から 1 軸ぶんのステップ区間を作る計算（Klipper に依存しない）。
//
// 時間を一定長の区間に分け、区間ごとに目標速度へ加速度の制限つきで近づける。
// 区間の中は平均速度の等速とみなし、位置が整数ステップを横切る時刻をステップにする。
// 結果は Klipper の queue_step（直前のステップからの間隔）で表せる形で返す。
#ifndef __VS_PLANNER_H
#define __VS_PLANNER_H

#include <stdint.h>

// 位置の固定小数点の小数部のビット数
#define VS_POS_SHIFT 16
// 加速度の下限（steps/s^2）。1 歩未満の残りの制動の速さ sqrt(2·a·r) が 0 に丸まらず、
// 目標位置への移動が端数を詰め切れる
#define VS_MIN_ACCEL 65536
// 区間の先頭のステップを、直前のステップからあける最小の時間（2 µs）。
// 区間の境目で向きが変わっても、ドライバが方向を取り込む時間を残す
#define VS_MIN_GAP_TICKS(clock_freq) ((clock_freq) / 500000)

struct vs_axis_config {
    int32_t min_pos, max_pos;  // 可動域（ステップ）
    uint32_t max_velocity;     // steps/s
    uint32_t accel;            // steps/s^2
};

// 1 区間ぶんのステップ。count == 0 ならステップなし。
// 先頭の 1 歩は直前のステップから first_interval 後。残りの count - 1 歩は、
// はじめの count - 1 - long_count 歩が interval ごと、続く long_count 歩が interval + 1 ごと。
// 間隔を 2 段に分けるのは、区間の最後のステップを正確な時刻に合わせ、丸めの誤差を次の区間へ持ち越さないため。
struct vs_segment {
    uint32_t first_interval, interval;  // clock tick
    uint16_t count, long_count;
    uint8_t dir;  // 1 なら正の向き
};

struct vs_axis {
    struct vs_axis_config config;
    int64_t pos;               // 位置（ステップ、小数部 VS_POS_SHIFT ビット）
    int32_t velocity;          // steps/s
    uint32_t last_step_clock;  // 最後のステップの時刻（stepper が計算するのと同じ値）
    // 最後のステップが、計画中の区間の開始より最小間隔以上前にある。
    // 32 bit の時刻の差は 2^31 tick で一周するので、止まっている間も区間ごとに判定して覚えておく
    uint8_t rested;
};

// 位置を pos ステップに置き、速度を 0 にする。clock は次の区間の基準になる時刻。
void vs_axis_reset(struct vs_axis *a, int32_t pos, uint32_t clock);

// 整数ステップの位置（これまでに作ったステップの数と一致する）。
int32_t vs_axis_position(const struct vs_axis *a);

// [seg_start, seg_start + period) の区間を進め、その区間のステップを out に返す。
// 区間は途切れず順に渡す（止まっている間も呼ぶ）。前提:
// - max_velocity * period / clock_freq < 60000（1 区間のステップ数が uint16_t に収まる）
// - max_velocity <= clock_freq / VS_MIN_GAP_TICKS、VS_MIN_ACCEL <= accel <= 1e9（int64 があふれない）
// 位置が可動域の外にあるときは、外向きには動かさず、内向きには加速度の範囲で戻す。
void vs_axis_plan(struct vs_axis *a, int32_t target_velocity, uint32_t seg_start,
                  uint32_t period, uint32_t clock_freq, struct vs_segment *out);

// vs_axis_plan と同じだが、目標位置 target_pos（ステップ）へ速さ speed（steps/s）で向かい、
// ちょうど止まる。目標が可動域の外なら可動域の端で止まる。前提は vs_axis_plan と同じ。
void vs_axis_plan_to(struct vs_axis *a, int32_t target_pos, uint32_t speed,
                     uint32_t seg_start, uint32_t period, uint32_t clock_freq,
                     struct vs_segment *out);

// vs_axis_plan_to の目標（可動域に切り詰めたもの）に着いて止まっているか。
int vs_axis_arrived(const struct vs_axis *a, int32_t target_pos);

#endif // vs_planner.h
