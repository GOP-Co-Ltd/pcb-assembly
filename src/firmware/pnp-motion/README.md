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

| コマンド                          | 引数                                                               | いつ                   |
| --------------------------------- | ------------------------------------------------------------------ | ---------------------- |
| `config_velocity_stream`          | `oid`、X・Y・Z の stepper の oid、`invert_dir`（軸ごとの bit）     | 設定時                 |
| `velocity_stream_set_limits`      | `axis`、`min_pos`、`max_pos`、`max_velocity`、`accel`              | 停止中                 |
| `velocity_stream_set_position`    | `axis`、`pos`                                                      | 停止中（原点出しの後） |
| `velocity_stream_start`           | `clock`、`period_ticks`、`lead_ticks`、`report_ticks`、`ttl_ticks` | 停止中・`CONTACT` の後 |
| `velocity_stream_set_velocity`    | `seq`、`vx`・`vy`・`vz`、`expire_clock`                            | `STREAM` 中            |
| `velocity_stream_stop`            |                                                                    | `STREAM` 中            |
| `velocity_stream_stop_on_trigger` | `trsync_oid`                                                       | 開始の前               |
| `velocity_stream_query`           |                                                                    | いつでも               |

応答は `velocity_stream_state`（`clock`、`seq`、`state`、`x`・`y`・`z`、`vx`・`vy`・`vz`）。
`report_ticks` ごとと、`CONTACT` になったときと、`velocity_stream_query` に返す。

- `x`・`y`・`z` は報告時刻の stepper の位置、`vx`・`vy`・`vz` は先読み（`lead_ticks`）の分だけ先の計画の速度
- `seq` は最後に受け取った速度指令の連番（実行中の指令の連番ではない）
- `state` は `velocity_stream_state` の列挙（`idle`・`stream`・`stopping`・`contact`）

## ホストが守る前提

- **順序:** `config_stepper`（3 軸）→ `config_velocity_stream` → `velocity_stream_set_limits`（3 軸）→
    原点出し → `velocity_stream_set_position`（3 軸）→ `trsync_start` と登録 → `velocity_stream_start`
- **接触で止める:** 同じ trsync に、3 軸すべての `stepper_stop_on_trigger` と `velocity_stream_stop_on_trigger` を登録する。
    `velocity_stream` は stepper を止めない。止めるのは trsync の stepper の側で、`velocity_stream` は `CONTACT` になって位置を合わせ直すだけ。
    登録しない軸は、先読みの分だけ動き続け、位置がずれる
- **再開:** `CONTACT` のあとの速度指令は無視する。ホストが離れる向きを決め、`velocity_stream_start` で開始し直す
- **期限:** 速度指令の `expire_clock` を過ぎると、目標を 0 にして減速する。期限は受け取った時点から `ttl_ticks` までに切られる。
    期限は先読みの分だけ先の区間と比べるので、ホストが固まってから減速し始めるまでの時間は `ttl_ticks` − `lead_ticks` ほど
- **向き:** `invert_dir` の bit が立った軸は、正の速度で dir ピンを 0 にする（Klippy の `!dir_pin` に当たる）
- **範囲:** 次を外れると shutdown になる
    - `period_ticks` は 0.5 ms 以上
    - `lead_ticks` は `period_ticks` 以上、100 ms 以下、`period_ticks` の 64 倍以下
        （先読みで積む move を、Klipper の move のプール（最大 1024、全 stepper で共有）に収める）
    - `ttl_ticks` は `lead_ticks` + `period_ticks` より長く、1 秒以下
    - `max_velocity` は 500000 steps/s 以下、`accel` は 10⁹ steps/s² 以下
    - 開始時と位置の設定時に、位置が可動域の中にあること
    - 開始の `clock` は、いまから `lead_ticks` 以上先
- **遅れ:** task が遅れて、計画する区間の開始が過ぎていたら `velocity_stream fell behind` で shutdown する
    （過去の時刻のステップを積むと、Klipper が理由の分かりにくい shutdown になるため）
