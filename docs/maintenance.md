# 保守ガイド

[ドキュメント一覧](../README.md) · [運用ガイド](operations.md) · [実機受け入れ試験](acceptance.md)

機体の設定と運用データを持ち出し、戻す手順をまとめる。

## バックアップと復元

### バックアップを取る

機体のページの「開発」タブ →「設定履歴とバックアップ」で「バックアップをダウンロード」を押す。
UI サーバーの更新ページ（`/update`）の機体一覧からも取れる。
ssh は要らない。
UI サーバーを更新する前、両機が同じ版のうちに、両機で取っておく。
UI サーバーを先に更新すると、まだ更新していない機体は版がずれて更新専用になり、UI サーバーからはバックアップを取れない。
そのときは機体の backend の `http://<機体>:8081/api/backup` をブラウザで直接開いて取る。

zip の中身は次のとおり。

| zip 内のパス                      | 中身                                                                                                         |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `config/`                         | `machine.toml`、`machines.toml`、設定履歴（`history/`）、カメラ校正、`printer.cfg`（symlink 先の実ファイル） |
| `data/webui/board_settings/`      | 基板ごとの塗布設定                                                                                           |
| `data/webui/webui_state.json`     | 選択中の PCB とジョブの既定値                                                                                |
| `uploads/`                        | アップロードした PCB（基板ごとの塗布設定が参照する原本）                                                     |
| `data/paste-volume-calibrations/` | 塗布量校正                                                                                                   |
| `manifest.json`                   | 機体名、作成時刻、旧パスの有無とファイル一覧                                                                 |

`manifest.json` の `legacy_paths` は、旧パス（`data/board_settings`、`data/webui_state.json`）のファイル一覧を示す。
旧パスが無ければ `null` になる。

### 設定履歴から戻す

`machine.toml` を書き換えるたびに、書き換える前の内容が `config/history/machine-<時刻>.toml` に残る。
残すのは最新の 50 世代で、それより古い世代は消える。
移行の直前に取る `config/history/machine-pre-<移行名>.toml` は消えない。

「設定履歴とバックアップ」の画面は、世代を新しい順に並べ、各世代のあとに入った変更を「キー: 前 → 後」で示す。
「この変更の前に戻す」を押すと、その世代の内容で `machine.toml` を置き換える。
置き換える前の内容も新しい世代として残るので、戻し直せる。
戻すには操作権が要る。ジョブの実行中は戻せない。

画面が開けないときは、ssh で世代のファイルを書き戻し、backend を再起動する。

```sh
cd ~/pcb-assembly
cp config/history/machine-<時刻>.toml config/machine.toml
sudo systemctl restart pcbasm-api
```

### バックアップの zip から戻す

backend が動いていると、置いている間に `webui_state.json` などを書き換えることがある。
backend を止めてから置き、置き終えたら起動する。

1. zip を `unzip` で展開する。`config/history/` を `cp` で置くときは `cp -p` で mtime を保つ（名前付きの世代は mtime の順で並ぶ）
2. `sudo systemctl stop pcbasm-api` で backend を止める
3. 戻したいファイルを同じ場所へ置く
    - `config/`、`data/`、`uploads/` は `~/pcb-assembly/` の下の同じパスへ置く
    - `config/printer.cfg` は実ファイルが入っている。symlink を上書きせず、symlink 先（`~/printer_data/config/printer.cfg`）へ置く
4. `sudo systemctl start pcbasm-api` で backend を起動する
5. `printer.cfg` を戻したときは、トップバーの「ファームウェア再起動」で Klipper に読み直させる

## 手動ロールバック

新しい版が起動しない、または新しい版で困ったときに、前の版へ戻す手順。

1. `ssh <機体>` で入り、`cd ~/pcb-assembly` する
2. `git status --short` で未保存の変更を確かめる。変更があれば、次へ進む前に別の場所へ退避する
3. 戻り先の commit を決める。PR 本文に記録した head か、`git log --oneline -5` で探す（WebUI の更新記録にも残っている）
4. `git switch -c fix/YYYY-MM-DD/recovery <前の head>` で復旧ブランチを作る
5. `uv sync --locked --inexact` を実行する
6. 設定も戻すなら、[設定履歴から戻す](#%E8%A8%AD%E5%AE%9A%E5%B1%A5%E6%AD%B4%E3%81%8B%E3%82%89%E6%88%BB%E3%81%99) の ssh の手順で世代のファイルを書き戻す。設定の移行（PR17）を含む版から、それより前の版へ戻すときは、必ず `config/history/machine-pre-<移行名>.toml` を戻す。旧コードは新しい節を読まず、既定値で黙って動く
7. サービスを再起動する。同居機は `sudo systemctl restart pcbasm-api pcbasm-ui`、機体だけのホストは `pcbasm-api`、frontend 専用機は `pcbasm-ui` を再起動する
8. 起動しなければ `journalctl -u pcbasm-api -n 200` で理由を見る

復旧ブランチは診断と一時運用のためのもの。
WebUI からの更新は復旧ブランチでは使えない。
原因を直した版が `main` に入ったら、ssh で `git switch main` してから更新する。
