# MR7 トラック B（ドキュメント）

`chore/20260730/web-service-units`。MR1〜MR6 で実装された 2 プロセス構成・mDNS 探索・
操作権リースをドキュメントへ反映した。トラック A（`Makefile` / `web-service.sh` / テスト）とは
同一ツリーで並列作業したため、所有ファイル以外は触っていない。

## 簡素化した内部実装

なし（このトラックはドキュメント専任。`src/` `tests/` を編集していない）。

## 公開IF維持の確認

コード変更なし。ドキュメントに書いた事実はすべて実物を読んで確認した（下記「事実の出典」）。

## 同期したドキュメント

### README.md

`## WebUI` 節を全面改稿（旧 37 行 → 新 128 行）。

- 2 プロセス構成（backend `src/web/api/` = 機体ごと :8081 / frontend `src/web/ui/` = LAN に 1 台 :8080）
- `make api` / `api-dev` / `api-fake`（8099）/ `ui` / `ui-dev` / `ui-fake`（8098）。
    `webui` 系エイリアスの記述を削除（トラック A が Makefile から全廃）
- マシン登録: mDNS 探索と `config/machines.toml` の 2 経路。`[[machine]]` の
    `machine_id`（必須）/ `host`（必須）/ `port`（省略時 8081）/ `name` / `machine_type`。
    **探索できない環境では WARNING を出して静的登録だけで続行する**（起動は失敗しない）
- 「PCB ファイル / USB は backend 機に挿す」（ファイルブラウザは backend のローカル FS）
- systemd: `./web-service.sh install|start|stop|restart|status|remove [api|ui|all]`、
    既定 `api`、unit 名 `pcbasm-api.service` / `pcbasm-ui.service`、旧
    `pcbasm-webui.service` の自動削除、起動順依存なし
- 操作権: **緊急停止と abort は常に誰でも実行できる**／自己申告であって認証ではない／
    1 ブラウザプロファイル = 1 人（共有キオスクでは分けられない）／30 秒切断・10 分無操作で自動解放
- 無認証で LAN に公開するリスク（既定 `0.0.0.0`、ファイルブラウザから `/media` `/mnt`）
- env 一覧（`PCBASM_API_*` 7 個 + `PCBASM_CONFIG_DIR` / `PCBASM_UI_*` 7 個）

旧 `./webui-service.sh …` の 6 行（README.md:65-70）を新構文へ差し替え済み。

### CLAUDE.md

- 「プロジェクト概要」: `src/web/api/` のみの記述を `src/web/{api,ui}` の 2 プロセスに更新
- 「開発コマンド」: `webui` / `webui-dev` / `webui-fake` エイリアスの行を削除
- 「WebUI 設計」: 対象を「router / page ハンドラ / JS」の 3 層に広げ、Do を 2 行追加
    （frontend の page ハンドラは backend の JSON をテンプレへ渡すだけ／表示文字列の組み立てはサーバ側）

### .claude/skills/webui-e2e/SKILL.md

- 冒頭を 2 プロセス構成に。`live_server` / `live_ui` / `live_ui_two` を表で使い分け、
    `browser_pages` / `RunningServer.stop()` を追記
- 「変更系のブラウザ検証は先に操作権を取る」節を新設（fail-closed、`acquire_control(page)`、
    `session_headers(page)`、**Playwright の `is_enabled()` は `inert` を検出しない**）
- 「mDNS テストの隔離の鉄則」節を新設（service type ランダム化 + `interfaces=["127.0.0.1"]` +
    総件数で assert しない、広告側と探索側で別 `AsyncZeroconf`、他 fixture は `discovery_enabled=False`）
- 「in-process（`ASGITransport`）で書けないもの」節を新設（レスポンス全バッファで MJPEG が
    永久ハング／websocket scope 非対応／lifespan が走らない。**`@pytest.mark.timeout` は
    `TestClient` のブロッキング待ちを止めない**）
- `make ui-fake` を追記し、ページを見るには 2 つとも起動が要る点を明記
- 実在しないファイル参照 `tests/e2e/test_webui_e2e.py` を実在の 6 ファイルに修正

### .claude/skills/testing-strategy/SKILL.md

- テストレイアウトに `tests/web/ui/` を追加し、api / ui / e2e で検証範囲がどう変わるかを明記
- 「実 zeroconf（mDNS）は『実オブジェクト検証』」節を新設。**モック禁止則の例外ではなく
    優先順位 1〜2 の側**である旨と、`skip_if_no_mdns` が実行時能力プローブ
    （5353 の共有 bind + ループバックへの join を実際に試す）である旨
