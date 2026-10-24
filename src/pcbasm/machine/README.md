# machine

設定（`pcbasm.config`）と HAL（`pcbasm.hal`）を結んで装置を扱う部品を置く。
HAL はこのパッケージを import しない。

## 構成

| モジュール   | 役割                                                                                                            |
| ------------ | --------------------------------------------------------------------------------------------------------------- |
| `devices.py` | 機体 1 台の装置 `Devices`（Klipper とカメラ）と、それを組む `open_devices`。fake への切替もここで行う           |
| `journal.py` | G-code ジャーナル `Journal`。実機へ送ったブロックを実効 timeout の見出し付きで `data/webui/journal/` に追記する |

## 装置（Devices）

- backend は起動時に `Devices` を 1 つ作り、`AppState` が保持する。ジョブは `JobContext.devices` / `JobContext.klipper()` で同じものを使う
- 作っただけでは外部装置へ接続しない。Moonraker へは最初の送信か問い合わせで、カメラへは最初の `frame_hub()` で接続する。Moonraker が遅れて起動しても、作り直さずにそのまま使える
- Klipper の HTTP クライアントは 1 つを使い回す。`klipper(timeout, journal=, endpoint=)` は既定の timeout とジャーナルだけを替えた接続を返す（塗布の本体は None、jog 60 秒、状態 10 秒、駐機 30 秒、Z 取得 5 秒、クリーニングのテスト 90 秒は呼び出し側が渡す）
- REST は `[klipper]` の接続先を呼ぶたびに読み、ジョブは開始時に読んだ接続先を渡す。接続先が変わればクライアントを作り直す（古いクライアントは実行中のジョブが使っていることがあるので閉じない）
- printer.cfg は接続ごとに最初の 1 回だけ読む。ジョブの本体は 1 つの接続を使い回すので、途中で Mainsail から変えても同じ版で走り、次のジョブから新しい可動域が効く
- 緊急停止は使い回すクライアントとは別のクライアントで送り、実行中の長い送信の後ろで待たない。送ったら閉じる
- golden は `Devices(..., klipper=FakeKlipper(), camera=...)` を注入する。`open_klipper` は root conftest が塞いでいる。backend を組み立て経路ごと確かめるテストは、root conftest の `mount_klipper` / `moonraker` / `unreachable_klipper` で `open_klipper` の返り値を差し替える

## G-code ジャーナル

- ジョブは `<日付>-<job_id>.gcode`、ジョブの外の操作（REST）は当日の `<日付>-rest.gcode` に書く
- 書くのは `Klipper.send_gcode` で、送る前に書く。届かなかった送信も残る
- ファイルは直近 200 個を残し、最後に書いた時刻の古いものから消す。ジョブの成果物と違い、次のジョブでは消えない
- 書式は `# send timeout=<実効 timeout>` の見出し行に G-code の行が続く。`FakeKlipper.sent` を `format_block` で並べたものと同じになる
- 緊急停止（`Klipper.emergency_stop`）は `send_gcode` を通らず Moonraker の専用 API へ送るので書かない。緊急停止の前にファイル I/O を挟まないためで、`FakeKlipper` が `M112` として記録する点だけが食い違う
- 書けないとき（ディスク満杯など）は log に残して送信を続ける。ジャーナルは送信列を変えない
