# Pcb Assembly

## セットアップ

### ハードウェア

- Raspberry Pi 5
- Pick and PlaceまたはPaste Dispenser Machine
- スピーカー（通知音を使う場合。I2S DAC / アンプまたは HDMI 音声出力）

### ソフトウェア

- RPi OS 64bit
- [Klipper](https://www.klipper3d.org/)
    - [KIAUH](https://github.com/dw-0/kiauh) 経由でインストール
        - Klipper + Moonraker + Mailsail
- [KiCAD](https://www.kicad.org/download/linux/)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- `alsa-utils`（通知音を再生する `aplay` を含む）

OS以外のソフトウェア類は[`scripts/install-softwares.sh`](scripts/install-softwares.sh)を実行

Raspberry Pi のカメラ・スピーカーと Klipper MCU firmware は、ソフトウェアの
インストール後に対話式スクリプトで設定する。

```sh
./scripts/setup-hardware.sh
```

- カメラは CAMERA port 0 / 1 と driver（`ov9281` または手入力）を選ぶ
- スピーカーは `max98357a`、overlay 名の手入力、未設定から選ぶ
- BTT SKR Pico v1.0 は画面の案内に従って BOOT jumper と RESET を操作し、
    `/media/$USER/RPI-RP2` volume へ同梱 UF2 firmware をコピーする
- 最後に案内される `sudo reboot` で boot 設定を反映する

boot 設定は `/boot/firmware/config.txt`（旧 OS では `/boot/config.txt`）へ反映され、
変更前の内容は同じ場所の `config.txt.pcbasm.bak` に保存される。スクリプトが管理する
marker 内だけを再実行時に置換し、それ以外の既存設定は保持する。

### GitLab CI Runner

専用のRaspberry Pi 5をGitLab Runnerとして構築する場合は、
[`gitlab-runner/README.md`](gitlab-runner/README.md)を参照する。

### 開発

上記のソフトウェアをインストールしたうえで、次を実行

```sh
make setup
```

- VSCodeでリモートアクセスし、開発することを推奨する。

#### Codex の完了通知

このリポジトリで Codex CLI を起動すると、処理完了時に Windows 側へ通知する。
`.codex/config.toml` は、terminal が非フォーカスのときだけ
`agent-turn-complete` 通知を送る設定である。通知方法は自動選択され、VSCode の
integrated terminal では OSC 9 のポップアップ通知を優先し、未対応の場合は
terminal bell にフォールバックする。

設定は Codex の起動時に読み込まれるため、追加・変更後は Codex を再起動する。
通知が表示されない場合は、Windows の「設定 > システム > 通知」で VSCode の通知を
許可し、集中モードが通知を抑止していないことを確認する。

### マシン設定の配置

装置を動かす前に、テンプレートから `config/` を作る。

```sh
./scripts/setup-machine-config.sh  # data/config-templates/ から選んで config/ を作る
sudo systemctl restart klipper  # printer.cfg の反映
```

`config/` は git 管理外（実測値の書き換えでリポジトリが dirty にならないようにするため）。
テンプレートの規約と printer.cfg の運用は
[`data/config-templates/README.md`](data/config-templates/README.md) を参照。

## WebUI

装置をブラウザから操作するUI。**backend WebAPI（`src/web/api/`）と UI frontend
（`src/web/ui/`）の 2 プロセス**に分かれる。

- **backend WebAPI** — 機体ごとに 1 つ。port 8081。カメラ・Klipper・ジョブ実行・
    マシン設定（`config/`）・PCB ファイルの所有者
- **UI frontend** — LAN に 1 つ。port 8080。ページを描き、`/m/{machine_id}/api/**` を
    各 backend へ中継する。装置の状態を持たず `config/` も読まないので、機体でない
    ホストでも動く

同居機（frontend と backend が同じ Raspberry Pi）では 8080 = frontend / 8081 = backend
に分ける。既存ブックマークの `:8080` はそのまま frontend に着地する。

```sh
make api      # backend WebAPI 起動
make api-dev  # 開発用（auto-reload）
make api-fake # fake camera + 隔離 data_dir（port 8099）

make ui       # UI frontend 起動
make ui-dev   # 開発用（auto-reload）
make ui-fake  # api-fake（8099）を上流にした frontend（port 8098）
```

### マシンの登録

frontend が backend を知る経路は 2 つある。

1. **mDNS 探索** — backend が `_pcbasm._tcp` を広告し、frontend が LAN を探索する。設定不要
2. **静的登録** — `config/machines.toml`

AP のマルチキャスト抑制などで探索できない環境では、frontend は WARNING を出して
静的登録だけで続行する（起動は失敗しない）。**探索に頼れないネットワークでは
`config/machines.toml` を書く。**

```toml
[[machine]]
machine_id = "kurousagi"  # 必須。URL の /m/{machine_id} になる
host = "kurousagi.local"  # 必須。ホスト名または IP
port = 8081               # 省略時 8081
name = "黒兎 1 号機"      # 省略可。画面の表示名
machine_type = "paste"    # 省略可
```

**`machine_id` はその機体のホスト名（`hostname` の出力）に合わせる。** backend が名乗る ID は
`socket.gethostname()` で決まり環境変数では変えられないので、ここがずれると探索が見つけた
同じ backend が別マシン扱いになり一覧に 2 件出る（重複排除は `machine_id` だけで行う）。

ファイルが無い場合は静的登録 0 台として起動する（探索で見つかった分だけが一覧に出る）。
同じ `machine_id` を両方の経路が知っている場合は静的登録の `host` / `port` を優先し、
静的側が持たない `name` / `machine_type` だけ探索側で埋める。

`machines.toml` を読むのは frontend の起動時の 1 回だけなので、**編集したら frontend を
再起動する**（実行中に更新されるのは mDNS 探索の分だけ）。`scripts/setup-machine-config.sh` が案内する
`mv config config.bak.<ts>` で `config/` を作り直すと `machines.toml` も一緒に退避され、
不在はエラーにならず静的登録 0 台になる（退避先から戻す）。

mDNS には生存判定が無く、電源を切った機体は最大 75 分ほど一覧に残る。到達できない
マシンを選ぶと 503 ページになる。

### PCB ファイルは backend 機に置く

ファイルブラウザとアップロードが見るのは **backend プロセスのローカル FS**
（frontend は中継するだけで、frontend 機のファイルは見えない）。**USB メモリは
その機体の Raspberry Pi に挿す。** 閲覧を許すのはリポジトリ直下・`/media`・`/mnt`
（`src/web/api/settings.py` の `pcb_browse_allowed`）。

### 通知音

通知対象ジョブ（はんだ塗布など）の成功・失敗時に、**backend 機**の Raspberry Pi に接続した
スピーカーから通知音を再生する（音を鳴らすのは backend プロセス。ブラウザからは鳴らさず、
完了通知は画面表示のみ）。

開発タブの `/dev/audio` で出力デバイス・音量を選び、テスト再生で確認できる。設定は
`config/machine.toml` の `[audio]` に保存される（未設定時は ALSA のシステム既定デバイス・音量 75%）。
テスト再生は機体のスピーカーが実際に鳴るので操作権を要する。

音声ファイルを差し替える場合は `src/pcbasm/hal/sounds/success.wav` と `failure.wav` を
**非圧縮 16-bit PCM WAV** で同名のまま上書きする（git-lfs 追跡下）。差し替え後は `/dev/audio` の
テスト再生で確認する。

### systemd サービス

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

### WebUI からの更新

`main` が進んだときの反映を、ssh せず WebUI のページから実行できる。
やることは **`git pull`（fast-forward のみ）→ `uv sync` → systemd 再起動**。

- 機体（backend）: `/m/<machine_id>/dev/update`（開発タブ → ソフトウェア更新）。操作権が要る
- frontend 専用機: `/update`（マシン非依存のページ）

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
    dependency group（Pi の `ml-runtime` = torch）が消える。機体ごとに増やすなら
    `PCBASM_API_UPDATE_UV_SYNC_ARGS` / `PCBASM_UI_UPDATE_UV_SYNC_ARGS`（**丸ごと置換**
    なので既定の 2 つを書き直したうえで足す）
- **同居機（backend + frontend が同じホスト）では画面も一度切れる。** 再起動対象は
    そのホストで active な pcbasm unit すべてで、api → ui の順に 1 回で投げる
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
git log --oneline -5              # 更新前の commit は WebUI の記録にも残っている
git reset --hard <更新前の sha>
uv sync --locked --inexact
sudo systemctl restart pcbasm-api  # frontend 専用機は pcbasm-ui
journalctl -u pcbasm-api -n 200    # 起動失敗の理由
```

**攻撃面（率直に）**: この WebUI に認証は無い（下記「公開範囲」）。LAN に居る者は既に
ステージを動かせるので物理的なリスクは増えないが、**信頼境界が「LAN に居る者」から
「LAN に居る者 ∪ GitLab に push できる者」へ広がる**。GitLab 側の保護ブランチと 2FA を
必須にすること。無効化するには `PCBASM_API_UPDATE_ENABLED=0` /
`PCBASM_UI_UPDATE_ENABLED=0`（ページの操作は 403 になる）。

**`API_VERSION` を上げる MR はこの経路で更新できない。** mDNS 発見が完全一致フィルタ
なので、上げた瞬間に機体が frontend の一覧から消える。静的登録（`config/machines.toml`）の
機体だけが更新でき、他は ssh で対応する。

### 複数人で同時に開いたとき（操作権）

変更操作は「操作権」を持つ 1 セッションだけに許す。閲覧は誰でも自由。

- **緊急停止とジョブ中止（abort）は操作権に関係なく常に誰でも実行できる。** 安全機能
    なのでゲートしない
- 空いていれば取得、他の人が保持していれば**奪取**できる（詰み防止）。保持者の
    WebSocket が切れて 30 秒、または無操作 10 分（ジョブ実行中は除く）で自動解放
- **これは認証ではなく自己申告**。セッション ID は frontend が発行する cookie で、
    LAN 上の誰でも他人の ID と表示名を騙れる。防ぐのは「複数人が同時に指示を出す事故」
    であって、権限分離ではない
- **1 ブラウザプロファイル = 1 人**。cookie 単位なので、共有キオスク端末の同じ
    ブラウザで開いた 2 人は同一セッション扱いになり、分けられない

### 公開範囲（無認証であることの注意）

**WebUI に認証は無い。** 待ち受けは既定で `0.0.0.0` なので、LAN から届く誰でも装置を
動かせる（ステージ移動・ペースト吐出・ジョブ実行）。ファイルブラウザからは backend 機の
`/media` / `/mnt` が読める。信頼できない範囲に晒す場合はファイアウォールか前段の
リバースプロキシで認証をかける。

### 環境変数

backend（正典は `src/web/api/settings.py` の `Settings.from_env`）:

- `PCBASM_API_PORT` — 待ち受けポート（既定 8081）
- `PCBASM_API_DATA_DIR` — 成果物・状態ファイルの保存先
- `PCBASM_API_PCB_ROOT` — ファイルブラウザの root（指定すると閲覧許可にも追加される）
- `PCBASM_API_FAKE_CAMERA` — `1` でカメラ実機なしの固定画像配信
- `PCBASM_API_FAKE_CAMERA_IMAGE` — その固定画像のパス
- `PCBASM_API_DISCOVERY_ENABLED` — `0` で mDNS 広告を無効
- `PCBASM_API_UPDATE_ENABLED` — `0` で WebUI からの更新を無効（実行系は 403）
- `PCBASM_API_UPDATE_UV_SYNC_ARGS` — `uv sync` の引数を**丸ごと置き換える**（空白区切り。既定 `--locked --inexact`）。`--locked` を落とすと `uv.lock` が書き換わって以後の更新が全部止まるので、足すときも既定の 2 つは必ず残す
- `PCBASM_API_UPDATE_STATE_DIR` — 更新の記録と単一実行ロックの置き場所（既定はリポジトリ直下の `data/selfupdate`。**`PCBASM_API_DATA_DIR` では動かない** — ロックが守るのは worktree なので、同居機の backend と frontend が必ず同じファイルを掴む）
- `PCBASM_MAINSAIL_URL` — Mainsail へのリンク先
- `PCBASM_CONFIG_DIR` — マシン設定ディレクトリ（既定 `config/`）の差し替え。pcbasm コア層と共通

backend の待ち受けアドレスは環境変数では変えられない（`PCBASM_API_HOST` は無く、常に
`0.0.0.0`）。特定アドレスに絞るならファイアウォールか前段のリバースプロキシで行う。

frontend（正典は `src/web/ui/settings.py` の `Settings.from_env`）:

- `PCBASM_UI_HOST` / `PCBASM_UI_PORT` — 待ち受け（既定 `0.0.0.0` / 8080）
- `PCBASM_UI_MACHINES_FILE` — machines.toml のパス（既定 `config/machines.toml`）
- `PCBASM_UI_DEFAULT_BACKEND_PORT` — machines.toml で `port` を省いたマシンに使う port（既定 8081）
- `PCBASM_UI_DISCOVERY_ENABLED` — `0` で mDNS 探索を無効
- `PCBASM_UI_UPDATE_ENABLED` — `0` で frontend 自身の更新を無効
- `PCBASM_UI_UPDATE_UV_SYNC_ARGS` — `uv sync` の引数を**丸ごと置き換える**（空白区切り。既定の `--locked --inexact` は残すこと）
- `PCBASM_UI_UPDATE_STATE_DIR` — 更新の記録と単一実行ロック（既定はリポジトリ直下の `data/selfupdate`。backend の既定と同じ場所）
- `PCBASM_UI_SSR_TIMEOUT` / `PCBASM_UI_BACKEND_CONNECT_TIMEOUT` / `PCBASM_UI_PROXY_READ_TIMEOUT` — 秒

frontend が `config/` から読むのは `machines.toml` **だけ**（機体設定の `machine.toml` は読まない）。
そのため `PCBASM_CONFIG_DIR` は持たず、これを変えても `machines.toml` の場所は動かない
（既定はリポジトリ直下の `config/machines.toml` 固定）。**場所を変えるノブは
`PCBASM_UI_MACHINES_FILE`。**

### E2E

WebUI のブラウザ E2E は実 uvicorn（backend + frontend の 2 プロセス）と Chromium で検証する:

```sh
make test-e2e
make playwright-install  # /usr/bin/chromium が無い環境向け
```