- 4 区分表の e2e 行を実態（WebUI の実サーバー通し、`make test-e2e`）に修正

### .claude/skills/webui-thin-wrapper/SKILL.md

- レイヤ責務に frontend page ハンドラを追加（`MachineClient` の pydantic モデルを渡すだけ）
- 「2 プロセスで増える線引き」節を新設（frontend は再計算しない／表示文字列はサーバ側／
    frontend が持ってよい知識の範囲／中継は素通し）
- 点検チェックリストに 2 項目追加、検証コマンドに `tests/web/ui/` と `make ui-fake` を反映

### 変更不要と判断したもの

- `data/config-templates/README.md` — `machines.toml` はテンプレート対象外（frontend は
    `config/` を読まないため機体設定テンプレートに含まれない）
- `gitlab-runner/README.md` — WebUI と無関係
- docstring — トラック B の所有範囲外（`src/` は触らない）。実装側の docstring は
    MR1〜MR6 で既に 2 プロセス前提になっていた

## 書いた事実の出典（すべて実物を確認）

| 記述 | 出典 |
| --- | --- |
| backend 8081 / frontend 8080、同居機の割り当て | `src/web/api/settings.py:44-46`、`src/web/ui/settings.py:31-32` |
| env 名の全量 | `Settings.from_env` の docstring（api / ui 両方） |
| `machines.toml` の書式・`port` 既定 8081・ファイル不在は空 | `src/web/ui/machines.py` の `load_machines_file` / `DEFAULT_BACKEND_PORT` |
| 静的登録と mDNS のマージ規則 | `src/web/ui/machines.py` の `_merge` / `_fill_gaps` |
| 探索失敗は WARNING で続行 | `src/web/ui/discovery.py` の `MachineDiscovery.start`（`OSError` / `ZeroconfError` を捕捉して return） |
| 生存判定なし・最大 75 分残る | `src/web/ui/discovery.py` モジュール docstring |
| `_pcbasm._tcp` | `src/web/api/discovery.py:26` `SERVICE_TYPE` |
| 緊急停止・abort は非ゲート | `src/web/api/routers/system.py:51-54`、`routers/jobs.py:198-206`・`_dispatch` の `case "abort"` |
| 自己申告であって認証ではない | `src/web/api/identity.py` モジュール docstring、`src/web/api/control.py` の `ClientIdentity` |
| 1 ブラウザプロファイル = 1 人 | `src/web/ui/pages.py:244-246`（`SESSION_COOKIE` を httponly cookie で 1 度だけ発行） |
| 30 秒 / 10 分の自動解放 | `src/web/api/control.py` の `disconnect_grace=30.0` / `idle_timeout=600.0`（`app.py:138` は既定のまま生成） |
| 閲覧許可は repo + `/media` + `/mnt` | `src/web/api/settings.py:33` `pcb_browse_allowed` |
| fake ポート 8099 / 8098 | `Makefile:54,72` |
| unit 名・`ExecStart`・旧 unit 削除・依存なし | `web-service.sh:7,21-22,89,126-136`（トラック A の成果物。読み取りのみ） |
| fixture 名と役割 | `tests/e2e/conftest.py`（`live_server` / `live_ui` / `live_ui_two` / `browser_pages` / `acquire_control` / `session_headers`） |
| `ASGITransport` の制約 | `tests/web/ui/conftest.py:8-16` |
| `mark.timeout` が `TestClient` を止めない | `tests/helpers.py:68`、`tests/web/ui/test_proxy_unreachable.py:68` |
| `is_enabled()` は `inert` を見ない | `tests/e2e/test_multi_user_browser.py:10,93-95` |
| mDNS 隔離の 3 原則 | `tests/e2e/test_discovery_e2e.py:7-15`、`tests/helpers.py` の `random_service_type` |
| `skip_if_no_mdns` は実行時プローブ | `tests/helpers.py:143-177`（`_probe_mdns` が実際に bind + join を試す） |

## 検証結果

- `uv run pre-commit run --files README.md CLAUDE.md .claude/skills/{webui-e2e,testing-strategy,webui-thin-wrapper}/SKILL.md`:
    pass（初回は mdformat が番号付きリストを `--number` で振り直したため Failed → 再実行で Passed）
- `uv run pytest tests/test_claude_hooks.py -m "not hardware" -q`: 28 passed
- ツリー全体のコマンド（`make format` / `make type` / `make test-no-hardware` / `make test-e2e`）は
    並列作業の規約により実行していない。合流検証は後段が行う
