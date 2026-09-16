# 運用ガイド

[ドキュメント一覧](../README.md) · [WebUI の使い方](webui.md)

## systemd サービス

backend と frontend は別 unit（`pcbasm-api.service` / `pcbasm-ui.service`）。
対象を省略すると `api` を操作する。

```bash
./scripts/web-service.sh install all  # 同居機（backend + frontend の両方を置く 1 台）
./scripts/web-service.sh install      # 機体（backend のみ。= install api）
./scripts/web-service.sh install ui   # frontend 専用機（UI だけを置くホスト）
./scripts/web-service.sh start ui     # 起動（stop / restart / status も同じ形）
./scripts/web-service.sh remove all   # サービス登録を削除
./scripts/web-service.sh render api   # unit テキストを標準出力へ（systemd に触らない）
```

`render` は設置済み unit との差分確認用の読み取り専用サブコマンド（sudo も systemctl も
要らない）。WebUI の自己更新がこれを読んで「unit 定義が古いので再 install が要る」を
画面に出す。

旧 `pcbasm-webui.service`（= 旧 backend）の掃除は**対象が `api` または `all` のときだけ**
行う。`install api` / `install all` / `remove api` / `remove all` は旧 unit を disable して
削除する（`make webui` エイリアスを廃止したため、残すと `ExecStart` が解決できず restart
ループに入る）。**`install ui` は旧 unit を削除せず、残っていれば「`install api` を実行して
ください」と警告するだけ**（旧 unit は backend 本体なので、`ui` を入れ替えるついでに消すと
同居機の backend が消えて frontend だけが残る）。`remove ui` も旧 unit には触らない。
運用の対応は **同居機 = `install all`、機体 = `install api`、frontend 専用機 = `install ui`**。

通知音を使う場合は、backend サービスの実行ユーザーから対象の ALSA PCM を再生できることも確認する。

起動順の依存は付けていないので、frontend が backend より先に上がって構わない（未起動の
backend を選んだページが 503 になるだけ）。

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

トップバーの「ファームウェア再起動」は Klipper の `FIRMWARE_RESTART` に加えて、
**そのホストで動いている pcbasm のサービス（backend WebAPI / UI frontend）も再起動する**。
装置ごと立て直す復旧操作なので、WebUI 側の状態も一緒に作り直す。

- 再起動対象と argv は WebUI からの更新と同じ（下記の sudoers 設置だけで足りる）
- 押すと確認ダイアログが出る（誤クリックで LAN 中の画面が切れるため）
- Klipper へ送れなかった場合（Moonraker 不通など）は 502 を返し、**サービスには触れない**
- sudoers 未設置・更新の実行中は、ファームウェアだけ再起動して画面に警告を出す
    （更新と同じ flock を取るので、同居機の相方が `uv sync` 中なら再起動しない）

## WebUI からの更新

`main` が進んだときの反映を、ssh せず WebUI のページから実行できる。
やることは **`git pull`（fast-forward のみ）→ `uv sync` → systemd 再起動**。

- 機体（backend）: `/m/<machine_id>/dev/update`（開発タブ → ソフトウェア更新）。操作権が要る
- frontend 専用機: `/update`（マシン非依存のページ）

更新が待っているときは、全ページのトップバーに「ソフトウェア更新あり」のバッジが出る
（押すと該当ホストの更新ページへ飛ぶ）。有無の判定にはリモートの取得が要るので、
各サービスが 30 分ごとに `git fetch` だけを実行して観測値を新しく保つ。

セットアップは 1 台につき 1 回:

```bash
./scripts/install-update-sudoers.sh install  # 再起動に必要な NOPASSWD を設置
./scripts/install-update-sudoers.sh show     # 設置せず内容だけ確認
./scripts/web-service.sh install all         # unit 定義の更新を反映（既存機体も再実行が必要）
```

**許可する特権は再起動だけ**で、引数まで固定した次の 3 通りしか通さない
（ワイルドカードを 1 文字も置かない。`sudo` の glob は `/` も食うため、`*` を 1 つ
入れるだけで「任意のコマンドを root で実行」に悪化する）:

```text
/usr/bin/systemctl restart --no-block pcbasm-api.service
/usr/bin/systemctl restart --no-block pcbasm-ui.service
/usr/bin/systemctl restart --no-block pcbasm-api.service pcbasm-ui.service
```

方針:

- **fast-forward only。** 未コミットの変更・未 push のローカル commit・分岐・detached の
    いずれかがあれば**何もせず中断**する（作業ツリーは 1 バイトも動かない）
- **`uv sync` か起動チェック（import）が失敗したら再起動しない。** 起動できない
    リビジョンで WebUI ごと到達不能になるのを防ぐ
- **自動ロールバックはしない。** 失敗時の復旧は ssh（下記）
- `uv sync` は `--locked --inexact` 固定。`--locked` が無いと `uv.lock` が書き換わって
    tree が dirty になり以後の更新が全部止まり、`--inexact` が無いと指定しなかった
    dependency group が消える。機体ごとに増やすなら
    `PCBASM_API_UPDATE_UV_SYNC_ARGS` / `PCBASM_UI_UPDATE_UV_SYNC_ARGS`（**丸ごと置換**
    なので既定の 2 つを書き直したうえで足す）
- **同居機（backend + frontend が同じホスト）では画面も一度切れる。** 再起動対象は
    そのホストで active な pcbasm unit すべてで、api → ui の順に 1 回で投げる
- **再起動コマンドがシグナルで死んでも失敗にしない。** `--no-block` で投げた restart が
    自分の cgroup を先に止めると `sudo` が SIGTERM で落ちる（終了コード -15）。
    これは要求どおり再起動できた証拠なので、失敗として記録しない
- **git LFS は引かない**（`git lfs pull` は手動）。checkout の smudge フィルタは走るので、
    失敗すれば `merge --ff-only` が非 0 で止まる。smudge のネットワーク待ちを見込んで
    merge のタイムアウトは 600 秒（ここで打ち切ると作業ツリーが中途半端に残る）
- **更新後に unit 定義が古いままなら画面に警告を出す**（`web-service.sh` の出力と
    設置済み unit を非特権で比べるだけ。自動で install はしない）
- `config/` は git 追跡外なので更新で消えない

復旧（新しいリビジョンが起動しない・依存が壊れた）:

```bash
ssh <機体>
cd ~/pcb-assembly
git status --short               # 未保存の変更があれば先に別の場所へ退避する
git log --oneline -5              # 更新前の commit は WebUI の記録にも残っている
git switch -c fix/YYYY-MM-DD/recovery <更新前の-sha>
uv sync --locked --inexact
sudo systemctl restart pcbasm-api  # frontend 専用機は pcbasm-ui
journalctl -u pcbasm-api -n 200    # 起動失敗の理由
```

復旧ブランチは診断と一時運用用。原因を修正した main へ戻るまでは WebUI の自動更新を使わない。

**更新を許可する範囲**: この WebUI に認証は無い（[公開範囲](webui.md#%E5%85%AC%E9%96%8B%E7%AF%84%E5%9B%B2%E7%84%A1%E8%AA%8D%E8%A8%BC%E3%81%A7%E3%81%82%E3%82%8B%E3%81%93%E3%81%A8%E3%81%AE%E6%B3%A8%E6%84%8F)）。LAN に居る者は既に
ステージを動かせるので物理的なリスクは増えないが、**信頼境界が「LAN に居る者」から
「LAN に居る者 ∪ GitHub に push できる者」へ広がる**。GitHub 側の保護ブランチと 2FA を
必須にすること。無効化するには `PCBASM_API_UPDATE_ENABLED=0` /
`PCBASM_UI_UPDATE_ENABLED=0`（ページの操作は 403 になる）。

**`API_VERSION` を上げる PR はこの経路で更新できない。** mDNS 発見が完全一致フィルタ
なので、上げた瞬間に機体が frontend の一覧から消える。静的登録（`config/machines.toml`）の
機体だけが更新でき、他は ssh で対応する。
