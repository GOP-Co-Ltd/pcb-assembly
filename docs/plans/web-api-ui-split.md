# WebUI を backend WebAPI（`web.api`）と UI frontend（`web.ui`）に分離する

計画書。実装は未着手。MR ごとの分割・確定した設計判断・検証方法をまとめる。

## 背景と目的

現在 `src/webui/` の単一 FastAPI プロセスが「Jinja SSR ページ + `/api` JSON + MJPEG + WebSocket + `/artifacts`」を全部担い、1 マシン = 1 プロセスで固定されている。マシン選択機能は過去に意図的に全廃されており（commit `cbdf8ac`）、複数ユーザーの同時利用も想定されていない（認証・CORS・Cookie・セッションが一切なく、選択 PCB もジョブフォーム既定値もプロセス内グローバル 1 個）。

これを次の形にする。

- **backend WebAPI** を各マシン（機体）のラズパイで動かす。装置制御・カメラ・設定ファイル・ジョブ実行はそのマシンに閉じる。
- **UI frontend** を 1 台のラズパイで動かす。LAN 上の backend を mDNS で発見し、マシン選択ドロップダウン（表示は「マシン名 (hostname: ip)」）で切り替える。backend 機に同居させることもでき、単独でも起動できる。
- **複数ユーザーの同時利用**を前提にする。閲覧は自由、操作は 1 セッションのみ（操作権リース）。緊急停止と abort は安全のため誰でも常に可能。

副産物として、単一ユーザー前提に依存した既存バグ（設定の lost update）も潰す。

現状の構造は分離に対して素性が良い。HTML を返すのは `routers/pages.py` の 4 本だけで、残り 10 router はすべて `APIRouter(prefix="/api")`。HTMX 的な部分 HTML 返却は 0 件。JS の `/api/...` literal は 31 本あるが全部 `static/js/app.js:17` の `api()` を通り、WebSocket URL は `job_console.js:192` の 1 箇所しかない。

> 以下、現行コードの参照はすべて `src/webui/...` 表記。MR3 以降は `src/web/api/...` に読み替える。

## モジュール構造

```
src/
  pcbasm/                装置ドメイン層（変更なし。async も pydantic も持ち込まない）
  web/
    __init__.py
    api/                 backend WebAPI（各マシンで動く。現 src/webui/ から templates・static・pages を除いた全部）
      app.py  state.py  preview.py  config_store.py  board_settings.py  settings.py  __main__.py
      models.py          ← 共有 contract（pydantic のみ import）
      discovery.py       ← mDNS 広告 + 共有定数（MR5）
      control.py         ← 操作権リース（MR6）
      jobs/  routers/
    ui/                  UI frontend（1 台で動く。UI 配信 + 発見 + プロキシ）
      app.py  settings.py  pages.py  layout.py  __main__.py
      machines.py        ← MachineEndpoint / MachineRegistry
      machine_client.py  ← BackendGateway / MachineClient（backend への HTTP クライアント）
      proxy.py           ← 純 ASGI リバースプロキシ（HTTP / WS / MJPEG）
      discovery.py       ← mDNS 探索（MR5）
      templates/  static/
tests/
  pcbasm/
  web/api/   ← 現 tests/webui/ の大半
  web/ui/    ← 現 tests/webui/routers/test_pages.py + 新規
  e2e/
```

`.venv` の `pcbasm.pth` が `src/` 自体を sys.path に載せる（実測）ため、`src/web/__init__.py` を置くだけで `web.api` / `web.ui` が import できる。`pyproject.toml` の build 設定は触らない。

**データパスは一切改名しない** — `data/webui/`・`webui_state.json`・`Settings.webui_data_dir`・`/artifacts` のマウント元。改名すると実機の選択 PCB・基板別塗布 override・ジョブ成果物が丸ごと孤立する。

## 目標アーキテクチャ

```
              ┌────────── frontend 機（1 台。backend 機に同居も可） ──────────┐
  ブラウザ ──▶│ web.ui  :8080                                                │
              │  SSR ページ（既存 Jinja テンプレをそのまま）+ /static         │
              │  MachineRegistry（mDNS 探索 + config/machines.toml 静的登録）  │
              │  リバースプロキシ（HTTP / WS / MJPEG / アップロード）          │
              └─────────┬────────────────────────┬─────────────────────────┘
                        │ /m/{machine_id}/api/** │
            ┌───────────▼────────┐   ┌───────────▼────────┐
            │ web.api  :8081     │   │ web.api  :8081     │  … 各マシン
            │ 装置制御 / カメラ / │   │                    │
            │ ジョブ / machine.toml│  │                    │
            └────────────────────┘   └────────────────────┘
```

URL 空間（frontend）:

```
GET /                                   既知 1 台 → 307 /m/{id}/posctrl、0 台 → 案内、複数 → ピッカー
GET /{tab}[/{feature}], /settings        未 prefix → 同じ規則で 307（既存ブックマークと既存テストの受け皿）
GET /m/{machine_id}/{tab}[/{feature}]    SSR ページ
GET /m/{machine_id}/settings             ※ /{tab} より先に登録
    /m/{machine_id}/api/**               Mount → backend /api/**（http + websocket）
    /m/{machine_id}/artifacts/**         Mount → backend /artifacts/**
    /static/**                           frontend 所有。prefix しない（ブラウザキャッシュを 1 本で共有）
```

## 確定した設計判断