- コミットしていない

---

## レビュー指摘への追記（トラック B 第 2 巡）

`code-reviewer` の採用指摘 B1〜B9 に対応した。上記「同期したドキュメント」の内容は
そのまま有効で、以下はその上への差分。

### B1 `AGENTS.md` を `CLAUDE.md` に同期（must-fix）

`AGENTS.md` は Codex 側ミラーなので、同一コミットで揃える（MR3 = `e636725` と同じ扱い）。

- モジュール一覧: `src/web/api/` を「機体ごとの backend WebAPI（port 8081）」に直し、
    `src/web/ui/`（LAN に 1 つの UI frontend、port 8080）を追加。2 プロセスであることと
    `scripts/` の中身を 1 段落で補った
- **`src/scripts/`: 開発・運用スクリプト の行を削除**。`src/scripts/` は実在しない
    （`ls -d src/*/` → `src/pcbasm/` `src/web/` のみ）。元から誤りだったが、同じ箇条書きの
    差し替え対象だったのでこの機に正した
- 開発コマンド: **削除済みの `make webui` / `webui-dev` / `webui-fake` の行を削除**し、
    `make ui` / `ui-dev` / `ui-fake` を追加（`api` 系の説明もポート付きに揃えた）
- WebUI 設計: 対象を router / page ハンドラ / JS の 3 層に広げ、`CLAUDE.md` に足した 2 行
    （page ハンドラは JSON をテンプレへ渡すだけ／表示文字列はサーバー側）を反映

### B2 `CLAUDE.md` の参照先マップ（must-fix）

`webui-e2e` 行の `make test-e2e / webui-fake` を `make test-e2e / api-fake + ui-fake` に修正。
同一ファイルの「開発コマンド」節が既に `webui` 系を消していたので自己矛盾していた。

### B3 `.agents/skills/` の 3 本を同期（should-fix）

`migrate-claude` の手順に従い、`.claude/` 側を正として移植した（Codex 固有の情報は消さない）。

- `webui-e2e`: 2 プロセス構成・fixture 表・操作権・mDNS 隔離・`ASGITransport` 制約・
    `ui-fake` を反映。**実在しない `tests/e2e/test_webui_e2e.py` への追記案内を削除**し、
    実在ファイル名に差し替えた。Codex 固有として残したもの: 「WS イベントの形」節、
    `exec_command` PTY session による常駐起動、`pkill` self-kill の教訓（memory 参照）。
    Claude 固有の `run_in_background` / Playwright 前提の一部表現は Codex 表現に置換
- `testing-strategy`: `.claude/` 版（B6・B7 反映後）と同内容。Claude 固有記述が無いため全文同期
- `webui-thin-wrapper`: `.claude/` 版と同内容。参照先だけ `CLAUDE.md` →`AGENTS.md`、
    skill リンクを `../webui-e2e/SKILL.md` に直した

### B4 frontend と `config/` の関係（should-fix）

`README.md` の「frontend は `config/` を読まないので `PCBASM_CONFIG_DIR` を持たない」は誤り。
frontend が読むのは `config/machines.toml` **だけ**（`machine.toml` は読まない）で、
`PCBASM_CONFIG_DIR` ではその場所が動かない（既定は `PROJECT_ROOT / "config" / "machines.toml"`
固定）。**場所を変えるノブは `PCBASM_UI_MACHINES_FILE`** と明記した。

### B5 `machine_id` はホスト名に合わせる（should-fix）

backend の自己申告 ID は `resolve_machine_id` = `settings.hostname or socket.gethostname()` で、
`hostname` は `from_env` が読まないため本番では env で変えられない。`machines.toml` の
`machine_id` がホスト名とずれると `_merge` の重複排除（`machine_id` のみ）を通り抜け、
同じ backend が一覧に 2 件出る。TOML 例の直後に 1 段落で書いた。

### B6 `tests/` 直下の説明（should-fix）

「`__init__.py`、`helpers.py`、`conftest.py`、`test_package.py`、`e2e/` のみ」は実態と違う
（`test_claude_hooks.py` / `test_makefile_fake_targets.py` が既にあり、この MR で
`test_web_service_script.py` が 3 本目）。**「`src/` 側にミラー元が無い対象のテスト」**という
許容条件の形に書き換え、4 本の実例と各々の検証対象を添えた。

### B7 `before_deadline` 共有ヘルパ（nit）

- `webui-e2e`: `@pytest.mark.timeout` が効かない話の直後に、締め切りは
    `tests.helpers.before_deadline`（daemon スレッドで走らせて join）で包む旨を追記
