# pnp-motion ファーム

PnP 機の XYZ ステージ（BTT SKR Pico）を、ホストから速度指令で動かすファーム。
Klipper の MCU ファームに、モジュール `velocity_stream` を足したもの。
設計と理由は [ピンセット式 PnP の運動制御](../../../docs/design/pnp-tweezer-motion.md) にある。

このディレクトリは GPLv3（[LICENSE](LICENSE)）。Klipper（GPLv3）を元にするため。

## 構成

| パス                                      | 内容                                                                                   |
| ----------------------------------------- | -------------------------------------------------------------------------------------- |
| `core/vs_planner.{h,c}`                   | Klipper に依存しない計算。1 軸の速度を目標へ近づけ、区間ごとのステップの時刻を作る     |
| `module/velocity_stream.c`                | Klipper とつなぐ部分。コマンド、タイマと task、trsync                                  |
| `patches/0001-stepper-internal-api.patch` | Klipper の `stepper.c` の内部関数を公開し、`src/pnp/*.c` をビルドに足す                |
| `klipper/`                                | upstream の Klipper（git submodule）。元にする commit を固定する。書き換えない         |
| `skr-pico.config`                         | Klipper の `.config`（menuconfig の既定から変える項目だけ）                            |
| `build.sh`                                | `klipper/` を `build/` へ書き出して patch を当て、`.uf2` を作る                        |
| `build/`                                  | ビルドの出力（Git で追跡しない）                                                       |
| `tests/test_vs_planner.c`、`Makefile`     | `core/` のホストでのテスト                                                             |
| `tools/bench.py`                          | 実機で試す対話のツール（[実機で試す](#%E5%AE%9F%E6%A9%9F%E3%81%A7%E8%A9%A6%E3%81%99)） |

## ビルド

```sh
src/firmware/pnp-motion/build.sh
```

`build/` に、`pnp-motion.uf2` とデータ辞書 `pnp-motion.dict` ができる。

- Klipper は submodule の `klipper/` にある。`build.sh` はビルドのたびに `git submodule update --init` で、
    親のリポジトリが記録した commit に揃える（未取得なら取得する）
- `.gitmodules` の `ignore = all` で、submodule の版の違いは `git status` に出ない（機体の自己更新を止めないため）
- `build.sh` は、親のリポジトリが記録した commit の Klipper を `build/klipper/` へ書き出し、そこに patch を当ててビルドする。
    submodule は書き換えないので、いつも upstream と同じ状態に保たれる
- `klipper/` と `build/` は、pyright・ruff・pytest の対象から設定で外している（`pyproject.toml`）

`arm-none-eabi-gcc`・`g++`・`make`・`git`・`patch`・`tar` が要る。
submodule を取得した git worktree は `git worktree remove` で消せない（Git の制約）。消すときは `--force` を付ける。
ファームの版（`PNP_MOTION_VERSION`）は、ファームの中身を決めるもの（`core/`・`module/`・`patches/`・`klipper`・`skr-pico.config`・`build.sh`）を最後に変えた commit。
データ辞書の `config` に入る。

## テスト

```sh
make -C src/firmware/pnp-motion test
```

`core/` をホストの cc と UBSan でビルドし、区間を Klipper の stepper と同じ規則で展開して確かめる。
pytest（`tests/firmware/`）からも流れ、CI に載る。`module/` は実機なしではビルドまでしか確かめられない。

## Klipper を上げるとき

1. submodule を新しい commit にする。`git -C src/firmware/pnp-motion/klipper fetch` と `checkout <commit>` のあと、
    親で `git add src/firmware/pnp-motion/klipper` する（`ignore = all` なので `git status` には出ない。`git diff --cached` で確かめる）
2. `build.sh` で patch が当たるか確かめる（文脈がずれると失敗する）。当たらなければ、submodule の作業ツリーで
    `stepper.c`・`stepper.h`・`src/Makefile` を直し、`git -C src/firmware/pnp-motion/klipper diff` で patch を作り直して、
    `git -C src/firmware/pnp-motion/klipper checkout .` で submodule を戻す
3. データ辞書で、Klipper の既存のコマンドと `velocity_stream_*` が揃っていることを確かめる

## 書き込み

機体へは `install.sh` で書き込む。部品実装機（雛形の `machine.toml` に `[pnp]`）の機種を指定すると、
Klipper の firmware の代わりに pnp-motion をビルドして書き込む（[導入](../../../docs/install.md)）。

```sh
./install.sh hardware usaremino_pnp
```

- カメラとスピーカーの選択と boot 設定の更新にも答える（いまの機体と同じものを選ぶ）。終わると再起動を求められる
- 手で書き込むなら、`build.sh` でビルドし、電源を切って BOOT のジャンパ pin を挿し、USB をつないで RESET を押して起動する。
    現れた `RPI-RP2` ボリュームへ `build/pnp-motion.uf2` をコピーし、ジャンパ pin を抜いて RESET を押す
- pnp-motion は Klipper の firmware に、同じ `.config` でモジュールを足しただけのもの。
    Klippy と固定した Klipper の版が同じなら、書き込んだあとも Klippy（`printer.cfg`）はそのまま動く

## 実機で試す

Pi 側のクライアントができるまでは、`tools/bench.py` で MCU に直接つないで試す。
Klippy は動かさず、submodule の Klipper（ファームと同じ commit）の klippy の部品（シリアルの通信、時刻の同期、TMC のレジスタの定義）を使う。
ピンと値は PnP 機（`deploy/templates/usaremino/printer.cfg`）と同じものをファイルの先頭に持つ。違う機体で使うときはそこを直す。

PnP 機の Pi で行う。bench と Klippy は同じ MCU を同時に使えないので、bench を使う間は Klipper を止める
（その間は WebUI の Klipper の操作も使えない）。

1. pnp-motion を書き込む（[書き込み](#%E6%9B%B8%E3%81%8D%E8%BE%BC%E3%81%BF)）
2. `sudo systemctl stop klipper` で Klipper を止める（MCU のシリアルを空ける）
3. モーターの電源（24 V）を入れる。TMC2209 は電源がないと UART に応答しない
4. `~/klippy-env/bin/python src/firmware/pnp-motion/tools/bench.py` で起動する。
    MCU を設定し、TMC2209 に Klipper と同じレジスタ（電流 0.54 A、64 microsteps、stealthChop）を書いて確かめる

| コマンド         | 動き                                                                                                   |
| ---------------- | ------------------------------------------------------------------------------------------------------ |
| `nudge AXIS MM`  | 軸を MM（10 mm まで）だけ 2 mm/s で動かす。時間で測る目安。向きの確認に使う                            |
| `home [AXIS...]` | 原点出し（既定は z → x → y）。可動域を広げてスイッチへ向かい、広げたまま 1 mm 退避してから可動域を戻す |
| `v VX VY VZ`     | 速度（mm/s）で動かし続ける。原点出しの前の軸は 0 だけ                                                  |
| `s`              | 減速して止める。`home` などの実行中にも受け付ける                                                      |
| `q`              | 状態、位置、速度、押されているスイッチを表示する                                                       |
| `on`・`off`      | モーターの励磁を入れる・切る（`off` は原点を失う）                                                     |
| `quit`           | 止めて励磁を切り、終わる                                                                               |

流している間は、押されていない 3 軸のリミットスイッチを trsync に張る（`home` の退避中は、その軸のスイッチを除く）。どれかが押されると全軸が止まり、`[trsync]` に軸が出る。
速度指令は 50 ms ごとに送り直し、期限（`ttl`）は 200 ms。

確かめること:

- [ ] 起動で、3 軸の TMC2209 の設定を確かめられる
- [ ] `nudge` で、各軸が正の向きに指定の距離ほど動く（Z の正はスイッチの側）。
    原点出しの前は可動域が分からず、スイッチのない端（X・Y の正、Z の負）では止まらないので、可動域の中ほどで 1 mm ほどから試す
- [ ] `home` で 3 軸の原点が出て、繰り返しても位置がそろう
- [ ] `v` で滑らかに動き、`s` で減速して止まる。可動域の端（X・Y は 0.5 mm、Z は −0.5 mm）の手前で減速して止まる
- [ ] 動いている最中にリミットスイッチを手で押すと、即座に全軸が止まる。そのあと `v` で離れる向きに動かせる
- [ ] 動いている最中に bench を Ctrl-C で落とすと、期限が切れて減速して止まる（ホストが止まったときのウォッチドッグ）。
    Ctrl-C では止める命令を送らず、シリアルを切るだけにしている

終わったら `sudo systemctl start klipper` で Klipper を起動する。Klipper の firmware に書き戻す必要はない（[書き込み](#%E6%9B%B8%E3%81%8D%E8%BE%BC%E3%81%BF)）。

- [ ] Klipper を起動し直すと、WebUI から従来どおり原点出しと移動ができる

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
