# MR6 トラック A（backend）実装メモ

対象: `feat/20260730/control-lease`。契約は `/tmp/pcbasm-plan/mr6-brief.md`（§1-§4, §6, §9, §10）。
トラック B（frontend）とは同一作業ツリーで並列実行したため、所有ファイル以外は一切触っていない。

## 完了状況

- `uv run pre-commit run --files <所有ファイル>`: 2 回目で全 Passed（ツリー全体の `make format` は並列作業のため実行せず）
- `uv run pyright src/web/api tests/web/api`: 0 errors
- `uv run pytest tests/web/api -m "not hardware"`: 743 passed
- mutation 11 種を影コピー（`PYTHONPATH=/tmp/mr6a-mut`）で確認し、全て検出（詳細は下記）

## 実装判断

### 1. `control.py` / `test_control.py` はコピーのみ

worktree から `cp` して最初に `pytest tests/web/api/test_control.py`（33 件）を通した。以後変更なし。
`_expire_stale` の「在線中でも無操作 600s + 非ジョブ中なら失効」という裁定もそのまま。

### 2. `busy` の注入は `AppState.busy_owner is not None`

「装置がジョブ実行中か」の判定に `jobs.current()` の非終端ではなく装置排他ロックの保持者を使った。
ロックはジョブ worker が起動から終了まで持つので prompt 待ち（`waiting_input`）も busy になり、
「長時間ジョブ中に無操作失効しない」という契約を満たす。HTTP 側の短時間ロック（machine-control 等）も
busy 扱いになるが、失効判定にしか使わないので無害。

### 3. 423 は `ControlDeniedError` の exception handler で組む（契約の鉄則）

- 認可は `ControlDep`（= `Depends(require_control)`）だけで行い、ハンドラ本体では claim しない。
  `require_control` は `ControlDeniedError` をそのまま投げ、`app.py` の `@app.exception_handler`
  が 423 + `{"detail", "holder"}` に変換する。
- `HTTPException(detail=...)` を使わなかった理由: body が `{"detail": ...}` 固定で、契約 §10 の
  `holder` を同階層に置けない。
- `holder` はハンドラ内で `lease.snapshot()` を読み直す（拒否 → 応答の間に保持者が変わる理論上の
  レースは、表示用情報なので許容）。
- FastAPI の例外ハンドラは依存解決中の例外にも効く（`ExceptionMiddleware` がルーティングを包む）。
  mutation (d) でこの経路が実際に効いていることを確認済み。

### 4. `ControlEventHub` を新設した（`routers/control_api.py`）

`control_changed` を「全 subscriber へ同一 payload」で配る必要があるが、`JobManager` には
自前イベントを流す公開 API が無く（`_publish` は private、`publish_state_changed` は
`state_changed` 固定）、**`src/web/api/jobs/manager.py` はトラック A の所有外**なので触らなかった。
代わりに WS ハンドラが `jobs.subscribe()` で作ったキューを hub にも登録し、hub が
`loop.call_soon_threadsafe` で投入する（送信は既存どおり `_send_loop` に一本化）。

→ **後段の簡素化候補**: `JobManager` に `publish(event)` を 3 行足せば `ControlEventHub` は削れる。
所有境界が解けたタイミングで統合トラックが判断すること。

### 5. WS error の key は既存どおり `detail`（契約 §10 の `message` と食い違う）

契約 §3 は「error は既存経路（購読キューへ `{"type":"error"}`）でクライアント改修ゼロ」と書き、
§10 のワイヤ表は `{"type":"error","message":...}` と書いている。既存の `_receive_loop` は
`{"type": "error", "detail": ...}` を積み、`job_console.js` と既存テストもそれを読むので、
**`detail` を維持した**（§3 の「クライアント改修ゼロ」を優先）。B / C に周知が必要。

### 6. `StateResponse.control` / `you` は既定値付き

`web/ui/machine_client.py` と `tests/web/ui/**`（トラック B 所有）が `StateResponse` を直接
parse するため、必須フィールドにすると並列作業中の相手のスタブ payload を壊す。既定値は
「未保持 + 空キー」= どの保持者キーとも一致しない fail-closed な値なので、SSR が誤って
「自分が保持者」と判断することはない。API が返す実値は常に埋まる。

### 7. ゲート対象の判断（契約 §3 からの具体化）

- `POST /api/pcb-file/upload` もゲートした（契約は「`pcb-file`」表記。選択状態を変える変更系で、
  PUT と同じ扱いにするのが自然）。
- `POST /api/jobs/{name}/param-defaults` / `POST /api/jobs/last/{apply,discard}` は
  「`jobs/*` の POST・PUT」に含めた。