- `testing-strategy`: `tests/helpers.py` の所在地の行に `before_deadline` / `wait_until` /
    `random_service_type` を列挙

### B8 backend の待ち受けアドレス（nit）

`PCBASM_API_HOST` は存在せず（`from_env` が読まない）常に `0.0.0.0`。env 一覧の backend 節の
直後に 1 段落で補い、絞りたい場合はファイアウォール/リバースプロキシと案内した。

### B9 `machines.toml` の読み込みタイミング（nit）

読み口は `create_app` 内の `load_machines_file` だけ、実行中の書き込み口は
`MachineRegistry.set_discovered`（mDNS）のみ。編集後は frontend の再起動が必要である旨と、
`setup-machine-config.sh` の `mv config config.bak.<ts>` で `machines.toml` も退避されて
静的登録が黙って 0 台になる点を「マシンの登録」節に書いた。

### 却下の記録

- **実装ノートのファイル名を `mr7-web-service-units.md` に統一する案 — 却下。**
    トラック別（`mr7-script.md` / `mr7-docs.md`）の方が読みやすい。MR6 でも同じ裁定をした。

### 追記した事実の出典（すべて実物で確認）

| 記述 | 確認 |
| --- | --- |
| `make webui` / `webui-dev` / `webui-fake` は削除済み | `make webui` → `No rule to make target 'webui'` / exit 2（3 つとも exit 2） |
| `src/scripts/` は存在しない | `ls -d src/*/` → `src/pcbasm/` `src/web/` |
| `make ui` / `ui-dev` / `ui-fake` は実在 | `grep -nE "^(ui|ui-dev|ui-fake):" Makefile` → 59 / 62 / 68 行 |
| backend に `PCBASM_API_HOST` は無い | `PCBASM_API_HOST=1.2.3.4` で `Settings.from_env().host` → `0.0.0.0` |
| `PCBASM_CONFIG_DIR` で machines.toml は動かない | `PCBASM_CONFIG_DIR=/tmp/altconfig` で `machines_file` → `<repo>/config/machines.toml` |
| `PCBASM_UI_MACHINES_FILE` が唯一のノブ | 同 env で `machines_file` → `/tmp/altconfig/machines.toml` |
| frontend は `machine.toml` を読まない | `src/web/ui/settings.py` の `Settings` docstring（`get_config_dir()` を呼ばない） |
| 自己申告 ID は hostname、env 不可 | `src/web/api/settings.py` の `hostname` フィールド（`from_env` の対応 env 一覧に無い）、`resolve_machine_id` |
| 重複排除は `machine_id` のみ | `src/web/ui/machines.py` の `_merge`（`static_ids` に無い discovered を追加） |
| machines.toml の読み口は起動時 1 回 | `grep -rn "load_machines_file\|set_discovered" src/web/ui/` → 読みは `app.py:96`（`create_app`）だけ、書きは `set_discovered` だけ |
| ファイル不在はエラーにならない | `load_machines_file`: `if not path.is_file(): return ()` |
| `mv config config.bak.<ts>` を案内している | `setup-machine-config.sh:175` |
| `tests/` 直下の実物 | `ls tests/*.py` → `__init__.py` `conftest.py` `helpers.py` `test_package.py` `test_claude_hooks.py` `test_makefile_fake_targets.py` `test_web_service_script.py` |
| `before_deadline` は共有ヘルパで 9 呼び出し | `tests/helpers.py:62` に定義、`grep -rn before_deadline tests/` → 定義 1 + import 2 + 呼び出し 8（うち `tests/web/ui` はローカル wrapper `_get_before_deadline` 経由） |
| `.agents/` 側が stale だった | `grep -rn "test_webui_e2e" .agents/skills/` → `webui-e2e/SKILL.md:43`（`.claude/` 側は修正済み） |

### 検証結果（第 2 巡）

- `uv run pre-commit run --files README.md CLAUDE.md AGENTS.md .claude/skills/{testing-strategy,webui-e2e,webui-thin-wrapper}/SKILL.md .agents/skills/{testing-strategy,webui-e2e,webui-thin-wrapper}/SKILL.md`:
    全 Passed（mdformat / codespell 含む。再実行不要）
- `uv run pytest tests/test_claude_hooks.py -m "not hardware" -q`: 28 passed
- `make migrate-codex-check`: `Codex rules are up to date`
- ツリー全体のコマンド（`make format` / `make type` / `make test-no-hardware` / `make test-e2e`）は
    並列作業の規約により実行していない。合流検証は後段が行う
- コミットしていない
