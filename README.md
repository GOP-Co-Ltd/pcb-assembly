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

OS以外のソフトウェア類は[`install-softwares.sh`](install-softwares.sh)を実行

### GitLab CI Runner

専用のRaspberry Pi 5をGitLab Runnerとして構築する場合は、
[`gitlab-runner/README.md`](gitlab-runner/README.md)を参照する。

### 開発

上記のソフトウェアをインストールしたうえで、次を実行

```sh
make setup
```

- VSCodeでリモートアクセスし、開発することを推奨する。

### マシン設定の配置

装置を動かす前に、テンプレートから `config/` を作る。

```sh
./setup-machine-config.sh       # data/config-templates/ から選んで config/ を作る
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
再起動する**（実行中に更新されるのは mDNS 探索の分だけ）。`setup-machine-config.sh` が案内する
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
./web-service.sh install all  # 同居機（backend + frontend の両方を置く 1 台）
./web-service.sh install      # 機体（backend のみ。= install api）
./web-service.sh install ui   # frontend 専用機（UI だけを置くホスト）
./web-service.sh start ui     # 起動（stop / restart / status も同じ形）
./web-service.sh remove all   # サービス登録を削除
```

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

### ソフトウェア更新

ソフトウェア更新は、ホストへ直接配置して systemd で動かす構成だけを対象とする。Podman
などのコンテナ構成は対象外。管理 checkout から、ホストの役割に合わせて更新用 unit と
timer も導入する。

```bash
./web-service.sh install api  # backend 機
./web-service.sh install ui   # frontend 機
./web-service.sh install all  # 同居機
```

runtime は `/var/lib/pcbasm` 以下へ配置する。

- `releases/<revision>/` — revision ごとの release worktree
- `current-api` / `current-ui` — role ごとに稼働中 release を指す symlink
- `state/status-api.json` / `state/status-ui.json` — 確認・適用・rollback の状態

timer は起動後と 15 分ごとに更新の有無を**確認するだけ**で、自動適用しない。適用は
WebUI で対象 revision を確認し、「適用」を明示操作したときだけ行う。

更新元は、管理 checkout で現在選択している branch と同名の `origin/<branch>` に固定する。
管理 checkout の dirty な変更や未 push の commit は release に入らない。SSH remote を使う
ホストでは、サービス実行ユーザー用の read-only deploy key を登録し、そのユーザーの
`~/.ssh/known_hosts` を事前に用意して、対話なしで fetch と Git LFS の取得が通ることを確認する。

同じ branch の non-fast-forward（force-push）は適用を block する。管理 checkout の branch を
切り替えた場合は、誤操作防止のため新しい branch 名の完全入力が必要。どちらも通常の確認
dialog だけでは解除されない。

更新先の `deploy/os-packages.txt` に不足 package がある場合、自動更新は block する。内容を
確認して、管理 checkout で次を明示実行してから再確認する。

```bash
./install-os-packages.sh --ref origin/<branch>
```

更新処理自身は APT を実行せず、ホストを自動 reboot しない。`deploy/schema-version` が導入済み
version と一致しない場合も自動適用せず、対象変更の手順に従って手動更新した後に
`./web-service.sh install <target>`（`target` は `api` / `ui` / `all`）を実行する。

適用後の health 確認に失敗すると、失敗した role だけを直前の release へ rollback する。
`api` と `ui` の release は独立しており、片方の失敗で他方を戻さない。
`./web-service.sh remove <target>` は unit・timer・sudoers の登録だけを削除し、release と status
は保持する。`config/`、`uploads/`、`data/webui/` は管理 checkout 側に永続化され、release の
切り替えやサービス削除では消えない。

更新画面は frontend host 自身が `/software-update`、選択中の機体を含む画面が
`/m/{machine_id}/software-update`。状態取得・確認・適用 API はそれぞれ
`GET /api/software-update`、`POST /api/software-update/check`、
`POST /api/software-update/apply` で、機体 backend への frontend 経由の path は先頭に
`/m/{machine_id}` が付く。

この更新機能も trusted LAN 前提で認証を持たない。確認 dialog と branch 名入力は誤操作を
防ぐ確認であり、本人確認や権限分離ではない。信頼できないネットワークへ公開しない。

実機への適用後は、次を確認する。

- 対象 role の systemd service が active で、更新画面の稼働 revision と一致する
- frontend から機体を選択でき、API、WebSocket、カメラ映像の中継が復旧する
- backend 機では、安全を確保したうえでカメラ、原点復帰・ステージ移動、ペースト吐出、
    通知音など、その機体で使用する実機機能が動作する
- 既存のマシン設定、upload、ジョブ成果物が更新前から保持されている

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
- `PCBASM_MAINSAIL_URL` — Mainsail へのリンク先
- `PCBASM_CONFIG_DIR` — マシン設定ディレクトリ（既定 `config/`）の差し替え。pcbasm コア層と共通

backend の待ち受けアドレスは環境変数では変えられない（`PCBASM_API_HOST` は無く、常に
`0.0.0.0`）。特定アドレスに絞るならファイアウォールか前段のリバースプロキシで行う。

frontend（正典は `src/web/ui/settings.py` の `Settings.from_env`）:

- `PCBASM_UI_HOST` / `PCBASM_UI_PORT` — 待ち受け（既定 `0.0.0.0` / 8080）
- `PCBASM_UI_MACHINES_FILE` — machines.toml のパス（既定 `config/machines.toml`）
- `PCBASM_UI_DEFAULT_BACKEND_PORT` — machines.toml で `port` を省いたマシンに使う port（既定 8081）
- `PCBASM_UI_DISCOVERY_ENABLED` — `0` で mDNS 探索を無効
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
