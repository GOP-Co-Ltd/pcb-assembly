# ファイルと環境変数

[ドキュメント一覧](../../README.md) · [設定キー一覧](config.md)

機体の設定、運用データ、ログの置き場所と、起動時に読む環境変数。
パスはリポジトリ（`~/pcb-assembly`）からの相対で書く。`config/` と `data/` は環境変数で場所を変えられる。

## 設定

| パス                                | 中身                                                                                                       | 書く人                           | Git      |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------- | -------------------------------- | -------- |
| `config/machine.toml`               | 機体の設定（[設定キー一覧](config.md)）。コメントは校正の履歴として残す                                    | 設定ページ、校正の反映、即時書込 | 管理外   |
| `config/history/`                   | 書き換える前の `machine.toml` の世代と、移行の直前の `machine-pre-*.toml`                                  | backend                          | 管理外   |
| `config/<名前>.json`                | カメラ校正（`[camera]` の `calibration_file` が指す）                                                      | カメラ校正の反映                 | 管理外   |
| `config/machines.toml`              | frontend の静的な機体登録（[機体の登録](../maintenance.md#%E6%A9%9F%E4%BD%93%E3%81%AE%E7%99%BB%E9%8C%B2)） | 人                               | 管理外   |
| `~/printer_data/config/printer.cfg` | Klipper の設定。`config/printer.cfg` はここへの symlink                                                    | 人、Klipper の `SAVE_CONFIG`     | 管理外   |
| `deploy/templates/<機種>/`          | 新しい機体の `machine.toml` と `printer.cfg` の初期値                                                      | 人                               | 管理する |

## 運用データ

| パス                              | 中身                                                                | Git      |
| --------------------------------- | ------------------------------------------------------------------- | -------- |
| `uploads/`                        | WebUI からアップロードした PCB                                      | 管理外   |
| `data/webui/webui_state.json`     | 選択中の PCB と、ジョブフォームの次回の既定値                       | 管理外   |
| `data/webui/board_settings/`      | 基板ごとの塗布設定。ファイル名は PCB のパスから決まる               | 管理外   |
| `data/webui/machine_check.json`   | 直近の装置診断の結果（立ち上げチェックリストが読む）                | 管理外   |
| `data/webui/<job_id>/`            | ジョブの成果物。次のジョブを始めると消える                          | 管理外   |
| `data/webui/journal/`             | 実機へ送った G-code のジャーナル。古いファイルから消える            | 管理外   |
| `data/paste-volume-datasets/`     | 塗布量校正のデータセット（撮影画像と `metadata.json`）              | 管理外   |
| `data/paste-volume-calibrations/` | 塗布量校正ファイル。機体の間で Git で配る                           | 管理する |
| `data/selfupdate/`                | 自己更新の記録と単一実行のロック。同居機の 2 つのサービスが共有する | 管理外   |

バックアップの zip に入るものは [保守ガイド](../maintenance.md#%E3%83%90%E3%83%83%E3%82%AF%E3%82%A2%E3%83%83%E3%83%97%E3%82%92%E5%8F%96%E3%82%8B) にある。

## 同梱の資産とテストの入力

| パス                                                 | 中身                                                                            |
| ---------------------------------------------------- | ------------------------------------------------------------------------------- |
| `data/paste-test-board/`、`data/orthogonality_test/` | テスト塗布基板と直交性テストの基板                                              |
| `data/sim/config/`                                   | `make dev` が一時ディレクトリへ複製して使う設定                                 |
| `src/pcbasm/hal/sounds/`                             | 通知音の WAV                                                                    |
| `tests/data/`                                        | テストの入力。`tests/data/config/` はテスト用の完全な設定で、実機には接続しない |

## 環境変数

運用で切り替える環境変数は次の 5 つだけ。本番の unit は 1 つも設定しない。

| 変数                | 意味                                                                                                  | 未設定のとき               |
| ------------------- | ----------------------------------------------------------------------------------------------------- | -------------------------- |
| `PCBASM_CONFIG_DIR` | 設定の置き場所。frontend はここの `machines.toml` だけを読む                                          | リポジトリ直下の `config/` |
| `PCBASM_DATA_DIR`   | backend の成果物と状態の置き場所                                                                      | リポジトリ直下の `data/`   |
| `PCBASM_FAKE`       | `1` で Klipper とカメラを fake にし、mDNS と WebUI からの更新を切り、アップロード先を data の下へ移す | 実機                       |
| `PCBASM_API_PORT`   | backend の待ち受けポート                                                                              | 8081                       |
| `PCBASM_UI_PORT`    | frontend の待ち受けポート                                                                             | 8080                       |

ほかはコードの定数である。

- 待ち受けアドレスは常に `0.0.0.0`。特定のアドレスに絞るなら、ファイアウォールか前段のリバースプロキシで行う
- PCB を開ける場所は、リポジトリ直下、`/media`、`/mnt`
- Mainsail へのリンクは、機体のホスト名（`http://<machine_id>.local`）から作る
- 自己更新の `uv sync` の引数（`--locked --inexact`）と、`data/selfupdate/` の場所は、`PCBASM_DATA_DIR` に関係なく固定
