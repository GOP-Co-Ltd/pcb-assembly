# 運用ガイド

[ドキュメント一覧](../README.md) · [WebUI の使い方](webui.md)

## systemd サービス

backend と frontend は別 unit（`pcbasm-api.service` / `pcbasm-ui.service`）。
`scripts/web-service.sh` は `sudo` を付けずに実行する（必要な操作はスクリプト内で `sudo` する）。
対象（`api` / `ui` / `all`）を省略すると `api` を操作する。

ホストの構成ごとに install する対象を決める。

| ホストの構成                              | install コマンド                       |
| ----------------------------------------- | -------------------------------------- |
| 同居機（backend と frontend を置く 1 台） | `./scripts/web-service.sh install all` |
| 機体（backend だけ）                      | `./scripts/web-service.sh install api` |
| frontend 専用機（frontend だけ）          | `./scripts/web-service.sh install ui`  |

```bash
./scripts/web-service.sh start ui     # 起動（stop / restart / status も同じ形）
./scripts/web-service.sh remove all   # サービス登録を削除
./scripts/web-service.sh render api   # unit テキストを標準出力へ（systemd に触らない）
```

- `render` は読み取り専用で、`sudo` も systemctl も要らない。設置済み unit との差分確認に使う
- `all` を対象にした `start` / `stop` / `restart` は、未登録の unit を警告して読み飛ばす

旧 `pcbasm-webui.service`（旧 backend）が残ると restart ループに入る。
旧 unit を削除するのは、対象が `api` または `all` の `install` / `remove` だけ。
`install ui` は旧 unit を削除せず警告だけを出す。警告が出たら `install api` を実行する。

起動順の依存は付けていない。frontend が backend より先に起動してよい（未起動の backend を選んだページが 503 になるだけ）。

通知音を使う場合は、backend サービスの実行ユーザーから対象の ALSA PCM を再生できることも確認する。

## 校正結果の保存に失敗したとき

「設定に反映」で校正ファイルを保存できなかった場合、`machine.toml` は変更されず、
結果は再試行できる状態で残る。backend ホストの `journalctl -u pcbasm-api -n 200` で
原因を確認し、設定ディレクトリの書込権限・空き容量・同名ディレクトリの有無を調べる。
原因を取り除いたら、同じ結果の「設定に反映」をもう一度押す。

直近のジョブ結果だけを保持するため、再試行する前に別のジョブを開始しない。
サービスを再起動する必要がある場合は、先に結果欄から校正 JSON をダウンロードしておく。

## PCB 選択・ジョブ既定値の保存に失敗したとき

PCB の選択とジョブフォームの既定値は、backend のデータディレクトリ内の
`webui/webui_state.json` に保存される。保存に失敗した場合、選択と既定値は変更前のまま維持される。
backend のログと保存先の書込権限・空き容量を確認し、原因を取り除いて同じ操作を再試行する。
PCB のアップロードでは、選択状態の保存が失敗してもアップロードファイル自体が残る場合がある。

## ファームウェア再起動

トップバーの「ファームウェア再起動」は Klipper の `FIRMWARE_RESTART` を送り、続けて**そのホストで動いている pcbasm のサービス（backend / frontend）も再起動する**。
操作権が要り、押すと確認ダイアログが出る。

| 状況                                            | 結果                                         |
| ----------------------------------------------- | -------------------------------------------- |
| Klipper へ送れない（Moonraker 不通など）        | 502 を返す。サービスは再起動しない           |
| sudoers 未設置、または WebUI からの更新が実行中 | ファームウェアだけ再起動し、画面に警告を出す |
| 上のどれにも当たらない                          | ファームウェアとサービスの両方を再起動する   |

サービスの再起動には、次節の sudoers 設置が必要（追加の設定は要らない）。

## WebUI からの更新

`main` が進んだときの反映を、ssh せず WebUI のページから実行できる。
実行内容は `git fetch` → `merge --ff-only` → `uv sync --locked --inexact` → サービス再起動の順である。

### セットアップ（1 台につき 1 回）

```bash
./scripts/install-update-sudoers.sh show     # 設置する内容を確認する（特権不要）
./scripts/install-update-sudoers.sh install  # 再起動に必要な NOPASSWD を設置する
./scripts/web-service.sh install <対象>      # unit 定義を最新にする（対象は「systemd サービス」の表）
```

