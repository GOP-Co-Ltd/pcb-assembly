# 保守ガイド

[ドキュメント一覧](../README.md) · 前: [日常の運用](daily.md) · [実機受け入れ試験](acceptance.md)

ソフトウェアの更新、設定と運用データの持ち出しと復元、困ったときの復旧、ホストの追加、ログの見方をまとめる。
ファイルの置き場所は [ファイルと環境変数](reference/files.md) にある。

## 更新する

`main` が進んだら、ssh せずに WebUI から更新できる。
更新の操作は `/update`（ソフトウェア更新ページ）の 1 ページにまとめてあり、UI サーバー自身と、登録済みの全機体のパネルが並ぶ。
機体一覧の各行の「更新ページ」からも開ける。

- 各サービスは 30 分ごとに `git fetch` だけを行う。更新があると、全ページのトップバーに「ソフトウェア更新あり」のバッジが出る
- 「更新を確認」は操作権なしで押せる（`git fetch` するだけ）
- 「更新して再起動」は、その機体の操作権が空いていれば取って進める。ほかの端末が持っていると断られる（423）。取った操作権は更新後の再起動で消える
- 実行するのは `git fetch` → `merge --ff-only` → `uv sync --locked --inexact` → サービスの再起動の順
- 同居機では backend と frontend を両方再起動するので、画面の接続も一度切れる。更新ページの「UI サーバー」と自機のパネルは同じソースを指すので、どちらか片方を押せばよい

