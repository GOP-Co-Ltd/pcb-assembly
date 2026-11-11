// SPDX-License-Identifier: GPL-3.0-only
//
// 速度指令から XYZ ステージのステップ区間を作る計算（Klipper に依存しない）。
//
// 時間を一定長の区間に分け、区間ごとに直交座標の各軸（vs_axis）の速度を目標へ加速度の
// 制限つきで近づけ、速度上限と可動域の手前の制動で制限する。区間の中は平均速度の等速とみなす。
// 軸の位置を kinematics でモーターの位置へ写し、モーター（vs_motor）ごとに整数ステップを
// 横切る時刻をステップにする。結果は Klipper の queue_step（直前のステップからの間隔）で表せる形で返す。
//
// 位置の単位はステップ。corexy では X と Y の 1 mm あたりのステップ数が同じことを前提にする
// （直交座標の 1 ステップ = モーターの 1 ステップ）。
#ifndef __VS_PLANNER_H
#define __VS_PLANNER_H

#include <stdint.h>

#define VS_AXES 3
// 位置の固定小数点の小数部のビット数
#define VS_POS_SHIFT 16
// 区間の先頭のステップを、直前のステップからあける最小の時間（2 µs）。
// 区間の境目で向きが変わっても、ドライバが方向を取り込む時間を残す
#define VS_MIN_GAP_TICKS(clock_freq) ((clock_freq) / 500000)

// 直交座標の軸（x, y, z）からモーター（stepper_x, stepper_y, stepper_z）への写し方。
// Klipper の printer.cfg の kinematics と同じ向き
enum {
    VS_CARTESIAN,        // a = x、b = y
    VS_COREXY,           // a = x + y、b = x - y
    VS_INVERSED_COREXY,  // a = x - y、b = x + y（klipper/inversed_corexy.py）
    VS_KINEMATICS_COUNT
};

struct vs_axis_config {
    int32_t min_pos, max_pos;  // 可動域（ステップ）
    uint32_t max_velocity;     // steps/s
    uint32_t accel;            // steps/s^2
};

// 直交座標の 1 軸
struct vs_axis {
    struct vs_axis_config config;
    int64_t pos;       // 位置（ステップ、小数部 VS_POS_SHIFT ビット）
    int32_t velocity;  // steps/s
};

// モーター 1 つ。pos はモーターの座標（stepper の位置、向きは dir=1 で増える側）
struct vs_motor {
    int64_t pos;               // 位置（ステップ、小数部 VS_POS_SHIFT ビット）
    uint32_t last_step_clock;  // 最後のステップの時刻（stepper が計算するのと同じ値）
    // 最後のステップが、計画中の区間の開始より最小間隔以上前にある。
    // 32 bit の時刻の差は 2^31 tick で一周するので、止まっている間も区間ごとに判定して覚えておく
    uint8_t rested;
};

// 1 区間・1 モーターぶんのステップ。count == 0 ならステップなし。
// 先頭の 1 歩は直前のステップから first_interval 後。残りの count - 1 歩は、
// はじめの count - 1 - long_count 歩が interval ごと、続く long_count 歩が interval + 1 ごと。
// 間隔を 2 段に分けるのは、区間の最後のステップを正確な時刻に合わせ、丸めの誤差を次の区間へ持ち越さないため。
struct vs_segment {
    uint32_t first_interval, interval;  // clock tick
    uint16_t count, long_count;
    uint8_t dir;  // 1 なら正の向き
};

// XYZ ステージ。直交座標の位置 = to_axes(モーターの位置) + offset
struct vs_stage {
    uint8_t kinematics;
    struct vs_axis axes[VS_AXES];
    struct vs_motor motors[VS_AXES];
    int64_t offset[VS_AXES];  // 小数部 VS_POS_SHIFT ビット
};

// 直交座標とモーターの座標を写す（どちらも小数部 VS_POS_SHIFT ビット）
void vs_kin_to_motors(uint8_t kinematics, const int64_t axes[VS_AXES],
                      int64_t motors[VS_AXES]);
void vs_kin_to_axes(uint8_t kinematics, const int64_t motors[VS_AXES],
                    int64_t axes[VS_AXES]);

// 速度を目標へ近づけ、[区間の開始, + period) の分だけ位置を進める。
// 位置が可動域の外にあるときは、外向きには動かさず、内向きには加速度の範囲で戻す。
void vs_axis_plan(struct vs_axis *a, int32_t target_velocity, uint32_t period,
                  uint32_t clock_freq);

// モーターを pos ステップに置く。clock は次の区間の基準になる時刻。
void vs_motor_reset(struct vs_motor *m, int32_t pos, uint32_t clock);

// 整数ステップの位置（これまでに作ったステップの数と一致する）。
int32_t vs_motor_position(const struct vs_motor *m);

// [seg_start, seg_start + period) の区間で、位置を new_pos まで等速で進めるステップを out に返す。
// 区間は途切れず順に渡す（止まっている間も呼ぶ）。前提:
// - 1 区間のステップ数が 60000 未満（uint16_t に収まる）
// - モーターの速さが clock_freq / VS_MIN_GAP_TICKS 以下
void vs_motor_plan(struct vs_motor *m, int64_t new_pos, uint32_t seg_start,
                   uint32_t period, uint32_t clock_freq, struct vs_segment *out);

// 軸 axis の直交座標の位置を pos ステップに置く（モーターは動かさず、offset を変える）。
// motors は各モーターの stepper の位置。止まっている間に呼び、動かす前に vs_stage_reset を呼ぶ。
void vs_stage_set_position(struct vs_stage *s, const int32_t motors[VS_AXES],
                           uint8_t axis, int32_t pos);

// モーターを stepper の位置に、軸をそこから求めた位置に置き、速度を 0 にする。
void vs_stage_reset(struct vs_stage *s, const int32_t motors[VS_AXES], uint32_t clock);

// stepper の位置から求めた、軸 axis の直交座標の位置（整数ステップ、floor）。
int32_t vs_stage_position(const struct vs_stage *s, const int32_t motors[VS_AXES],
                          uint8_t axis);

// 1 区間を計画する。target は直交座標の目標速度（steps/s）、out はモーターごとのステップ。
// 前提（vs_motor_plan のモーターの速さ）は、corexy では X と Y の max_velocity の和で守る。
// max_velocity は 500000 以下、accel は 1e9 以下（int64 があふれない）。
void vs_stage_plan(struct vs_stage *s, const int32_t target[VS_AXES], uint32_t seg_start,
                   uint32_t period, uint32_t clock_freq, struct vs_segment out[VS_AXES]);

#endif // vs_planner.h