| #   | 判断                                                                                                          | 根拠                                                                                                                                                          |
| --- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | frontend は **リバースプロキシ**。ブラウザから見て常に同一オリジン                                            | CORS・WS URL の絶対化・artifact 相対 URL・MJPEG URL の問題がまとめて消える                                                                                    |
| 2   | **SSR を維持**し、frontend の page ハンドラが backend の HTTP クライアントになる                              | client-render 化するとテンプレ 887 行 + JS 3390 行の書き換えと `test_pages.py` 61 件の破棄が発生する                                                          |
| 3   | マシンは **パス prefix** `/m/{machine_id}/…`。`machine_id` = backend ホストの hostname                        | Cookie 方式では 1 ブラウザで 2 マシンを別タブ表示できない。WS と MJPEG（`img.src`）はブラウザから独自ヘッダを付けられないので、per-tab 化はパスにしか載らない |
| 4   | prefix 付与点は **`app.js` の `withBase()` 1 箇所**。backend の応答は常に backend 相対のまま                  | JS の `/api` literal 31 本が単一 funnel を通る。`<base href>` は先頭スラッシュ付き絶対パス（ログ本文の `/artifacts/...`）を解決しないので不採用               |
| 5   | パッケージは `src/web/api/`（backend）と `src/web/ui/`（frontend）。データパスは不変                          | 名前と役割を一致させる。`web` 配下にまとめることで「Web 層は 2 プロセス」が構造から読める                                                                     |
| 6   | ポートは **frontend 8080 / backend 8081**（変更は MR4）                                                       | 同居機の既存ブックマーク `http://kurousagi.local:8080` がそのまま UI に着地する                                                                               |
| 7   | 発見は **mDNS/Zeroconf 広告 + 静的登録の併設**。`AsyncZeroconf` を lifespan に載せる                          | 同期 `Zeroconf` は running loop 内で構築すると自 loop 待ちになる。Wi-Fi のマルチキャスト抑制環境では静的登録が保険                                            |
| 8   | 操作権リースの liveness は **WS 在線**（heartbeat タイマーを作らない）                                        | Chrome は背景タブの `setInterval` を 1/min にクランプするため、実用的な TTL では「閲覧のためタブを裏に回した操作者のリースが失効する」事故になる              |
| 9   | リースは **claim 方式**（変更系リクエストが空きリースを自動取得）。明示 acquire は API として公開するだけ     | 既存 553 テスト（`TestClient` = ヘッダ無し）が `session_id="anonymous"` フォールバックで無改変に通る                                                          |
| 10  | リース不足は **423 Locked**。既存 `BusyError` → 409 `{detail, owner}` の契約は変えない                        | 409 は既に 4 用途で多重使用されており、「待てば解決する」と「操作権を取れば解決する」を区別できなくなる                                                       |
| 11  | 無制限（誰でも可）は **`POST /api/emergency-stop` / `POST /api/jobs/current/abort` / WS `abort`** の 3 つだけ | `firmware-restart` は停止ではなく「他人のジョブを abort して MCU を再起動する復帰動作」（`routers/system.py:39-48`）なのでリース必須                          |
| 12  | `machine_lock`（装置が動作中）と `ControlLease`（誰が操作していいか）は**統合しない**                         | 前者はハードウェアの相互排他、後者は認可。`busy_owner`（`"job:paste_solder"` 等）を人の名前で上書きしない                                                     |
| 13  | `machine.toml` に**楽観ロックは入れない**                                                                     | HTTP 経路 4 本すべてが `machine_lock` を取得済みで完全に直列化されている。`settings.js` は 1 キーずつ PUT するので `If-Match` は 412 を量産する               |
| 14  | 新 pytest マーカーは追加しない                                                                                | `--strict-markers` の下で `hardware` / `e2e` / `browser` の 3 つを維持。mDNS は実行時能力プローブで skip                                                      |
| 15  | env prefix は backend `PCBASM_API_*` / frontend `PCBASM_UI_*`（互換シムなし）                                 | 参照は `settings.py` と `Makefile` のみでリポジトリ内に閉じる。`PCBASM_CONFIG_DIR` は pcbasm 所有なので不変                                                   |

## MR 分割

```
MR1 ─┐
MR2 ─┴→ MR3 → MR4 → ┬→ MR5 ─┐
                     └→ MR6 ─┴→ MR7
```

MR1 と MR2 は触るファイルが重ならないので並列可。MR3 は全ファイルの import 行に触るので**単独で出して即マージ**（他ブランチと並走させない）。MR5 と MR6 は MR4 後に並列開発可。

各 MR 末で `make format && make type && make test-no-hardware`。MR4 以降は加えて `make test-e2e`。`make test` / `@mark_hardware` は Claude は実行しない（実機確認はユーザー）。

______________________________________________________________________

### MR1 — `fix(webui)`: 同時編集で設定が黙って失われる RMW を排他化

現行でも 1 人で再現する既存バグ。`pad_editor/index.js:336-346` の debounce が `nodeId|field` 単位に分かれ、`load_board` が毎回 `PcbFile` をパースするため 2 本の PATCH が確実に重なる。

**新規** `src/webui/atomic.py` — `write_text_atomic(path, text)`（同一ディレクトリ `NamedTemporaryFile` → `Path.replace`）。同処理を既に持つ 2 箇所（`config_store.py` の書き戻し末尾、`board_settings.py:231-253`）をここへ寄せる。

**変更**

- `board_settings.py` に `update(source_pcb, base_config, *, board_signature, mutate) -> PasteSettingsModel` を追加。`threading.Lock` 内で **再 load → mutate → atomic write**。`mutate` に渡すのは `PasteSettingsModel` の純変換（`with_level_patch` / `with_pads_enabled` / `with_initial_purge_pad_id`）だけで I/O 禁止。`save` は「単発上書き専用」と docstring に明記。
- `routers/pasting.py` の 3 箇所（`:79-108` / `:111-132` / `:135-190`）を「PCB パース・階層構築・検証はロック外 → `update(mutate=…)` → 戻り値から `affected_pads` を組む」へ組み替える。`:189` の 2 回目 `load_board`（同一リクエストで PCB を 2 回パースしている）は戻り値で置き換わる。
- `jobs/manager.py:411-413` の `BoardSettingsStore` 自前生成を削除し、コンストラクタ引数で必須受け取りにして `app.py:100-103` の 1 インスタンスに統合する。**これをやらないとロックが効かない**（worker と HTTP が別インスタンスを掴んでいる）。
- `state.py`: `_persist_lock`（`threading.Lock`）を追加し、**ロック順序を `machine_lock → _persist_lock` に固定**して docstring に 1 行書く。`save_job_param_defaults` を `merge_job_param_defaults(job_name, values) -> dict`（ロック内で読む → マージ → 永続化 → 結果を返す）に置き換え、`routers/jobs.py:115-119` と `jobs/manager.py:556-563` の手書き二重マージを削除。`_persist`（`:233-239`）を `write_text_atomic` に。
- pad PATCH に `expected_pcb: str | None` を追加し、選択中 PCB と不一致なら 409（他人が PCB を切り替えた直後の編集が別基板の JSON を汚す事故を止める。`None` は従来通り通すので既存テストは無改変）。

