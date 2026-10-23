# machine

設定（`pcbasm.config`）と HAL（`pcbasm.hal`）を結んで装置を扱う部品を置く。
HAL はこのパッケージを import しない。

## 構成

| モジュール   | 役割                                                                                                            |
| ------------ | --------------------------------------------------------------------------------------------------------------- |
| `journal.py` | G-code ジャーナル `Journal`。実機へ送ったブロックを実効 timeout の見出し付きで `data/webui/journal/` に追記する |

## G-code ジャーナル

- ジョブは `<日付>-<job_id>.gcode`、ジョブの外の操作（REST）は当日の `<日付>-rest.gcode` に書く
- 書くのは `Klipper.send_gcode` で、送る前に書く。届かなかった送信も残る
- ファイルは直近 200 個を残し、最後に書いた時刻の古いものから消す。ジョブの成果物と違い、次のジョブでは消えない
- 書式は `# send timeout=<実効 timeout>` の見出し行に G-code の行が続く。`FakeKlipper.sent` を `format_block` で並べたものと同じになる
- 緊急停止（`Klipper.emergency_stop`）は `send_gcode` を通らず Moonraker の専用 API へ送るので書かない。緊急停止の前にファイル I/O を挟まないためで、`FakeKlipper` が `M112` として記録する点だけが食い違う
- 書けないとき（ディスク満杯など）は log に残して送信を続ける。ジャーナルは送信列を変えない
