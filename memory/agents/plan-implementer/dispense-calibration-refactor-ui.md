# 吐出量キャリブ 実行中パラメータ編集 UI（コミット5: フロント＋e2e）

承認済み計画: `/home/gop/.claude/plans/fluffy-sleeping-truffle.md`（能力1のフロント部分）。
前提コミット1〜4（runtime_editable 基盤・PUT /api/jobs/current/params・pasting フロー）は実装済み。
本タスクはコミット5のみ担当（基盤の src には触れていない）。

## 実装した内容

### テンプレ `src/webui/templates/pasting/dispense_calibration.html`
- runtime_editable な input に `{% if spec.runtime_editable %} data-runtime-editable="true"{% endif %}` を付与。
- scripts ブロックに `<script src="{{ static_asset('js/dispense_runtime_params.js') }}"></script>` 追加。

### 新 JS `src/webui/static/js/dispense_runtime_params.js`（薄ラッパー）
- `form#job-form.dispense-calibration-form` が無ければ no-op。`input[data-runtime-editable="true"]` を集める（無ければ return）。
- `window.webui.jobs.onUpdate` で `active = job != null && !TERMINAL.has(job.status) && job.accepts_commands` を更新。初期値 `currentJob()`。
- `input` イベントで name→value を `pending`（Map）に貯め、400ms debounce で **1 リクエストにまとめて** `PUT /api/jobs/current/params {values, persist:true}`。
- 送信時: `active` でなければ pending を捨てて送らない。空欄スキップ＋`Number.isFinite` のみ values へ。空なら送らない。
- 失敗時 `toast(err.message, false)`。成功時は表示なし（サーバが真実）。
- ドメイン検証（正値/整数/enum/下限）は一切持たない。calibration_menu.js / loading_controls.js の作法準拠。

### `src/webui/jobs/dev.py`（job_demo を e2e 題材化）
- params に `ParamSpec("live_value", "実行中編集値", "float", 1.0, runtime_editable=True)` 追加。
- number 応答 log 直後に `ctx.log(f"live_value = {ctx.params['live_value']}")` 追加。他挙動は不変。

### e2e `tests/e2e/test_webui_e2e.py`
- ヘルパ `_wait_first_prompt(ws)` 追加（最初の prompt / pending_prompt を待つ）。
- 新クラス `TestRuntimeParamUpdateOverWebSocket`（実 uvicorn + httpx + websockets）:
  - `test_live_update_applies_while_waiting_prompt`: prompt 待機中に PUT live_value=42 → 200 / `{"params":{"live_value":42.0}}`、`GET /jobs/current` の `params.live_value==42.0`（out-of-band 反映）。confirm/number 応答で succeeded、終端 summary の params にも 42.0。
  - `test_fixed_or_unknown_key_returns_400`: 実行中ジョブに steps(固定) / 未知キー PUT → 400。
  - `test_update_without_active_job_returns_400`: 無ジョブで PUT → 400。
  - `test_update_after_terminal_returns_400`: 終端後 PUT → 400。
- `TestDispenseCalibrationOverBrowser` に追記:
  - `test_menu_and_loading_controls_render` 末尾に `#param-line_length` の data-runtime-editable=="true" / `#param-board_width` は None を追加。
  - `test_runtime_params_script_is_loaded`（HTTP のみ）: ページに `js/dispense_runtime_params.js` が含まれる。

## 計画外の判断ログ
- 計画通り。逸脱なし。job summary の params 公開形は `job["params"]["<name>"]`（既存 test_jobs.py TestUpdateCurrentParams / test_start_fills_param_defaults_into_summary で確認済み）。
- アクティブジョブ無し/終端後は **400**（test_jobs.py の TestUpdateCurrentParams と同じ。manager.update_current_params が ValueError → router で 400）。計画書 (c) の「409 or 400」は実装上 400 で確定。
- 中止ボタンラベル表示の重複追加はしていない（TestPromptDialogOverBrowser::test_confirm_dialog_uses_custom_button_labels が job_demo confirm でカバー済みのため、計画書の注意通り）。

## 既知の制約・残課題（IF 変更通知なし）
- 公開 IF 変更なし（テンプレ data 属性・新 JS・dev.py の param 追加・e2e のみ）。他 implementer への影響なし。

## 検証結果
- make format: pass
- make type: pass（0 errors）
- make test-no-hardware: pass（1401 passed, 65 deselected）
- make test-e2e: 私の担当 `tests/e2e/test_webui_e2e.py` は **21 passed**。
  - **既存フレーク 1 件**: `tests/e2e/test_paste_solder_browser.py::TestPasteSolderBrowserPadInteraction::test_dispense_mode_and_height_controls_persist_after_reload`。
    私の変更を git stash で退避した状態でも失敗し、実行ごとにエラーが変わる
    （Internal Server Error → Locator.fill Timeout → Locator.wait_for Timeout）。
    私の触ったファイル（dispense_calibration / dev.py / 新 JS / test_webui_e2e.py）とは
    無関係。Playwright ブラウザ自動化のタイミング/リソース起因の既存不安定テストと判断。
    → ユーザー検証時の環境（CI/実機）で要確認。本タスクのスコープ外。
