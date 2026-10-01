---
name: webui-e2e
description: WebUI（src/web/api/ の backend と src/web/ui/ の frontend、FastAPI）を実サーバーで E2E 検証・デバッグする手順。pytest E2E スイート（make test-e2e）と手動/ブラウザ用の隔離起動（make api-fake / ui-fake）、ASGITransport で書けないもの、mDNS テストの隔離、および「Claude が常駐サーバーを起動すると kill される」問題の回避策。WebUI の HTTP/WS/MJPEG を通しで確認したいとき、E2E がデバッグできないとき、ブラウザで体感確認したいときに参照する。
---

# WebUI を E2E で検証・デバッグする

WebUI は **2 プロセス**（どちらも FastAPI + uvicorn）。

- **backend WebAPI**（`src/web/api/`、port 8081）：装置・カメラ・ジョブ・設定の所有者
- **UI frontend**（`src/web/ui/`、port 8080）：SSR ページと `/m/{machine_id}/api/**` の中継

fake カメラ・テスト用 config ディレクトリ・隔離 data_dir を使い、実機もカメラも無くても
HTTP / WebSocket / MJPEG を最後まで検証できる。

関連: skill [testing-strategy](../testing-strategy/SKILL.md)。E2E の検証は Claude 自身が最後まで行う（設定書き込みは `data/testing/config` の複製を使う）。

## 大原則: サーバーの生存期間を有限コマンドに閉じ込める

**Claude が常駐 web サーバーを起動しようとすると kill されやすい。**

- foreground 起動（`make api` / `make ui` 等）は Bash の 120s タイムアウトで強制終了される
- detach が不十分な background プロセスはツール終了時のプロセス後始末で強制終了される
- sandbox 自体はループバック（127.0.0.1）のリッスン・到達を許可している（ネット制約が原因ではない）

→ **検証は常駐サーバーに依存しない形にする。** 第一選択は pytest E2E（下記 A）。
fixture（= pytest プロセス）内でサーバーを起動・停止するため、`make test-e2e`
という有限コマンドの中で起動 → 検証 → teardown が完結し、kill 問題が起きない。
ブラウザ体感が要るときだけ手動起動（下記 B）を使う。

## A. pytest E2E スイート（自動・Claude の第一選択）

実 uvicorn を `127.0.0.1:0`（エフェメラルポート）に daemon スレッドで立て、
httpx + websockets + Playwright で実 HTTP/WS/MJPEG にアクセスする。配置は `tests/e2e/`（\[[testing-strategy]\] の e2e 区分）。

```bash
make test-e2e        # = uv run pytest -v -m e2e --timeout=180
```

- `tests/e2e/` 配下は conftest の `pytest_collection_modifyitems` で自動的に `e2e`
    マーカーが付く（個別の付与不要）。実ブラウザ fixture を使うテストには `browser` も付くので
    `-m "e2e and not browser"` で切り分けられる。`make test` / `make test-no-hardware` からは
    `-m "not e2e"` で除外済み（e2e は手動区分）
- fake カメラ + `data/testing/config` を tmp_path に複製して使うので、実機設定（`config/`）は
    変更しない
- 既存の題材: `tests/e2e/test_api_e2e.py`（backend 直の JSON/WS。WS ジョブ通しの雛形は
    `TestJobLifecycleOverWebSocket`）、`test_proxy_e2e.py`（中継）、`test_discovery_e2e.py`（mDNS）、
    `test_browser_ui.py` / `test_multi_user_browser.py` / `test_paste_solder_browser.py`（ブラウザ）

### fixture（`tests/e2e/conftest.py`）

| fixture       | 何を起動するか          | 使うとき                           |
| ------------- | ----------------------- | ---------------------------------- |
| `live_server` | backend 1 台のみ        | JSON API を backend 直で叩く       |
| `live_ui`     | backend 1 台 + frontend | **ページ・MJPEG・WS はここを通す** |

fixture に無い構成（backend 複数台、途中で停止する backend、検査用の上流）はテスト内で組む。
`start_app(app)` で実 uvicorn を起動し、settings は `make_api_settings` / `make_ui_settings` で作る
（例: `tests/e2e/test_proxy_e2e.py`）。`start_app` で起動したサーバーは try-finally で必ず `stop()` する。