sudoers で許可するのは次の 3 通りの固定 argv だけ。
ワイルドカードを足さない（`sudo` の glob は `/` にも一致するため、`*` を 1 つ入れると任意コマンドを root で実行できてしまう）。
sudoers ファイルは手で編集せず、`install-update-sudoers.sh` で作り直す。

```text
/usr/bin/systemctl restart --no-block pcbasm-api.service
/usr/bin/systemctl restart --no-block pcbasm-ui.service
/usr/bin/systemctl restart --no-block pcbasm-api.service pcbasm-ui.service
```

### 更新する

| ホスト          | 更新ページ                                                                |
| --------------- | ------------------------------------------------------------------------- |
| 機体（backend） | `/m/<machine_id>/dev/update`（開発タブ → ソフトウェア更新）。操作権が要る |
| frontend 専用機 | `/update`                                                                 |

複数の機体をまとめて扱うときは、一括管理タブ（`/bulk`）の各行で「取得」して操作権を取り、
「更新」を押す。更新があれば確認のうえ、その機体で更新と再起動を行う。

各サービスは 30 分ごとに `git fetch` だけを実行する。
更新があると全ページのトップバーに「ソフトウェア更新あり」のバッジが出て、押すと更新ページへ移る。

更新時の動作は次のとおり。

- 未コミットの変更・未 push のローカル commit・分岐・detached HEAD のどれかがあれば、何もせず中断する（作業ツリーは変わらない）
- `uv sync` か起動チェック（import）が失敗したら、サービスを再起動しない
- **自動ロールバックはしない。** 失敗したら下の「復旧」を ssh で行う
- 同居機では backend と frontend を両方再起動するので、画面の接続も一度切れる
- git LFS のファイルは取得しない。必要なら ssh して `git lfs pull` を手で実行する
- 更新後に unit 定義が古いままなら画面に警告が出る。自動では install しないので、`web-service.sh install <対象>` を手で実行する
- `config/` は git 追跡外なので更新で消えない

`uv sync` の引数は `PCBASM_API_UPDATE_UV_SYNC_ARGS` / `PCBASM_UI_UPDATE_UV_SYNC_ARGS`（空白区切り）で変えられる。
この変数は既定の引数を丸ごと置き換えるので、`--locked --inexact` を必ず含めてから引数を足す。
`--locked` が無いと `uv.lock` が書き換わり、作業ツリーが dirty になって以後の更新がすべて中断する。
`--inexact` が無いと、指定しなかった dependency group が消える。

### 更新できない場合

- **`API_VERSION` を上げる PR は、機体の見つけ方で更新方法が変わる。** mDNS 発見は frontend と `API_VERSION` が一致する機体だけを一覧に出すため、`API_VERSION` がずれた機体は一覧から消える
    - mDNS で見つけている機体: WebUI からは更新しない。ssh で更新する
    - 静的登録（`config/machines.toml`）の機体: 一覧から消えないので、WebUI から更新できる
- 更新を無効にするには `PCBASM_API_UPDATE_ENABLED=0` / `PCBASM_UI_UPDATE_ENABLED=0` を設定する（更新ページの操作は 403 になる）

**信頼境界**: この WebUI に認証は無い（[公開範囲](webui.md#%E5%85%AC%E9%96%8B%E7%AF%84%E5%9B%B2%E7%84%A1%E8%AA%8D%E8%A8%BC%E3%81%A7%E3%81%82%E3%82%8B%E3%81%93%E3%81%A8%E3%81%AE%E6%B3%A8%E6%84%8F)）。
WebUI からの更新を有効にすると、`main` に push できる者も装置で動くコードを変えられる。
GitHub 側で保護ブランチと 2FA を必須にする。

### 復旧（新しいリビジョンが起動しない・依存が壊れた）

1. `ssh <機体>` で入り、`cd ~/pcb-assembly` する
2. `git status --short` で未保存の変更を確かめる。変更があれば、次へ進む前に別の場所へ退避する
3. `git log --oneline -5` で更新前の commit を探す（WebUI の更新記録にも残っている）
4. `git switch -c fix/YYYY-MM-DD/recovery <更新前の-sha>` で復旧ブランチを作る
5. `uv sync --locked --inexact` を実行する
6. `sudo systemctl restart pcbasm-api` で再起動する（frontend 専用機は `pcbasm-ui`）
7. 起動しなければ `journalctl -u pcbasm-api -n 200` で理由を見る

復旧ブランチは診断と一時運用のためのもの。
原因を直した `main` へ戻るまでは、WebUI からの更新を使わない。
