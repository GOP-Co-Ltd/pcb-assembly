# spec-test-author ノート: paste-align-max-failures

計画書: memory/agents/implementation-planner/paste-align-max-failures.md（「tests/ 変更」節）

## 追加したテスト

- **tests/pcbasm/test_config.py** — `class TestPadAlignMaxFailures`
  - `test_defaults_to_zero_when_absent` — pad_align セクション無しで max_failures == 0（計画書「デフォルト 0」）
  - `test_reads_explicit_value` — tmp_path の toml に `[paste_dispenser.pad_align] max_failures = 2` → 2
  - `test_rejects_negative_value` — `PadAlign(max_failures=-1)` → ValueError（match="max_failures"）
- **tests/webui/test_config_store.py** — `class TestPadAlignMaxFailures`
  - `test_missing_max_failures_reads_as_none` — 欠落キー → None
  - `test_write_then_reread_reflects_value` — write 2 → reread 2
  - `test_write_zero_allows_no_failure` — write 0 → reread 0（境界: 0 は有効値）
  - `test_negative_max_failures_raises` — write -1 → UnknownFieldError
  - ※ 既存 `test_read_covers_every_whitelisted_key` が FieldSpec 追加を自動網羅（無修正）
- **tests/webui/routers/test_settings_api.py** — `class TestPadAlignMaxFailuresApi`
  - `test_get_reports_none_with_int_type_when_missing` — GET: value None かつ value_type == "int"
  - `test_put_writes_value` — PUT 2 → 200・レスポンスに反映
  - `test_put_negative_value_returns_400` — PUT -1 → 400
- **tests/webui/jobs/test_board_ops.py（新規）** — `class TestPadAlignAbortMessage`
  - `test_within_limit_returns_none`（parametrize: `([],0)`, `(["R1"],1)`, `(["R1","R2"],2)`）— 境界: 失敗数 == 許容数は許容
  - `test_none_max_failures_means_unlimited` — `(["R1","R2","R3"], None)` → None
  - `test_exceeding_limit_returns_message_with_counts_and_designator` — `(["R1"], 0)` → "失敗 1" / "許容 0" / "R1" を部分一致
  - `test_message_lists_every_failed_designator` — `(["R1","C3","U2"], 2)` → "失敗 3" / "許容 2" / 3 designator すべて部分一致
- **tests/e2e/test_webui_e2e.py** — `class TestPadAlignMaxFailuresOverRealHttp`
  - `test_put_max_failures_persists_and_reflects` — ホワイトリスト存在 → PUT 2 → GET 2 → 隔離 tmp の `configs/kurousagi/machine.toml` に `max_failures = 2`（TestAirPumpToggleOverRealHttp と同型）

## メッセージ検証の方針（仕様根拠）

計画書「公開 IF」節の指示どおり**完全一致は禁止・部分一致のみ**。
基準形 `（失敗 {n} / 許容 {m}）: {designators}` から "失敗 {n}" / "許容 {m}" / 各 designator の substring で検証。
文言微調整（句読点・末尾文）ではテストが壊れない。

## 実行結果（2026-07-10 時点）

plan-implementer の src/ 実装が本 worktree に既に入っており、**期待した赤ではなく全 green**:

- `pytest tests/pcbasm/test_config.py tests/webui/test_config_store.py tests/webui/routers/test_settings_api.py tests/webui/jobs/test_board_ops.py` → 123 passed（既存含む）
- `pytest tests/e2e/test_webui_e2e.py::TestPadAlignMaxFailuresOverRealHttp` → 1 passed
- `make format` 通過、`</content>` 混入なし（grep 確認済み）

## 実装側への要求

現時点でなし（契約どおりに実装済みと確認）。最終検証は `make format && make type && make test-no-hardware` → `make test-e2e`（`make test` は実行しない）。