更新の前に、両機が同じ版のうちに [バックアップ](#%E3%83%90%E3%83%83%E3%82%AF%E3%82%A2%E3%83%83%E3%83%97%E3%82%92%E5%8F%96%E3%82%8B) を取っておく。

### 更新が止まるとき

- 未コミットの変更、未 push のローカル commit、分岐、detached HEAD のどれかがあれば、何もせずに止まる（作業ツリーは変わらない）
- `uv sync` か起動前の import の確認が失敗したら、サービスを再起動しない。**自動のロールバックはしない。** [手動ロールバック](#%E6%89%8B%E5%8B%95%E3%83%AD%E3%83%BC%E3%83%AB%E3%83%90%E3%83%83%E3%82%AF) で戻す
- Git LFS のファイルは取得しない。要るなら ssh して `git lfs pull` を実行する
- 更新の後に unit の定義が古いと、画面に警告が出る。自動では設置しないので `./install.sh services <対象>` を手で実行する（[サービスの構成](#%E3%82%B5%E3%83%BC%E3%83%93%E3%82%B9%E3%81%AE%E6%A7%8B%E6%88%90)）
- シミュレーション起動（`make dev`）では更新できない（更新ページの操作は 403 になる）

`config/` は Git 管理外なので、更新で消えない。

### 版のずれた機体（更新専用）

frontend と backend は、契約の版（`API_VERSION`）が一致しているときだけ通常の画面を出す。

- 版がずれた機体は、機体一覧に「（更新専用）」と出る。どのページを開いても、緊急停止と更新ページへの案内だけの画面になる
- 古いほうを `/update` から更新して版をそろえると、通常の画面に戻る。UI サーバーが古いときは、UI サーバーのパネルで更新する
- 更新専用の機体では、操作権を取ることも奪取することもできない。ほかの端末が操作権を持っていると更新は断られるので、その端末から更新する。その端末が無ければ ssh で更新する
- 静的登録（`config/machines.toml`）だけで見えている機体は版がわからないので、更新専用にならない。版がずれて画面が 503 になったら ssh で更新する

**WebUI からの更新を有効にすると、`main` に push できる人は装置で動くコードを変えられる。** GitHub で保護ブランチと 2FA を必須にする。

## バックアップと復元

### バックアップを取る

機体のページの「機体」タブ →「設定履歴とバックアップ」で「バックアップをダウンロード」を押す。
ソフトウェア更新ページ（`/update`）の各機体の欄からも取れる。ssh は要らない。

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
古い世代は数が増えると消える。設定の移行の直前に取る `config/history/machine-pre-<移行名>.toml` は消えない。

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
5. `printer.cfg` を戻したときは、トップバーの「再起動（復旧）」で Klipper に読み直させる

## 手動ロールバック

新しい版が起動しない、または新しい版で困ったときに、前の版へ戻す手順。

1. `ssh <機体>` で入り、`cd ~/pcb-assembly` する
2. `git status --short` で未保存の変更を確かめる。変更があれば、次へ進む前に別の場所へ退避する
3. 戻り先の commit を決める。PR 本文に記録した head か、`git log --oneline -5` で探す（WebUI の更新記録にも残っている）
4. `git switch -c fix/YYYY-MM-DD/recovery <前の head>` で復旧ブランチを作る
5. `uv sync --locked --inexact` を実行する
6. 設定も戻すなら、[設定履歴から戻す](#%E8%A8%AD%E5%AE%9A%E5%B1%A5%E6%AD%B4%E3%81%8B%E3%82%89%E6%88%BB%E3%81%99) の ssh の手順で世代のファイルを書き戻す。設定の移行を含む版から、それより前の版へ戻すときは、必ず `config/history/machine-pre-<移行名>.toml` を戻す。古いコードは新しい節を読まず、既定値で黙って動く
7. サービスを再起動する。同居機は `sudo systemctl restart pcbasm-api pcbasm-ui`、機体だけのホストは `pcbasm-api`、frontend 専用機は `pcbasm-ui` を再起動する
8. 起動しなければ [ログ](#%E3%83%AD%E3%82%B0%E3%82%92%E8%A6%8B%E3%82%8B) で理由を見る

復旧ブランチは診断と一時運用のためのもの。
WebUI からの更新は復旧ブランチでは使えない。
原因を直した版が `main` に入ったら、ssh で `git switch main` してから更新する。

## 復旧

### 再起動（復旧）

トップバーの「再起動（復旧）」は、緊急停止や Klipper の異常から戻すための操作である。
Klipper へ `FIRMWARE_RESTART` を送り、続けて**そのホストで動いている pcbasm のサービス（backend / frontend）も再起動する**。
実行中のジョブがあれば中止してから送る。操作権が要り、押すと確認のダイアログが出る。

| 状況                                            | 結果                                         |
| ----------------------------------------------- | -------------------------------------------- |
| Klipper へ送れない（Moonraker 不通など）        | 502 を返す。サービスは再起動しない           |
| sudoers 未設置、または WebUI からの更新が実行中 | ファームウェアだけ再起動し、画面に警告を出す |
| 上のどれにも当たらない                          | ファームウェアとサービスの両方を再起動する   |

再起動の後は、ホーミングから操作を再開する。

### 設定エラーから戻す

`machine.toml` が読めない（書式の誤り）か、値が範囲外のときも、backend は起動して機体一覧に載る。
全ページに設定エラーの理由が出て、ジョブの開始は断られる。緊急停止は効く。

- 値の誤りは、設定ページでその項目を直す
- 書式の誤りは、「設定履歴とバックアップ」で誤る前の世代へ戻す（[設定履歴から戻す](#%E8%A8%AD%E5%AE%9A%E5%B1%A5%E6%AD%B4%E3%81%8B%E3%82%89%E6%88%BB%E3%81%99)）
- 直すと、再起動しなくても理由の表示が消える

### 電源の入れ直し

Raspberry Pi の電源を入れ直しても、操作なしで使える状態へ戻る。
Moonraker の起動が backend より遅れても、backend は最初に使うときに接続する。

## ホストの追加

### サービスの構成

backend と frontend は別の unit（`pcbasm-api.service` / `pcbasm-ui.service`）で動く。
`install.sh` の services 段が unit と更新用の sudoers を設置して起動する。`sudo` を付けずに実行する（必要な操作は段の中で `sudo` する）。

| ホストの構成                              | コマンド                    |
| ----------------------------------------- | --------------------------- |
| 同居機（backend と frontend を置く 1 台） | `./install.sh services`     |
| 機体（backend だけ）                      | `./install.sh services api` |
| frontend 専用機（frontend だけ）          | `./install.sh services ui`  |

```sh
./install.sh render api                 # unit のテキストを標準出力へ（systemd に触らない）
./install.sh render sudoers             # 設置する sudoers の内容を確かめる
sudo systemctl restart pcbasm-ui        # 起動・停止・状態は systemctl で行う（stop / status も同じ形）
```

- 再実行しても、unit が変わっていなければ再起動しない（変わっていれば置き換えて再起動する）
- `render` は読み取り専用で、`sudo` も systemctl も要らない。設置済みの unit との差分の確認に使う
- 起動順の依存は付けていない。frontend が backend より先に起動してよい（まだ起動していない backend を選んだページが 503 になるだけ）
- 通知音を使うなら、backend の実行ユーザーから ALSA の出力デバイスを鳴らせることも確かめる

sudoers で許すのは、次の 3 通りの固定の argv だけ。
ワイルドカードを足さない（`sudo` の glob は `/` にも一致するので、`*` を 1 つ入れると任意のコマンドを root で実行できてしまう）。
sudoers は手で編集せず、`./install.sh services` で作り直す。

```text
/usr/bin/systemctl restart --no-block pcbasm-api.service
/usr/bin/systemctl restart --no-block pcbasm-ui.service
/usr/bin/systemctl restart --no-block pcbasm-api.service pcbasm-ui.service
```

### frontend だけのホスト

機体でない Raspberry Pi などに frontend だけを置くときは、[導入](install.md) の手順 2 で `./install.sh --ui-only` を使う（preflight / packages / python と、services の ui だけを行う）。
同居機の frontend を止めてこのホストにまとめるなら、機体側で `sudo systemctl disable --now pcbasm-ui` を実行する。

### 機体の登録

frontend が機体（backend）を知る経路は 2 つある。

1. mDNS の探索: backend が `_pcbasm._tcp` を広告し、frontend が LAN を探す。設定は要らない
2. 静的登録: `config/machines.toml`

AP のマルチキャスト抑制などで探索できない網では、frontend は WARNING を出して静的登録だけで動き続ける。そのときは `config/machines.toml` を書く。

```toml
[[machine]]
machine_id = "kurousagi"  # 必須。URL の /m/{machine_id} になる
host = "kurousagi.local"  # 必須。ホスト名か IP
port = 8081               # 省略すると 8081
```

- **`machine_id` は、その機体のホスト名（`hostname` の出力）に合わせる。** backend が名乗る ID はホスト名で決まる。ずれると、探索で見つかった機体と静的登録した同じ機体が別の機体として一覧に 2 件出る
- 同じ `machine_id` を両方の経路が知っているときは、静的登録の `host` / `port` を使う
- frontend が `machines.toml` を読むのは起動時の 1 回だけ。編集したら frontend を再起動する
- ファイルが無ければ、静的登録 0 台で起動する。`config/` を退避して作り直したときは `machines.toml` も退避先から戻す
- mDNS には生存の判定が無いので、電源を切った機体はしばらく一覧に残る。届かない機体を選ぶと 503 になる

## ログを見る

| 見たいもの                  | 場所                                                                                            |
| --------------------------- | ----------------------------------------------------------------------------------------------- |
| backend / frontend のログ   | `journalctl -u pcbasm-api -n 200`、`journalctl -u pcbasm-ui -n 200`                             |
| ジョブのログと成果物        | ジョブの画面。ファイルは `data/webui/` の下にあり、次のジョブを始めると消える                   |
| 実機へ送った G-code         | `data/webui/journal/`。ジョブは `<日付>-<job_id>.gcode`、ジョブの外の操作は `<日付>-rest.gcode` |
| 自己更新の記録              | 更新ページ。ファイルは `data/selfupdate/`                                                       |
| Klipper と Moonraker のログ | `~/printer_data/logs/`                                                                          |

G-code のジャーナルは送る前に書くので、届かなかった送信も残る。次のジョブでは消えず、古いファイルから順に消える。
不具合を報告するときは、[開発の手引き](develop/index.md#%E4%B8%8D%E5%85%B7%E5%90%88%E3%82%92%E5%A0%B1%E5%91%8A%E3%81%99%E3%82%8B) の項目を添える。