**テスト** — `tests/webui/test_atomic.py`（新規・小）。`test_board_settings.py` に `class TestUpdate`: **ロック内再ロードの証明**（`update` A の後にファイルを直接書き換えてから `update` B → B の結果にファイル側の変更が残る）、実スレッド 2 本で別ノードを同時 update して両方残る。`test_state.py`: `merge_job_param_defaults` のマージ、実スレッド 2 本の同時保存で `webui_state.json` が常に valid JSON、`machine_lock` 保持中の別スレッドから merge がブロックせず完了する（ロック順序の回帰）。`grep -rn 'JobManager(' src tests` で生成箇所を一度に直す。

**実機確認** — 2 ブラウザで別 pad を同時編集して両方残る。ジョブ実行中の pad 編集で設定が消えない。

______________________________________________________________________

### MR2 — `feat(webui)`: backend の自己申告 API と `machine_name`

SSR 専用値をすべて JSON API から取れるようにする（MR4 の前提）。SSR は維持したまま**追加のみ**。

**新規エンドポイント 2 本**

- `GET /api/machine-info` → `MachineInfo{machine_id, machine_name, machine_type, mainsail_url, fb_start, api_version}`。到達性プローブも兼ねる（`Machine()` 3.8ms + `gethostname()` なので `/api/health` は作らない）。`mainsail_url` は backend 側で解決した値（`pages.py:240-241` の `request.url.hostname` フォールバックはプロキシ配下で必ず誤るので削除）。
- `GET /api/jobs` → `JobCatalogResponse{jobs: list[JobSpecInfo]}`。`JobCatalog.list()`（現在 src から未使用）を入口にし、`params` は `_param_specs_with_saved_defaults`（`pages.py:212-227`）適用後。**hidden も filter せず全件返す**（`tests/e2e/conftest.py:57-66` が hidden ジョブを実行時登録する）。

**既存 API の変更**

- `StateResponse` に `nozzle_cap: Position | None` を追加。値は**新設 `AppState.nozzle_cap()`（try/except → None）**から取る。`Machine.nozzle_cap` は x/y/z 必須の cattrs structure なので、`[nozzle_cap]` が無い machine.toml で設定画面から X だけ保存すると例外を投げる（現在はノズルキャップページだけが 500 する既存バグ）。防御せずに `/api/state` に載せると**全ページの SSR と全クライアントのポーリングが 500 する**。この回帰テスト 1 本が MR2 の要。
- `JobDefinition` に既定値付きで 3 フィールド追加: `provides_preview` / `loading_param` / `loading_stages`。現在 `pages.py:123,132,143` にある `_PASTING_PREVIEW` / `_PASTING_LOADING_PARAM` / `_LOADING_STAGE_OVERRIDE` は backend の事実（progress_stage 文字列・フレーム提供有無）なので frontend に置くとプロセス境界を越えた文字列契約が UI に残る。`pages.py:388` の `if tab == "pasting"` 特別扱いも消える。
- `models.py` に `routers/common.py` の `StateResponse` / `SettingsField` と新規モデルを移設（`routers/common.py` は再 export して既存 import を壊さない）。**このファイルは pydantic のみ import** を維持 → 分割後そのまま frontend から import 可能な唯一の contract モジュールになる。

**`machine_name`（要件の表示名。現在どこにも存在しない）**

`pcbasm/config.py` に `Machine.machine_name -> str | None`、`MACHINE_FIELDS` 先頭に `FieldSpec("machine_name", "マシン名", "str")`、`SECTION_LABELS` に `"machine_name": "マシン"`（`section_of` が生キーを返しラベルに出てしまう）。hostname フォールバックは環境依存なので API 層で解決。`Settings.hostname: str | None = None` を追加して注入可能にする（1 ホストに 2 backend を立てる e2e のため。`socket.getaddrinfo(gethostname())` は `/etc/hosts` の `127.0.1.1` を掴むので使わない）。tomlkit のトップレベル bare key 追記は実測で安全（`machine_type` の直後に挿入される）だが、書き込み後に `tomllib` 再パースしてトップレベルに残ることを assert してピンする。

**ファイル公開範囲を絞る（別コミット）**

`pcb_browse_root = Path("/")` は**変えない**。`board_id = sha256(pcb_browse_root 相対パス)`（`board_settings.py:63-73`）なので root を変えると既存の基板別塗布 override が全部孤立する。代わりに `Settings.pcb_browse_allowed: tuple[Path, ...]`（既定 = リポジトリルート + `/media` + `/mnt`）を追加し、`files.py:33` の `_resolve_under_root` とアップロード先で許可サブツリーを検証する。`GET /api/files` の列挙も許可範囲に限定する。分離により無認証の口が台数分並ぶため、任意ファイル閲覧の面を先に潰しておく。

**実機確認** — 設定画面で「マシン名」を保存 → machine.toml のコメントが残りトップレベルに `machine_name` がある。`curl :8080/api/machine-info` / `curl :8080/api/jobs`。USB（`/media/...`）から PCB を選べる。

______________________________________________________________________

### MR3 — `refactor`: `src/webui` → `src/web/api` の機械的リネーム

**振る舞い変更ゼロ。ロジック変更を 1 行も混ぜない。**

- `mkdir src/web && touch src/web/__init__.py`、`git mv src/webui src/web/api`、`git mv tests/webui tests/web/api`。`git mv` のコミットと文字列置換のコミットを分けて `--find-renames` でレビューできるようにする。
- 置換は **`\bwebui\.`（モジュール参照 218 箇所）と import 行のみ** → `web.api.`。`webui_data_dir` / `webui_state` / `settings.py:43` の文字列 `"webui"` / `data/webui` は**残す**（`s/webui/webapi/g` の一括置換は実機の選択 PCB・board_settings・成果物を丸ごと見えなくする）。
- `tests/e2e/conftest.py:29` の `from tests.webui.conftest import …` → `from tests.web.api.conftest import …`。
- env prefix を `PCBASM_WEBUI_*` → `PCBASM_API_*` に改名（互換シムなし）。**ポート既定値は変えない**（MR4 で変える）。
- `Makefile` に `api` / `api-dev` / `api-fake` を新設し、**`webui` は backend を指すエイリアスとして MR7 まで残す**（実機の `pcbasm-webui.service` の `ExecStart=make webui` を殺さないため）。
- `.gitlab-ci.yml` / `CLAUDE.md` / `README.md` / `.claude/skills/{webui-e2e,webui-thin-wrapper,testing-strategy}` の記述も追随。

