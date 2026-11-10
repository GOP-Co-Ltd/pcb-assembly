# pnp-motion ファーム

PnP 機の XYZ ステージ（BTT SKR Pico）を、ホストから速度指令で動かすファーム。
Klipper の MCU ファームに、モジュール `velocity_stream` を足したもの。
設計と理由は [ピンセット式 PnP の運動制御](../../../docs/design/pnp-tweezer-motion.md) にある。

このディレクトリは GPLv3（[LICENSE](LICENSE)）。Klipper（GPLv3）を元にするため。

## 構成

| パス                                      | 内容                                                                               |
| ----------------------------------------- | ---------------------------------------------------------------------------------- |
| `core/vs_planner.{h,c}`                   | Klipper に依存しない計算。1 軸の速度を目標へ近づけ、区間ごとのステップの時刻を作る |
| `module/velocity_stream.c`                | Klipper とつなぐ部分。コマンド、タイマと task、trsync                              |
| `patches/0001-stepper-internal-api.patch` | Klipper の `stepper.c` の内部関数を公開し、`src/pnp/*.c` をビルドに足す            |
| `klipper.commit`                          | 元にする upstream の Klipper の commit                                             |
| `skr-pico.config`                         | Klipper の `.config`（menuconfig の既定から変える項目だけ）                        |
| `build.sh`                                | Klipper を取得して patch を当て、`.uf2` を作る                                     |
| `tests/test_vs_planner.c`、`Makefile`     | `core/` のホストでのテスト                                                         |

## ビルド

```sh
src/firmware/pnp-motion/build.sh
```

`${XDG_CACHE_HOME:-~/.cache}/pcbasm/pnp-motion/` に、`pnp-motion.uf2` とデータ辞書 `pnp-motion.dict` ができる。
Klipper のソースもそこへ取得する（リポジトリの外に置き、pytest・pyright・ruff の対象に入れない）。

| 環境変数       | 既定                                            | 用途                                                                                  |
| -------------- | ----------------------------------------------- | ------------------------------------------------------------------------------------- |
| `BUILD_DIR`    | `${XDG_CACHE_HOME:-~/.cache}/pcbasm/pnp-motion` | ビルド用のディレクトリ                                                                |
| `KLIPPER_REPO` | upstream の GitHub                              | Klipper の取得元。手元の clone（例: `~/klipper`）を指せばネットワークなしで取得できる |

`arm-none-eabi-gcc`・`g++`・`make`・`git` が要る（Klipper の導入で入る）。
ファームの版（`PNP_MOTION_VERSION`）は、このディレクトリを最後に変えた commit。データ辞書の `config` に入る。

## テスト

```sh
make -C src/firmware/pnp-motion test
```

`core/` をホストの cc と UBSan でビルドし、区間を Klipper の stepper と同じ規則で展開して確かめる。
pytest（`tests/firmware/`）からも流れ、CI に載る。`module/` は実機なしではビルドまでしか確かめられない。

## Klipper を上げるとき

1. `klipper.commit` を新しい commit にする
2. `build.sh` で patch が当たるか確かめる。当たらなければ、新しい commit の上で `stepper.c`・`stepper.h`・`src/Makefile` を直し、
    `git diff` で patch を作り直す
3. データ辞書で、Klipper の既存のコマンドと `velocity_stream_*` が揃っていることを確かめる

## コマンド

単位は、位置がステップ、速度が steps/s、加速度が steps/s²、時刻と期間が MCU の clock tick（`CLOCK_FREQ`）。

| コマンド                          | 引数                                                               | いつ                     |
| --------------------------------- | ------------------------------------------------------------------ | ------------------------ |
| `config_velocity_stream`          | `oid`、X・Y・Z の stepper の oid、`invert_dir`（軸ごとの bit）     | 設定時                   |
| `velocity_stream_set_limits`      | `axis`、`min_pos`、`max_pos`、`max_velocity`、`accel`              | 停止中                   |
| `velocity_stream_set_position`    | `axis`、`pos`                                                      | 停止中（原点出しの後）   |
| `velocity_stream_start`           | `clock`、`period_ticks`、`lead_ticks`、`report_ticks`、`ttl_ticks` | 停止中・`TRIGGERED` の後 |
| `velocity_stream_set_velocity`    | `seq`、`vx`・`vy`・`vz`、`expire_clock`                            | `STREAM` 中              |
| `velocity_stream_move`            | `seq`、`axes`（軸ごとの bit）、`x`・`y`・`z`、`speed`              | `STREAM` 中              |
| `velocity_stream_stop`            |                                                                    | `STREAM` 中              |
| `velocity_stream_stop_on_trigger` | `trsync_oid`                                                       | 開始の前                 |
| `velocity_stream_query`           |                                                                    | いつでも                 |

`velocity_stream_set_velocity` は全軸を速度指令にする（進行中の移動をすべて取り消す）。そのため、ある軸を移動させながら
別の軸を連続でジョグすることはできない（速度指令は期限ごとに送り直すので、そのたびに移動が消える）。
`velocity_stream_move` は `axes` の軸を目標位置へ `speed` で動かし、ちょうど止める（各軸は独立に動き、直線にはならない）。
目標が可動域の外なら可動域の端で止まる。移動には期限がない。ほかの軸の速度指令はそのまま続く。