- ページ取得とブラウザ操作を frontend 経由にそろえているので、既存 E2E を実行するたびに
    中継経路（`/m/{machine_id}/api/**`）も検証される。`live_server` を残すのは
    backend 直と中継経由を分けて「どちら側の回帰か」を切り分けるため
- `LiveUi.base_url` は `/m/{machine_id}` を含む。prefix 無しの URL や `/static` は `origin` を使う
- 1 ホストに複数 backend を立てるときは、machine_id を `make_api_settings(..., hostname=...)` で分ける
    （`socket.gethostname()` のままでは区別できない）
- `browser_pages` は独立した browser context の page を必要な数だけ作るファクトリ
    （複数端末の同時操作を書くときに使う）。1 枚で足りるなら `browser_page`
- `RunningServer.stop()` を明示的に呼べるので、「backend を落とすと frontend 経由の WS が
    閉じる」ようなサーバーを途中で停止する検証が書ける

### 変更系のブラウザ検証は先に操作権を取る

UI は fail-closed。ページを開いた直後は `body[data-control] = "viewer"` で、
`data-requires-control` の要素には `inert` が付いてクリックが届かない。
**変更操作をするブラウザ検証は先に `acquire_control(page)` を呼ぶ**（`tests/e2e/conftest.py`）。
テストの下準備で backend に直接リクエストすると `anonymous` がリースを取得するので、ブラウザ側は
閲覧者から始まるのが普通。backend への直接リクエストをブラウザと同じ操作権で送りたいときは
`session_headers(page)` で cookie をヘッダに載せ替える。

**Playwright の `is_enabled()` は `inert` を検出しない。** ゲートの検証は
`el.hasAttribute('inert')` を `evaluate` で読む（`tests/e2e/test_multi_user_browser.py` の
`_is_inert`）。

### WS イベントの形

契約は `src/web/api/routers/jobs.py` / `jobs/manager.py` で定義されている。

- server → client: `job_status` / `log` / `progress` / `prompt` / `prompt_resolved` / `state_changed` / `error`
- client → server: `respond_prompt` {prompt_id, answer} / `command` {command} / `abort`
- `job_status` の `job` は全量サマリ。`pending_prompt` 経由でも prompt に応答できる

### mDNS テストの隔離の鉄則

実 zeroconf を使うテストは、**実 LAN に影響を与えない**よう次の 3 つを守る（`tests/e2e/test_discovery_e2e.py`）。

1. **サービス型を毎回ランダム化する**（`tests.helpers.random_service_type()` →
    `_pcbasmt<hex>._tcp.local.`）。運用の `_pcbasm._tcp` を使うと同じ LAN の実機や CI の
    並列ジョブが混ざる
2. **`interfaces=["127.0.0.1"]` でループバックに閉じる**（広告アドレスも `127.0.0.1`）
3. **assert は「期待した machine_id が現れる」で書く。総件数では assert しない**
    （実 LAN の他の広告を拾い得るため）

- 広告側と探索側で**別の `AsyncZeroconf`** を使う（同一インスタンスだと自分の登録を
    キャッシュから読むだけでマルチキャスト経路を通らない）
- それ以外の fixture（`make_api_settings` / `make_ui_settings` / `tests/web/**` の conftest）は
    `discovery_enabled=False` にして mDNS の広告を出さない。**この既定を外さない**
- `skip_if_no_mdns` は 5353 の共有 bind とマルチキャスト join を実行時にプローブする
    （一部コンテナでは失敗ではなく skip になる）

## in-process（`ASGITransport`）で書けないもの

`tests/web/ui/` は frontend を `TestClient` で駆動し、上流の backend app を httpx の
`ASGITransport` で in-process に接続する。**in-process で書けるのは JSON プロキシと SSR ページだけ。**

- `ASGITransport` は**レスポンスを最後までバッファしてから返す**ので、MJPEG
    （終端しない multipart）を読むと**永久にハングする**
- `ASGITransport` は **websocket scope を扱えない**ので WS 中継も検証できない
- backend 側の lifespan も走らない（`JobManager.bind_loop` が呼ばれないためジョブ実行と
    WS イベント配信が成立しない）

→ MJPEG / WS / ジョブ実行は `tests/e2e` で実 uvicorn（実ソケット経由）で検証する。