**検証** — pyright が import 漏れを全部拾う。`grep -rn '\bwebui\b'` で残存を確認（残ってよいのはデータパス系だけ）。

______________________________________________________________________

### MR4 — `feat`: frontend パッケージ + リバースプロキシ + `/m/{machine_id}` prefix

要件の本体。frontend は machine.toml を要求せず単独起動でき、同居もできる。

**新規パッケージ `src/web/ui/`** — `templates/` 23 ファイルと `static/` を `src/web/api/` から移設（内容はほぼ無改造）。加えて:

- `settings.py` — `host` / `port=8080` / `machines` / `machines_file` / `ssr_timeout=2.0` / `backend_connect_timeout=2.0` / `proxy_read_timeout=120.0` / `default_backend_port=8081`。`from_env` は `PCBASM_UI_*`。**`config_dir` / `data_dir` / `pcb_*` / `fake_camera` を一切持たない**（`get_config_dir()` を呼ばない = machine.toml 不在ホストで起動する。同居機で `data/webui` を backend と共有しない）。
- `machines.py` — `MachineEndpoint`（`base_url` と **`label` = `f"{name} ({machine_id}: {host})"`**。表示文字列はサーバ側で組む）と `MachineRegistry`（`list()` / `resolve()`）。静的登録は `config/machines.toml`（`tomllib` で読むだけ、書き込み API は作らない）。
- `proxy.py` — FastAPI ルートではなく**純 ASGI アプリ**（body が依存解決に巻き込まれない / http と websocket を 1 つの Mount で扱える）。`Mount("/m/{machine_id}/api", ProxyApp(...))` が http/websocket 両方で `scope["path_params"]` を埋め、`get_route_path()` が remainder を返す。`query_string` を target に付け直すのを忘れない（`/api/preview/stream?overlay=…`）。
    - リクエストヘッダ: `host` / hop-by-hop / **`cookie`（frontend のセッション cookie を backend に漏らさない）** を落とし、`x-forwarded-for` だけ足す（uvicorn の `ProxyHeadersMiddleware` は `X-Forwarded-Host` を読まず `trusted_hosts` 既定が `127.0.0.1` なので、`X-Forwarded-Proto/Host` の注入は無意味）。
    - ボディは `Request(scope, receive).stream()` を httpx の `content` に渡す（1 チャンクしかメモリに載らず、pull chain でバックプレッシャが効く）。
    - レスポンスヘッダ: **`date` と `server` を必ず落とす**（uvicorn が `default_headers` を無条件 prepend するため重複する）。多値ヘッダを潰さないため `StreamingResponse(headers=dict)` を使わず、構築後に `response.raw_headers` を代入する。
    - エラー: `ConnectError` / `ConnectTimeout` → 502、`ReadTimeout` → 504、その他 → 502。`{"detail": …}` 形なので `app.js:28-31` の既存トーストに乗る。
    - **MJPEG（最大の罠）**: 上流 `httpx.Response` を閉じ忘れると別プロセスの backend のカメラが永久に回る。`StreamingResponse` を継承して `stream_response` の finally と `__call__` の `except CancelledError` の両方から `anyio.CancelScope(shield=True)` 越しに `await upstream.aclose()` する（`web/api/routers/preview.py:44-70` の async 版）。プロキシの close が backend 既存の `hold_camera()` finally のトリガになる。タイムアウトは `/preview/stream` のときだけ `read=None`、他は 120s（`POST /api/machine-control` は M400 待ちで最大 60s）。multipart は再解析せずバイト列をそのまま流す。
    - **WS 中継**: 1 ブラウザ = 1 上流 WS（fan-out で共有しない。共有すると MR6 の操作権をセッションに帰属させられなくなる）。`ws.receive()` → **上流接続成功後に** `accept()`（失敗時は accept せず close。`job_console.js:203-206` のバックオフ再接続が効く）→ 2 タスクで中継し `asyncio.wait(FIRST_COMPLETED)`。`websockets` の `connect(..., proxy=None)` を**明示**（既定 `proxy=True` で `HTTP_PROXY` / `ALL_PROXY` env を読むため、proxy env のある環境で LAN 内 WS が全滅する）。close code 1005/1006 は 1011 に丸める。
- `machine_client.py` — `BackendGateway`（`machine_id` 毎に `httpx.AsyncClient` をキャッシュして keep-alive。`transport_factory` を注入可能にしてテストで実 backend app を挿す）と `MachineClient`（`machine_info` / `state` / `machine_settings` / `jobs`）。ページごとに必要なものだけ `asyncio.gather` で並列取得。**1 本でも落ちたら `BackendUnavailable` → 503 ページに一本化**（欠損値のフォームを見て書き込み操作をされるのが最悪）。**backend レスポンスはキャッシュしない**（frontend は自前の WS 購読を持たないので無効化条件を正しく書けない）。
- `layout.py` — `pages.py:31-168` から純粋な表示知識を移設（`TABS` / `TAB_LABELS` / `FEATURE_LABELS` / `TAB_PHASES` / `FEATURE_TEMPLATES` / `_JOB_TEMPLATES` / `_PASTE_AUTO_THRESHOLD_KEYS` / `_LOADING_ROTATION_PARAMS` / `_DISPENSE_CALIBRATION_PARAM_GROUPS` / `SECTION_LABELS` / `section_of` / `_grouped_fields`）。
- `pages.py` — 全ハンドラを `async def` に。`_base_context` は gather 結果から組み、`base=f"/m/{machine_id}"` / `machine_id` / `machines` / `current_suffix` を追加。feature 別 context 関数は入力を `AppState` / `ConfigStore` から `MachineSettingsResponse` / `JobSpecInfo` / `StateResponse` に差し替えて移設。
- `app.py` — **登録順を厳守**: `Mount("/m/{machine_id}/api")` → `Mount("/m/{machine_id}/artifacts")` → `include_router(pages)`（逆だと `/m/x/api/state` が `/m/{machine_id}/{tab}/{feature}` に食われる。`web/api/app.py:135` と同じ理由でコメントを残す）。`/static` は prefix なしで mount。例外ハンドラ: `BackendUnavailable` → 503 HTML + `Retry-After: 5`、`UnknownMachine` → 404 HTML。どちらも `base.html` で描く（ドロップダウンは frontend registry 由来なので backend 不要 = 必ず描ける）。lifespan の yield 後に `gateway.aclose()`。
- `__main__.py` — `_WebUIServer`（`PreviewService.request_shutdown` のためだけに存在する）は持ち込まない。`setup_logging(namespaces=("pcbasm", "web"))`。
- `templates/partials/machine_selector.html` + `static/js/machine_selector.js` — `option.value = /m/{id}/{current_suffix}`、表示は**サーバが組んだ `label` をそのまま `textContent`**。切替は `location.assign(value)` の数行。

