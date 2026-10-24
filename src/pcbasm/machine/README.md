# machine

設定（`pcbasm.config`）と HAL（`pcbasm.hal`）を結んで装置を扱う部品を置く。
HAL はこのパッケージを import しない。

## 構成

| モジュール       | 役割                                                                                                                                                                                                    |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `devices.py`     | 機体 1 台の装置 `Devices`（Klipper、カメラ、通知音）と、それを組む `open_devices`。fake への切替もここで行う                                                                                            |
| `journal.py`     | G-code ジャーナル `Journal`。実機へ送ったブロックを実効 timeout の見出し付きで `data/webui/journal/` に追記する                                                                                         |
| `diagnostics.py` | 装置診断（ジョブ `machine_check`）。Klipper の接続と状態、printer.cfg にジョブの部品が要求する section、カメラの校正（焦点 Z の記録と解像度の一致）、ALSA の再生デバイスを確かめる                      |
| `commands.py`    | マシン操作パネルの命令 `MachineCommand`（home / jog / move / relax / focus_z / move_to_cap / gcode）と、その G-code を組む `to_gcode`。REST の `/api/machine/command` と対話ジョブの command が共用する |
| `park.py`        | ノズルキャップへの駐機 `park`（キャップ → PRESENT → M84）。ジョブの終了時とノズルクリーニングのテストが使う                                                                                             |

## 装置（Devices）

- backend は起動時に `Devices` を 1 つ作り、`Backend` が保持する。ジョブは `JobContext.devices` / `JobContext.klipper()` で同じものを使う
- 通知音のプレイヤーも `Devices.audio` が持つ（既定は ALSA）。テストは `open_devices(..., audio=FakeAudioPlayer())` か `Devices(..., audio=...)` で差し替え、実スピーカーを鳴らさない
- 作っただけでは外部装置へ接続しない。Moonraker へは最初の送信か問い合わせで、カメラへは最初の `frame_hub()` で接続する。Moonraker が遅れて起動しても、作り直さずにそのまま使える
- Klipper の HTTP クライアントは 1 つを使い回す。`klipper(timeout, journal=, endpoint=)` は既定の timeout とジャーナルだけを替えた接続を返す（塗布の本体は None、jog 60 秒、状態 10 秒、駐機 30 秒、Z 取得 5 秒、クリーニングのテスト 90 秒は呼び出し側が渡す）
- REST は `[klipper]` の接続先を呼ぶたびに読み、ジョブは開始時に読んだ接続先を渡す。接続先が変わればクライアントを作り直す（古いクライアントは実行中のジョブが使っていることがあるので閉じない）
- printer.cfg は接続ごとに最初の 1 回だけ読む。ジョブの本体は 1 つの接続を使い回すので、途中で Mainsail から変えても同じ版で走り、次のジョブから新しい可動域が効く
- 緊急停止は使い回すクライアントとは別のクライアントで送り、実行中の長い送信の後ろで待たない。送ったら閉じる
- `open_devices(..., fake=True)` は Klipper を実機と同じ printer.cfg（`FAKE_MACHINE_PRINTER_CONFIG`。駐機の PRESENT マクロまである）の `FakeKlipper` に、カメラをその位置から `scene` の図形と塗布痕を描く `SceneCamera` にする（`PCBASM_FAKE=1` と `make dev`）。画素の大きさと解像度は校正ファイルから、ノズルの位置は machine.toml から取る
- golden は `Devices(..., klipper=FakeKlipper(), camera=...)` を注入する。`open_klipper` は root conftest が塞いでいる。backend を組み立て経路ごと確かめるテストは、root conftest の `mount_klipper` / `moonraker` / `unreachable_klipper` で `open_klipper` の返り値を差し替える

## 装置診断

- 読み取りだけで、G-code は送らない。不合格で終わっても駐機しない（golden `machine_check_*.gcode` は空）
- Klipper は、Moonraker に届くことに加えて `webhooks.state` が ready かを見る。shutdown（MCU の切断など）でも printer.cfg は読めるので、printer.cfg の項目は並べる
- printer.cfg は、ステージ（`XYZStage.limits`）とディスペンサー（`PasteDispenser`）を実際に作って確かめる。ジョブが要求するものと食い違わない
- プローブの `[load_cell_probe]` と駐機の `[gcode_macro PRESENT]` は section の有無だけを見る。`ProbeExecutor` は上の層（pasting）にあって作れないので、section 名を写して持つ。PRESENT が無くても駐機は M84 に落ちて動くが、実機にはあるはずなので不合格にする
- カメラは 1 枚撮り、その大きさを校正ファイルの `resolution` と比べる。校正前の機体では、校正ファイルが無いので不合格になる
- ALSA は、`[audio]` の出力デバイスが `aplay -L` の候補にあるかを見る。候補の先頭のシステム既定は常に入るので、それしか無ければ不合格にする

## G-code ジャーナル

- ジョブは `<日付>-<job_id>.gcode`、ジョブの外の操作（REST）は当日の `<日付>-rest.gcode` に書く
- 書くのは `Klipper.send_gcode` で、送る前に書く。届かなかった送信も残る
- ファイルは直近 200 個を残し、最後に書いた時刻の古いものから消す。ジョブの成果物と違い、次のジョブでは消えない
- 書式は `# send timeout=<実効 timeout>` の見出し行に G-code の行が続く。`FakeKlipper.sent` を `format_block` で並べたものと同じになる
- 緊急停止（`Klipper.emergency_stop`）は `send_gcode` を通らず Moonraker の専用 API へ送るので書かない。緊急停止の前にファイル I/O を挟まないためで、`FakeKlipper` が `M112` として記録する点だけが食い違う
- 書けないとき（ディスク満杯など）は log に残して送信を続ける。ジャーナルは送信列を変えない