応答は `velocity_stream_state`（`clock`、`seq`、`state`、`reason`、`moving`、`x`・`y`・`z`、`vx`・`vy`・`vz`）。
`report_ticks` ごとと、`TRIGGERED` になったときと、`velocity_stream_query` に返す。

- `x`・`y`・`z` は報告時刻の stepper の位置、`vx`・`vy`・`vz` は先読み（`lead_ticks`）の分だけ先の計画の速度
- `seq` は最後に受け取った速度指令か移動の連番（実行中の指令の連番ではない）
- `state` は `velocity_stream_state` の列挙（`idle`・`stream`・`stopping`・`triggered`）
- `reason` は `triggered` にした trsync の理由。ホストが `endstop_home`・`trigger_analog` に渡した値のほか、
    ホストの `trsync_trigger` の値や、trsync の期限切れの理由（`trsync_set_timeout` の `expire_reason`）も入る。開始で 0 に戻る
- `moving` は目標位置へ移動中の軸の bit。計画が目標に着くと落ちる。stepper が実際に着くのは先読みの分だけ後なので、
    完了は `moving` が 0 で、かつ位置が目標になったことで判断する

`VELOCITY_STREAM_PROTOCOL`（データ辞書の `config`）は、コマンドや応答の形を変えたら上げる。いまは 2。

## ホストが守る前提

- **順序:** `config_stepper`（3 軸）→ `config_velocity_stream` → `velocity_stream_set_limits`（3 軸）→
    原点出し → `velocity_stream_set_position`（3 軸）→ `trsync_start` と登録 → `velocity_stream_start`
- **trsync で止める:** 同じ trsync に、3 軸すべての `stepper_stop_on_trigger` と `velocity_stream_stop_on_trigger` を登録する。
    `velocity_stream` は stepper を止めない。止めるのは trsync の stepper の側で、`velocity_stream` は `TRIGGERED` になって位置を合わせ直すだけ。
    登録しない軸は、先読みの分だけ動き続け、位置がずれる
- **トリガの元:** ロードセル（`trigger_analog`）と、3 軸のリミットスイッチ（Klipper の `endstop_home`）を、同じ trsync に張る。
    理由（`reason`）を元ごとに変えておけば、`velocity_stream_state` で区別できる。
    `endstop_home` は端ではなくピンの値を見るので、押されたままのスイッチを張ると、開始した直後に止まる
- **スイッチの位置:** リミットスイッチは、通常の可動域（`set_limits`）の少し外で押されるように置く。
    可動域の端まで動いてもスイッチを押さず、押されたら位置を見失ったか可動域の設定が誤っていることになる
- **再開:** `TRIGGERED` のあとの速度指令と移動は無視する。ホストが向きを決め、`velocity_stream_start` で開始し直す。
    リミットスイッチで止まったときは、スイッチが押されたままなので、その軸を原点出しし直す
- **原点出し:** MCU に専用の状態はない。1 軸ずつ、ホストが次の順で行う
    1. その軸の可動域を、スイッチを越えて届く広さに `velocity_stream_set_limits` で一時的に広げ、
        `velocity_stream_set_position` で仮の位置を置く
    2. その軸のスイッチの `endstop_home` を trsync に張って開始し、スイッチへ向かう一定の速さの速度指令を送り続ける
        （止まる位置には検出の遅れの分の行き過ぎが入る。速さを一定にすれば、行き過ぎも一定になる）
    3. `triggered` になったら、`velocity_stream_set_position` でスイッチの位置を設定する（可動域は広げたまま）
    4. その軸のスイッチは張らずに開始し直し、`velocity_stream_move` で通常の可動域の中の退避位置へ動く。
        着いたら `velocity_stream_stop` で `idle` になるのを待つ
    5. `velocity_stream_set_limits` で通常の可動域に戻す。全軸が済んだら、3 軸のスイッチとロードセルを張って開始する。
        `endstop_query_state` でスイッチが離れていることを確かめてから張る
- **期限:** 速度指令の `expire_clock` を過ぎると、目標を 0 にして減速する。期限は受け取った時点から `ttl_ticks` までに切られる。
    期限は先読みの分だけ先の区間と比べるので、ホストが固まってから減速し始めるまでの時間は `ttl_ticks` − `lead_ticks` ほど
- **向き:** `invert_dir` の bit が立った軸は、正の速度で dir ピンを 0 にする（Klippy の `!dir_pin` に当たる）
- **範囲:** 次を外れると shutdown になる
    - `period_ticks` は 0.5 ms 以上
    - `lead_ticks` は `period_ticks` 以上、100 ms 以下、`period_ticks` の 64 倍以下
        （先読みで積む move を、Klipper の move のプール（最大 1024、全 stepper で共有）に収める）
    - `ttl_ticks` は `lead_ticks` + `period_ticks` より長く、1 秒以下
    - `max_velocity` は 500000 steps/s 以下、`accel` は 65536 以上、10⁹ steps/s² 以下
    - 開始時と位置の設定時に、位置が可動域の中にあること
    - 開始の `clock` は、いまから `lead_ticks` 以上先
- **遅れ:** task が遅れて、計画する区間の開始が過ぎていたら `velocity_stream fell behind` で shutdown する
    （過去の時刻のステップを積むと、Klipper が理由の分かりにくい shutdown になるため）