**JS / テンプレの編集（10 箇所）**

- `app.js`: `const BASE = document.body.dataset.machineBase ?? ""` / `withBase(path)` / `fetch(withBase(url), options)`（`:26`）/ `window.webui` に `withBase` を公開 → **`/api` literal 31 本がこの 1 箇所で乗る**。
- `job_console.js`: `:192` WS URL、`:295` ログ内 `/artifacts/...` リンク化、`:394/399/408` の artifact `img.src` / `href`（1 ヘルパにまとめる）。
- `preview.js:31`: `withBase(pane.dataset.streamUrl)`。
- テンプレ: `base.html:13` / `base.html:22` / `partials/sidebar.html:5` の `href` に `{{ base }}`、`base.html` に `<body data-machine-base="{{ base }}">`。
- **変更しない**: `settings.html:72` と `paste_solder.html:11` の `data-endpoint`、`preview_pane.html:2` の `data-stream-url`（bare のまま。`api()` と `preview.js` が吸収するので二重 prefix を防ぐ）、`job_console.js:74` の完了音 `/static`、`app.js:145/176` の `window.location.reload()`、`machine_control.js` の localStorage 2 キー。

**運用ファイル** — `web/api/settings.py` の `port` 既定を 8081 に。`Makefile` に `ui` / `ui-dev` / `ui-fake`（8098 + 静的登録 1 台 = 127.0.0.1:8099）を追加し、`api-fake` は 8099。`.gitlab-ci.yml` の `.python-validation.rules.changes.paths` に **`src/web/ui/templates/**` と `src/web/ui/static/**` を追加**（現在テンプレ・JS だけの変更では pytest も pyright も走らない）。

**あわせて入れる小改善** — `state_changed` に `scope` を追加し、`settings.js` / `pad_editor` が未編集時に再取得する（他人の変更が画面に出る）。`klipper_status.js:32` と `machine_control.js:239-242` のポーリングを `visibilitychange` で停止する（各 3 行。放置タブ分の Moonraker 負荷を削る）。

**テスト**

- `tests/web/ui/conftest.py`: `frontend_app` に `transport_factory=lambda ep: httpx.ASGITransport(app=backend_app)` を渡して**実物の backend app を in-process で駆動**する（httpx 純正 transport なので 3rd-party モックではない）。docstring に切り分け理由を書く: **`ASGITransport` はレスポンスを全部バッファしてから返し websocket も非対応**なので MJPEG / WS の in-process テストは永久ハングする → in-process は JSON プロキシと SSR ページ専用、MJPEG / WS は `tests/e2e` で 2 プロセス。
- `tests/web/ui/test_proxy.py`: GET/PUT/PATCH/POST の透過、実 `.kicad_pcb` の multipart アップロードが backend の `pcb_upload_dir` に着く、backend の 409 `{detail, owner}` が透過、**応答に `date` / `server` が 1 つずつしかない**、hop-by-hop が消えている、未知 machine_id → 404、**`/m/x/api/state` が pages ルータに食われない**（登録順の回帰）。
- `tests/web/ui/test_proxy_unreachable.py`: `machines=(…127.0.0.1:1…)` で**実 ECONNREFUSED**（MockTransport は使わない）→ `/m/dead/api/state` が 502 JSON、`/m/dead/posctrl` が 503 HTML でドロップダウンを含む。
- `tests/web/ui/test_pages.py`（現 `tests/web/api/routers/test_pages.py` 61 件の移設）: **本文無改変で通すことを MR4 の合格条件にする**（fixture 差し替えのみ）。`TestClient` の `follow_redirects` 既定が True なので未 prefix リダイレクト経由で 58 件がそのまま通り、変わるのは Location を assert する 1 件と mainsail href の 1 件だけ。追加で: 全 `href` が prefix 付き、`machine.toml` が無い `tmp_path` でも `create_app` が成功して `/settings` が 200、`/` が 1 台なら 307・複数ならピッカー・0 台なら案内。
- `tests/e2e/conftest.py`: `live_server` の起動処理を `_serve(app) -> Iterator[int]` に切り出し、`LiveServer` に `port` を追加（既存 3 属性は不変 = 50 件に影響なし）。`live_ui`（`live_server.port` から静的登録 1 台の frontend を起動）、`live_ui_two`（`copy_testing_config(tmp_path / "a")` / `"b"` + `Settings(hostname="alpha"/"bravo")` で machine_id を分ける）、`browser_page` を `browser_pages` ファクトリに一般化して `pytest_collection_modifyitems` の判定を拡張。既存 e2e のページ取得・`goto` **約 20 箇所**を `live_ui.base_url` に差し替え（`test_webui_e2e.py` にもページ取得が 6 箇所ある）。API 直叩きは `live_server.base_url` のまま = backend 直で経路を意図的に分離。
- `tests/e2e/test_proxy_e2e.py`（新規）: **MJPEG を数バイト読んで close 後に backend の `preview_clients` が 0 に戻る**（上流未 close によるカメラリークの回帰。最重要）、2 クライアント同時で 2 → 片方 close で 1、WS 経由でジョブ起動・prompt 応答・abort、backend を落とすと frontend 経由 WS が close する、不在 machine_id の WS は accept されず即 close、artifacts の bytes と Range 206 の透過、未 prefix ページの 307、`live_ui_two` で 2 マシンが並ぶ。