- 開放のまま: 全 GET・MJPEG・WS 接続・`pad-config/route`・`fill-path`・`emergency-stop`・
  `jobs/current/abort`・WS `abort`・`POST /api/control/takeover`。
  `system.py` と `jobs.py` の該当ハンドラの docstring に「ControlDep を足さない」と明記した。
- ゲート専用で値を使わない引数は `_control: ControlDep` と命名（値を使う `files.py` は `control`）。

### 8. `POST /api/control/name` の意味

body を取らず、ヘッダ / cookie の表示名を正とする（proxy が毎リクエスト注入するため）。
保持者本人なら `claim` で表示名と無操作タイマーを更新し、非保持者なら `snapshot()` を返すだけ
（rename が奪取にならないように `info.key == identity.key` で分岐）。

### 9. テストは 2 クライアントではなくリクエストごとのヘッダで identity を分けた

契約 §6 は「2 つの `TestClient`」だが、同一 app に 2 つ目の `TestClient` を被せると lifespan が
二重に走り `bind_loop` の指す loop がずれる（WS のイベント配信が壊れる）。単一 `TestClient` +
per-request ヘッダで同じ検証ができるので、そちらを採用した。WS も
`websocket_connect(headers=...)` で 2 本張れる。

ゲート掃引（`GATED_REQUESTS`）は「ゲートが外れたら 423 以外になる」入力を選び、
外れても装置を触らない / ジョブが走り出さないものにした（未登録ジョブ名・空 values 等）。
待ちは全て締切付きポーリング（`_wait_status`）と受信駆動で、`pytest.mark.timeout` に頼っていない。

## mutation 結果（全て DETECTED）

| mutation | 検出したテスト |
| --- | --- |
| a: `firmware-restart` から `ControlDep` を外す | `TestGateCoverage::test_viewer_is_denied_with_423[post-/api/firmware-restart-None]`, `TestControlEndpoints::test_takeover_wins_against_the_holder` |
| b: WS の `except` から `ControlDeniedError` を外す | `TestWebSocketControl::test_viewer_respond_prompt_yields_error_without_closing` |
| c: `emergency-stop` に `ControlDep` を付ける | `TestGateCoverage::test_emergency_stop_works_for_a_viewer`, `test_viewer_is_never_locked_out[post-/api/emergency-stop-None]` |
| d: `machine-control` の claim を `Depends` からハンドラ本体（`klipper_errors_to_502` の内側）へ移す | `TestGateCoverage::test_denied_machine_control_is_423_not_502`（502 に化ける） |
| e: WS の `lease.connect` / `disconnect` を外す | `TestWebSocketControl::test_connection_counts_as_presence` |
| f: `hub.register` を外す（control_changed が届かない） | `TestWebSocketControl::test_control_changed_is_broadcast_to_every_subscriber` |
| g: 表示名の非 ASCII フォールバックを外す | `TestIdentityResolution::test_undecodable_name_falls_back_to_placeholder[raw-utf8-bytes]` |
| h: WS の `abort` を操作権でゲートする | `TestWebSocketControl::test_viewer_abort_over_ws_is_allowed` |
| i: `/api/state` の `control` を常に未保持で返す | `TestStateControlFields`, `TestWebSocketControl::test_connection_counts_as_presence` |
| j: 表示名の cookie フォールバックを外す | `TestIdentityResolution::test_cookies_are_used_when_headers_are_absent` |
| k: 423 body の `holder` キーを潰す | `TestControlEndpoints::test_acquire_by_second_client_returns_423_with_holder` |

影コピーは実行後に削除済み（`/tmp/mr6a-mut` は残っていない）。

### mutation が自分のテストの欠陥を先に見つけた（記録）

初回の mutation 実行で (f)（`hub.register` 除去）が**検出ではなくハング**した。
`WebSocketTestSession.receive_json` は届くまで無期限に待つため、「イベントを配らない退行」が
テスト失敗ではなく「終わらないテスト」になっていた（契約 §0 が禁じている状態）。
`_receive_type` を `_before_deadline`（daemon スレッド + `join(15s)` → `pytest.fail`、
`tests/e2e/test_proxy_e2e.py` と同じ形）で包んで修正し、再実行で全 11 種 DETECTED になった。
mutation スクリプト側にも 1 件 300s の subprocess timeout を入れ、ハングを SURVIVED 扱いで
報告するようにしている。

## 未着手・後段への申し送り

- トラック B の担当: `control.js` / `app.js` の 423 フック / `data-requires-control` / templates。
- トラック C の担当: `tests/e2e/test_multi_user_browser.py`、および既存 e2e が
  「ブラウザコンテキストごとに別セッション → 423」で落ちないかの確認。
- 実機確認チェックリスト（契約 §8）は MR 説明へ転記が必要。特に
  「操作者がタブを開いたまま 10 分放置（ジョブ非実行）で操作権が空く」は意図確認が必要な挙動。