**`@pytest.mark.timeout` は `TestClient` のブロッキング待ちを中断できない。** この場合、テストは
失敗せず、終わらなくなる。in-process テストで待ちが発生し得る箇所は、
timeout マーカーに頼らず **httpx 側の timeout**（`read=` など）を持たせて失敗に変える。
スレッドの待ちを含む E2E シナリオも同じ理由で、シナリオ自身に締め切りを持たせる
（`test_discovery_e2e.py` の `_SCENARIO_TIMEOUT`）。

**締め切りは `tests.helpers.before_deadline` を使う**（daemon スレッドで呼び出しを走らせて
join し、期限内に返らなければ fail させる共有ヘルパ）。WS receive / ストリーム読み / 到達
不能な上流への GET などブロッキングし得る呼び出しはこれで包む。各テストで書き直さない。

## B. 手動 / ブラウザ用の隔離起動

ブラウザで体感確認したい・curl で対話的にリクエストを送りたいときだけ使う。fake カメラ + 隔離
data_dir + 実運用と衝突しない別ポートで起動する。

```bash
make api-fake  # PCBASM_API_FAKE_CAMERA=1, PORT=8099, DATA_DIR=/tmp/pcbasm-webui-fake
make ui-fake   # PORT=8098。api-fake（8099）を静的登録した machines.toml を生成して向ける
# 上書きしたいとき: PCBASM_API_PORT=9000 PCBASM_API_DATA_DIR=/tmp/foo make api-fake
```

ページを見るには **2 つとも起動する**（`ui-fake` だけでは上流の backend が無い）。JSON API の
一発確認なら `api-fake` 単体で足りる。

### Claude が叩く場合（kill 回避の実務）

`make api-fake` / `make ui-fake` は foreground なので Claude が直接呼ぶと 120s で強制終了される。Claude は次のいずれかで対処する。

1. **基本は A（pytest E2E）で済ませる。** ほとんどの検証は `live_ui` fixture で足りる。
2. どうしても常駐が要る一発確認は、`run_in_background: true` の Bash で起動し、別の Bash で
    readiness をポーリングしてからリクエストを送る（foreground の `sleep` は禁止なので `curl --retry ... --retry-connrefused` で待つ）。確認後は起動した background プロセスを止める。
    - 例: `curl --retry 30 --retry-delay 0 --retry-connrefused -s http://127.0.0.1:8099/api/state`
3. ブラウザでの体感確認はユーザーに依頼する（装置を動かすフローの実機確認と同様）。

`pkill` でサーバーを止めるときはパターンが自分のシェルに一致して self-kill しないよう注意する
（例: `pkill -f "python -m web.api"` ではなく対象ポート/PID を指定する）。

## 隔離の鉄則（実機設定を汚さない）

- 設定書き込みを伴う検証（apply / settings 保存）は **`data/testing/config` の複製**に対して行う。
    実機の `config/` は触らない
- pytest E2E は `data/testing/config` を tmp_path に複製するので自動的に隔離される
- 手動起動も `PCBASM_API_DATA_DIR` を tmp に向け、`webui_state.json` を実運用と分ける
- frontend の `machines_file` は tmp の**不在パス**を渡す。既定のままだとリポジトリの
    `config/machines.toml` を読み込み、実機の登録が混ざる

## worktree で実行するときの注意

worktree 内で `uv run` すると `--system-site-packages` 無しの新しい `.venv` が作られ、
システム提供の `picamera2` が見えず `pcbasm.hal` の import で `ModuleNotFoundError` になる。
worktree では次のコマンドで一度だけ venv を作り直す。

```bash
uv venv --clear --system-site-packages && uv sync --all-extras
```

## 関連設定

- `src/web/api/settings.py`：`PCBASM_API_FAKE_CAMERA` / `_PORT` / `_DATA_DIR` /
    `_DISCOVERY_ENABLED` や `PCBASM_CONFIG_DIR`（pcbasm コア層と共通）等の env 上書き
- `src/web/ui/settings.py`：`PCBASM_UI_PORT` / `_MACHINES_FILE` / `_DISCOVERY_ENABLED` /
    各種 timeout。frontend は `config_dir` を持たない
- `src/web/api/fake_camera.py`：`FixedImageCamera`（固定画像を fps ペーシングで返す）
- `data/testing/webui/fake_camera.png`：fake カメラの既定画像
- `data/testing/config/`：検証専用のマシン設定（Klipper port 7126 = 非リッスン）
