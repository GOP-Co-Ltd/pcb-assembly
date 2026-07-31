---
name: webui-e2e
description: WebUI（src/web/api/、FastAPI）を実サーバーでE2E検証・デバッグする手順。pytest E2Eスイート（make test-e2e）と手動・ブラウザ用の隔離起動（make api-fake）を扱う。WebUIのHTTP、WebSocket、MJPEGを通しで確認したいとき、E2Eをデバッグしたいときに使う。
---

# WebUI を E2E で検証・デバッグする

WebUI（`src/web/api/`、FastAPI + uvicorn）を実サーバーで通し検証する。fake カメラ・
テスト用 config ディレクトリ・隔離 data_dir を使い、実機もカメラも無くても HTTP / WebSocket /
MJPEG を最後まで叩ける。

関連: skill [testing-strategy](../testing-strategy/SKILL.md)。

## 大原則: サーバーの生存期間を有限コマンドに閉じ込める

**Codex が常駐 web サーバーを起動しようとすると kill されやすい。**

- foreground 起動（`make api` 等）は Bash の 120s タイムアウトで殺される
- detach が不十分な background プロセスはツール終了時のプロセス後始末で殺される
- sandbox 自体はループバック（127.0.0.1）のリッスン・到達を許可している（ネット制約が原因ではない）

→ **検証は常駐サーバーに依存しない形にする。** 第一選択は pytest E2E（下記 A）。
サーバーの起動〜停止を fixture（= pytest プロセス）内に閉じ込めるため、`make test-e2e`
という有限コマンドの中で起動 → 検証 → teardown が完結し、kill 問題が起きない。
ブラウザ体感が要るときだけ手動起動（下記 B）を使う。

## A. pytest E2E スイート（自動・Codex の第一選択）

実 uvicorn を `127.0.0.1:0`（エフェメラルポート）に daemon スレッドで立て、
httpx + websockets で実 HTTP/WS/MJPEG を叩く。配置は `tests/e2e/`（\[[testing-strategy]\] の e2e 区分）。

```bash
make test-e2e        # = uv run pytest -v -m e2e
```

- `tests/e2e/conftest.py` の `live_server` fixture が起動・停止を面倒見る。テストは
    `live_server.base_url` / `live_server.ws_url` を使うだけ
- `tests/e2e/` 配下は conftest の `pytest_collection_modifyitems` で自動的に `e2e`
    マーカーが付く（個別の付与不要）。`make test` / `make test-no-hardware` からは
    `-m "not e2e"` で除外済み（e2e は手動区分）
- fake カメラ + `data/testing/config` を tmp_path に複製して使うので、実機設定（`config/`）は
    汚さない
- 新しい機能の E2E を足すときは `tests/e2e/test_webui_e2e.py` に追記する。WS ジョブ通しの
    雛形は `TestJobLifecycleOverWebSocket`（hidden の `job_demo` を題材に
    起動 → log/progress/prompt 往復 → 完走 → 設定反映まで検証）

WS イベントの形（`src/web/api/routers/jobs.py` / `jobs/manager.py` が契約）:

- server → client: `job_status` / `log` / `progress` / `prompt` / `prompt_resolved` / `state_changed` / `error`
- client → server: `respond_prompt` {prompt_id, answer} / `command` {command} / `abort`
- `job_status` の `job` は全量サマリ。`pending_prompt` 経由でも prompt に応答できる

## B. 手動 / ブラウザ用の隔離起動

ブラウザで体感確認したい・curl で対話的に叩きたいときだけ使う。fake カメラ + 隔離
data_dir + 別ポート（既定 8099。実運用の 8080 と衝突しない）で起動する。

```bash
make api-fake      # PCBASM_API_FAKE_CAMERA=1, PORT=8099, DATA_DIR=/tmp/pcbasm-webui-fake
# 上書きしたいとき: PCBASM_API_PORT=9000 PCBASM_API_DATA_DIR=/tmp/foo make api-fake
```

### Codex が叩く場合

`make api-fake` は foreground の常駐 command。Codex は次の順で扱う:

1. **基本は A（pytest E2E）で済ませる。** ほとんどの検証は live_server fixture で足りる。
2. どうしても常駐が要る確認は `exec_command` の PTY session で起動し、別 command で
    readiness をポーリングする。確認後は session に interrupt を送り、終了を確認する。
    - 例: `curl --retry 30 --retry-delay 0 --retry-connrefused -s http://127.0.0.1:8099/api/state`
3. ブラウザでの体感確認はユーザーに依頼する（装置を動かすフローの実機確認と同様）。

`pkill` でサーバーを止めるときはパターンが自分のシェルに一致して self-kill しないよう注意
（例: `pkill -f "python -m web.api"` ではなく対象ポート/PID を指定する。memory `webui-implementation` の教訓）。

## 隔離の鉄則（実機設定を汚さない）

- 設定書き込みを伴う検証（apply / settings 保存）は **`data/testing/config` の複製**に対して行う。
    実機の `config/` は触らない
- pytest E2E は `data/testing/config` を tmp_path に複製するので自動的に隔離される
- 手動起動も `PCBASM_API_DATA_DIR` を tmp に向け、`webui_state.json` を実運用と分ける

## worktree で実行するときの注意

worktree 内で `uv run` すると `--system-site-packages` 無しの新しい `.venv` が作られ、
システム提供の `picamera2` が見えず `pcbasm.hal` の import で `ModuleNotFoundError` になる。
worktree では一度だけ venv を作り直す:

```bash
uv venv --clear --system-site-packages && uv sync --all-extras
```

## 関連設定

- `src/web/api/settings.py` — `PCBASM_API_FAKE_CAMERA` / `_PORT` / `_DATA_DIR` や
    `PCBASM_CONFIG_DIR`（pcbasm コア層と共通）等の env 上書き
- `src/web/api/fake_camera.py` — `FixedImageCamera`（固定画像を fps ペーシングで返す）
- `data/testing/webui/fake_camera.png` — fake カメラの既定画像
- `data/testing/config/` — 検証専用のマシン設定（Klipper port 7126 = 非リッスン）