**実機確認** — 別ラズパイに frontend を立て、**カメラ MJPEG / WS のジョブログ / prompt 応答 / 成果物リンク / 完了音**が従来どおり動く。装置を動かすジョブ 1 本を通す。同居機で 8080=UI / 8081=API が共存する。2 タブで別マシンを同時表示。**PCB ファイル / USB は backend 機に挿す**（`GET /api/files` は backend のディスクを列挙する）。

______________________________________________________________________

### MR5 — `feat`: mDNS 広告 + 探索でマシン一覧を自動構成

frontend を立てるだけで LAN 上の機体が並ぶ。静的登録と併設。

**`src/web/api/discovery.py`（広告 + 共有定数）** — `SERVICE_TYPE = "_pcbasm._tcp.local."` / `API_VERSION` / TXT キー（`id` / `name` / `type` / `api`）、`ServiceAdvertiser`（`start` / `update` / `stop`）、純関数 `select_advertise_addresses(candidates)`、`local_ipv4_addresses()`（ifaddr の 3 行アダプタ）。

- `AsyncZeroconf(ip_version=V4Only)` + `async_register_service(allow_name_change=True)`。
- **`ServiceInfo(..., server=f"{machine_id}-pcbasm.local.")` を明示**して avahi 所有の `kurousagi.local.` を主張しない（省略すると instance 名で A レコードを publish し、avahi がホスト名を改名して SSH / Mainsail の `.local` 名が壊れる）。frontend は名前解決せず `addresses` から URL を組むのでこの名前は誰も引かない。
- 広告アドレスは ifaddr 列挙 → `select_advertise_addresses` で `127.` / `169.254.` を除外。`socket.getaddrinfo(gethostname())` は使わない（`/etc/hosts` の `127.0.1.1` を掴む）。
- `start()` 失敗（マルチキャスト不可）は WARNING ログのみでアプリを落とさない（装置操作は mDNS に依存しない）。
- `Settings` に `discovery_enabled` / `advertise_addresses` / `discovery_service_type` / `discovery_interfaces` を追加（すべて注入可能 = テストをループバックに閉じられる）。
- `PUT /api/settings/machine` で `machine_name` が変わったら `advertiser.update()` を呼ぶ（呼ばないとドロップダウンの表示名が最大 75 分古いまま残る）。
- 実機の avahi と python-zeroconf は `SO_REUSEADDR` で 5353 を共存できる（実測: bind 成功、`224.0.0.251` の join と受信も成功）。`/etc/avahi/services/` によるフォールバックは不要。

**`src/web/ui/discovery.py`（探索）** — `MachineDiscovery`（`AsyncServiceBrowser`）と純関数 `endpoint_from_service_info`。

- `Added` / `Updated` は同扱いで `asyncio.create_task(self._resolve(name))`。**タスク参照を `set` に保持し `add_done_callback(discard)`**（保持しないと GC されて探索結果が不定に欠ける）。
- `endpoint_from_service_info` は純関数（実 `ServiceInfo` を構築して単体テスト可 = モック不要）。TXT は bytes なので decode 失敗・キー欠損はフォールバック。
- **生存管理は mDNS の `Removed` に依存しない**（PTR は other-TTL 4500s で保持され、電源断では goodbye が飛ばないので最大 75 分残る）。`Removed` は候補集合の掃除としてのみ扱い、**online/offline 判定は入れない**（要件は一覧と切替のみ。到達不能マシンは一覧に残し、選ぶと 503 ページになる）。
- `MachineRegistry` にマージ規則を追加: 同一 `machine_id` なら static の host/port/name を優先し、`machine_type` / `addresses` は mDNS 側で欠損補完。
- `GET /api/machines`（frontend）は `registry.list()` の**同期ダンプ**（`{machine_id, label, name, hostname, address, port, machine_type, source, current}`）。

**依存** — `[project].dependencies` に `zeroconf` と `ifaddr`（推移依存だが直接 import する）。**ネットワークがある状態で `uv add` → `make format`（pre-commit の uv-lock hook）→ `make type` の順**に作る（CI は `uv sync --locked`）。

**テスト** — `select_advertise_addresses` の純関数契約、TXT ラウンドトリップ、**`server` が `{machine_id}-pcbasm.local.` である**（avahi ホスト名を主張しない回帰）、`endpoint_from_service_info` の欠損・非 UTF-8 耐性、マージ規則、**`label` が `"黒兎 (kurousagi: 192.168.100.201)"`**（表示形をピン）。`tests/helpers.py` に `skip_if_no_mdns`（`bind(("",5353))` + `IP_ADD_MEMBERSHIP` の実行時能力プローブ）を追加。`tests/e2e/test_discovery_e2e.py` は 1 プロセス内で実 zeroconf の advertise + browse（別 `AsyncZeroconf` = 別ソケット）。**service type をランダム化**し `interfaces=["127.0.0.1"]` でループバックに閉じる（実 LAN 汚染と CI 3 並列ジョブのクロストーク防止）。assert は「期待 machine_id が現れる」で、**総件数では assert しない**。

**実機確認** — `avahi-browse -rt _pcbasm._tcp` に自機が出る。**`journalctl -u avahi-daemon | grep -iE "conflict|withdraw"` が空 かつ `ping kurousagi.local` が引き続き解決する**。frontend 起動のみで全機体が並ぶ。同居機で自ホストの backend も見える。Wi-Fi 越しで探索できるか（AP のマルチキャスト抑制の確認）。

______________________________________________________________________

### MR6 — `feat`: 操作権リース + 閲覧モード

**`src/web/api/control.py`（FastAPI 非依存・clock 注入・タイマースレッド無し）**

`ClientIdentity`（`session_id` / `display_name` / `key = sha256(session_id)[:8]`。生 id は API・UI・ログに出さない）、`LeaseInfo`、`ControlDeniedError(RuntimeError)`（`BusyError` と同階層）、`ControlLease`（`snapshot` / `claim` / `takeover` / `release` / `connect` / `disconnect`）。

- **liveness は WS 在線**。`dict[str, int]` で接続数を数え、保持者の接続数 > 0 の間は無期限。0 になったら `disconnect_grace=30s` 後に失効。クライアント側 heartbeat は作らない。`tab.html:14-17` が全タブページで `job_console.js` を無条件ロードし `createBackoff(1000,15000)` で再接続するので、grace 30s はページ遷移と一時的な Wi-Fi 断を包含する。
- 失効は **lazy 判定**（バックグラウンドスレッドを持たない = 停止処理とテストが単純）: 切断猶予超過 OR（`idle_timeout=600s` 超過 かつ `busy()` が False）。`busy=lambda: state.busy_owner is not None` を注入して、machine_lock がジョブ全期間保持される事実を再利用し **長時間ジョブ中に失効しない**ことを JobManager 無改造で保証する。
- 全フィールドを 1 本の `threading.Lock` で保護（critical section は O(1) の in-memory 操作のみ）。**`on_change` はロック解放後に呼ぶ**（`JobManager._publish` → `loop.call_soon_threadsafe` に入る）。

**セッション同定** — `get_identity(request)` の解決順は `X-Pcbasm-Session` ヘッダ → cookie `pcbasm_session` → `"anonymous"`。表示名は `X-Pcbasm-Client-Name`（**`urllib.parse.quote` 済み**。HTTP/1.1 ヘッダは latin-1 なので日本語名を生で載せると uvicorn / httpx が壊れる）→ cookie `pcbasm_name` → `"名前未設定 (<key>)"`。unquote 失敗・非 ASCII 生バイトは例外にせずフォールバック。frontend は HTML ページレスポンスでのみ `pcbasm_session`（httpOnly, `token_urlsafe(16)`）を発行し、`ProxyApp` の**ヘッダ組み立て 1 箇所**で注入する。**cookie の `Path=/` 既定を上書きしない**（`/m/{id}/api/**` と WS ハンドシェイクと `img.src` に cookie が乗ることが、「ブラウザは独自ヘッダを付けられない」制約の唯一の抜け道）。**これは認証ではなく自己申告**（LAN 上の誰でも騙れる）。認証なしの前提では現状より悪化しないので受容する。

**ゲート適用** — 全 GET と MJPEG と WS 接続、`pad-config/route`・`fill-path`（POST だが読み取り専用計算）、**`emergency-stop` / `jobs/current/abort` / WS `abort`** は誰でも可。それ以外の変更系（`pcb-file` / `settings/machine` PUT / `machine-control` / **`firmware-restart`** / `jobs/*` の POST・PUT / `pad-config` の PATCH・import / `nozzle-cap/record` / WS `respond_prompt`・`command`）は `ControlDep` を 1 個足す。新規 `POST /api/control/{acquire,release,takeover,name}`（`takeover` は誰でも可 = 詰みからの脱出口）。

- **middleware は使わない**（パスパラメータで取りこぼす・OpenAPI に出ない・route シグネチャから読めない）。
- **認可チェックは必ず `Depends`（ハンドラ本体に入る前）で行う** — `klipper_errors_to_502()` は `RuntimeError` を 502 に変換するので、ハンドラ本体で `claim` を呼ぶとリース拒否が「Klipper 通信エラー 502」に化ける（`machine_control.py:36-38` が `BusyError` に対して同じ配慮をしている）。
- **WS の gate**: `routers/jobs.py:255` の `except (ValueError, KeyError)` に **`ControlDeniedError` を明示追加**（忘れると認可例外が `asyncio.wait` を抜けて WS が切断され、クライアントが再接続ループに入る。FastAPI の `exception_handler` は WebSocket に効かない）。error は既存経路（購読キューへ `{"type":"error"}`）でクライアント改修ゼロで toast が出る。`control.connect` は **`accept` 前**（既存の「accept 前に subscribe」と同じ位置）、`finally` で `disconnect`。
- `on_change` → `{"type":"control_changed","control":{…}}` を全 subscriber に**同一 payload で**ブロードキャストし、各クライアントが `/api/state` の `you.key` と比較して「自分か」を判定する。`job_console.js` の switch は default 無しで未知 type を無視するので後方互換。

**ジョブとの相互作用** — ジョブ実行中にリースが失効してもジョブは止めない。**prompt 応答権は「ジョブ起動者」ではなく「現在のリース保持者」**（誰も取らなければ待ち続け、abort は誰でも可なので deadlock にならない）。`pending_prompt` があるのに holder が null なら「操作権が空いています。取得して応答してください」を出す。force takeover は実行中ジョブに一切触らない（指示を出す権利の移転であって、走っているプロセスの移転ではない）。クールダウンは入れない（誤操作で握った人から取り戻せなくなる方が有害）。

**frontend UI** — `static/js/control.js` が `body.dataset.control = "held"|"viewer"|"free"|"unknown"` の 1 箇所で全体を切り替える。**初期値は `viewer`（fail-closed）**。SSR は backend へのサーバ間通信でブラウザの cookie を持たないため「自分が保持者か」を判定できない → 更新源は (a) ページロード後の proxy 経由 `GET /api/state`、(b) WS `control_changed`、(c) `api()` の 423。`app.js` を 3 行拡張して `err.status` / `err.data` を付け、423 のときだけ `window.webui.control?.onDenied(data)` を呼んでから throw する（既存 20 箇所は `err.message` しか使わないので無改変）。

無効化手段は **`inert` 属性 1 種類に統一**する（`.disabled` はジョブ状態を見て 4 モジュールが既に書いており、同じ属性を使うと「ジョブ終了時に閲覧者のボタンが復活する」二重管理バグになる。`inert` はキーボード操作・フォーカス・Enter 送信もまとめて止める）。`data-requires-control` を付ける対象はジョブフォーム / マシン操作パネル（開閉と幅変更は開放）/ ローディングのコマンド・適用（質量キャリブは純計算 GET なので開放）/ キャリブメニュー / pad 編集系（レイヤ切替・route 表示・export・viewer は開放）/ 設定フォーム / `#jc-apply` と prompt の入力（prompt 本文は閲覧者にも見せて「〈田中〉の応答待ち」）/ `#pcb-chip` / `#firmware-restart`。**`#estop` と `#jc-abort` は絶対に含めない**。

**`settings.html` の `{% block scripts %}` に `job_console.js` を追加**する（現在 `settings.js` だけを読むため `/settings` に WS が無く、リース状態が届かない）。これはリース UI の前提条件。

**テスト** — `test_control.py` は clock 注入で実時刻を待たない（claim / 他人 → `ControlDeniedError(holder)` / release の冪等 / takeover / WS 在線中は grace 超過でも失効しない / 同一セッション 2 接続のうち 1 本切れても失効しない / `busy=True` の間は idle 失効しない / `on_change` がロック保持中に呼ばれない / **実スレッド 8 本 + `Barrier` で同時 claim → 成功が厳密に 1 本**）。router 側は 2 つの `TestClient`（別 `X-Pcbasm-Session`）で 423 / takeover / ヘッダ無しが anonymous として claim できる / `quote("田中")` が復元される。「estop と abort は非保持者でも 200 / firmware-restart は 423」「**machine-control の 423 が 502 に化けない**」。WS は 2 本張り、A が保持中に B の `respond_prompt` が `{"type":"error"}` を返し**接続が切れない**（続けて別メッセージを送れる）、B の `abort` は通る、A の release 後に B が claim して pending prompt に応答してジョブが進む（詰み回避の直接検証）。`tests/e2e/test_multi_user_browser.py` は `browser_pages` の 2 context で、アサートを **(a) `body[data-control]`、(b) 対象の `inert` 属性、(c) クリックしてもサーバ側効果が無い の 3 点**で行う（Playwright の `is_enabled()` は `inert` を検出しない）。

**実機確認** — 2 端末で取得 / 奪取 / 解放。**閲覧者から緊急停止と abort が効く（安全確認）**。閲覧者は firmware-restart が押せない。操作者がタブを裏に回しても失効しない。ブラウザを閉じて 30s で空く。

______________________________________________________________________

### MR7 — `chore`: 運用（systemd 2 unit / Makefile 確定 / ドキュメント）

**`webui-service.sh` → `web-service.sh`（`git mv`）**。単一 unit（`pcbasm-webui.service` / `ExecStart=make webui`）を `pcbasm-api.service`（backend）と `pcbasm-ui.service`（frontend）の 2 本にする。

```
web-service.sh install|start|stop|restart|status|remove [api|ui|all]     # 対象の既定は api
```

- 現行の `SERVICE_NAME` / `UNIT_PATH` のグローバル定数と `install_service` / `remove_service` / `control_service` / `show_status` をターゲット引数で受ける形に変え、`all` は api → ui の順に同じ処理を回すだけにする（unit 生成のテンプレートは `Description` と `ExecStart` だけが違う）。
- **対象の既定は `api`** — 機体ごとに置く backend が最多数で、frontend は 1 台だけ明示的に `install ui`、同居機だけ `install all` を使う。
- **`install` は旧 `pcbasm-webui.service` を検出したら disable + 削除する**。MR7 で `Makefile` の `webui` エイリアスを消すため、放置すると `ExecStart=make webui` が解決できず restart ループに入る。
- **起動順の依存は付けない**（`After=pcbasm-api.service` を付けると同居機で backend の起動失敗が frontend を止める。frontend は backend が落ちていても起動でき 503 を返すだけ）。`After=avahi-daemon.service` も不要（python-zeroconf は avahi に依存しない）。
- `Makefile` の `webui` エイリアスを削除して `api` / `ui` に確定。`README.md:62-67` の `./webui-service.sh …` の 6 行も差し替える。

`README.md`: 2 プロセス構成、ポート、`config/machines.toml` の書式、2 unit のインストール、env 一覧、**「PCB ファイル / USB は backend 機に挿す」**、「探索できない環境では静的登録」、「1 ブラウザプロファイル = 1 人（共有キオスクでは分けられない）」、無認証 LAN 公開のリスク。`CLAUDE.md` の主要モジュール構成（`src/web/{api,ui}`）・make ターゲット・WebUI 設計節（「frontend の page ハンドラは backend の JSON を受けてテンプレに渡すだけ」「表示文字列の組み立てはサーバ側」）。`.claude/skills/`: `webui-e2e`（`live_ui` / `live_ui_two` / `browser_pages`、2 プロセス起動、`ASGITransport` が MJPEG / WS に使えない理由、mDNS テストの隔離の鉄則）、`testing-strategy`（`tests/web/{api,ui}/` の追加、`skip_if_no_mdns` は実行時能力プローブ、実 zeroconf は実オブジェクト検証）、`webui-thin-wrapper`。

**実機確認** — 既存インストール機で `./web-service.sh install all`（同居機）/ `install api`（機体）を実行し、旧 `pcbasm-webui.service` が消えて新 unit に置き換わる。再起動後に自動起動する。frontend が backend より先に上がっても復帰する。

## 検証

- 各 MR 末: `make format && make type && make test-no-hardware`（MR4 以降は `make test-e2e` も）。
- MR4 の合格条件: **既存 `test_pages.py` 61 件が本文無改変で通る**（fixture 差し替えのみ）= SSR 移設が透過であることの証明。
- MJPEG のカメラリーク回帰（プロキシ close → backend の `preview_clients` が 0 に戻る）と、WS 認可拒否で接続が切れない回帰は e2e で常時ピンする。
- `make test` / `@mark_hardware` は Claude は実行しない。実機確認は各 MR に列挙した項目をユーザーが行う。

## 既知の制約（実装しない・記録するだけ）

- MJPEG は視聴者ごとに renderer 構築と `imencode` が走るため、閲覧自由の帰結として Pi の負荷が視聴者数に線形。同一パラメータの視聴者でエンコード結果を共有する最適化は将来案。
- `selected_pcb` と `job_param_defaults` は「マシンのグローバル状態」のまま残す（1 台の機体を全員で共有するのが実態に合う）。他人の切替は `state_changed` で反映する。
- ジョブ履歴は「直近 1 件・ログ 500 行・非永続」のまま。後から接続した人は `GET /api/jobs/current` のスナップショットまで見える。
- `X-Pcbasm-Session` は自己申告であり認証ではない。backend API は無認証で LAN に並ぶ。
- `machine.toml` の設定スキーマ二重定義（コア層 attrs+cattrs / Web 層 `MACHINE_FIELDS` 手書きホワイトリスト）の解消は分離とは独立した別課題。
