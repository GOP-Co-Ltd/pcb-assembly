# ノズルキャップ位置（nozzle cap parking）機能の仕様テスト

計画書: `memory/agents/implementation-planner/nozzle-cap-parking.md`（仕様の正）。
テスト一覧セクションのファイル・クラス・ケースをそのまま実装した。

## 書いたテスト一覧

### pcbasm（新規 12 件）

- `tests/pcbasm/test_gcode.py::TestMoveToCap`（3 件）
  - `test_sequence_is_g90_then_z0_then_xy_then_z_at_present_speed` — 正常系（厳密一致）
  - `test_sequence_contains_no_m400_or_m84` — 契約（M400/M84 非含有）
  - `test_velocity_override_changes_feedrate` — 正常系（velocity=30.0→F1800.0）
- `tests/pcbasm/test_config.py::TestMachineType`（4 件）
  - `test_reads_paste_from_config` / `test_reads_pnp_from_config` — 正常系
  - `test_missing_machine_type_raises_key_error` — 異常系（machine_minimal.toml 素材）
  - `test_unknown_machine_type_raises_value_error` — 異常系（fixture .replace() パターン）
- `tests/pcbasm/test_config.py::TestNozzleCap`（2 件）
  - `test_missing_section_returns_none` — エッジ（未記録が正常状態 → None 返却）
  - `test_reads_recorded_position` — 正常系
- `tests/pcbasm/test_parking.py::TestParkOrPresent`（新規ファイル、5 件）
  - `test_paste_with_cap_sends_park_sequence_in_single_send_gcode` — 正常系
    （send_gcode 1 回・G90→Z0→XY→Z→M400→M84・timeout 転送・fallback 不呼出・warn なし）
  - `test_paste_without_cap_warns_and_falls_back_to_present` — 異常系（warn に "nozzle_cap"）
  - `test_pnp_machine_falls_back_to_present_without_warning` — 正常系（pnp は警告なし）
  - `test_missing_machine_type_warns_and_falls_back_without_raising` — 異常系（warn に "machine_type"）
  - `test_invalid_machine_type_warns_and_falls_back_without_raising` — 異常系（同上）

### pcbasm（改修）

- `tests/pcbasm/posctrl/test_setup.py::TestMachineSession` — `machine_session(klipper, machine)` の
  2 引数化に追従。`mocker.patch("pcbasm.posctrl.setup.park_or_present")` で正常時・例外時とも
  `park_or_present(klipper, machine)` が 1 回呼ばれ例外は伝播することをピン（旧 PRESENT 断言 2 件を置換）。

### webui（新規 13 件）

- `tests/webui/test_config_store.py::TestNozzleCapFields`（2 件）— 欠落→None / write→reread round-trip
- `tests/webui/routers/test_machine_control.py::TestMoveToCap`（2 件）
  - 未記録→400（detail に「ノズルキャップ」）/ toml に [nozzle_cap] 追記後→502（バリデーション通過の証明）
- `tests/webui/routers/test_nozzle_cap.py`（新規ファイル）`::TestRecordNozzleCap`（3 件）
  - 7126 不達→502 / busy→409（owner in detail）
  - `@mark_hardware` `test_record_persists_position_and_stale_after_relax_returns_400`
    （home→record 200 {"x","y","z"}→実 configs/kurousagi/machine.toml に [nozzle_cap] 永続化→relax→record 400）
- `tests/webui/routers/test_pages.py::TestNozzleCapPage`（3 件）
  - サイドバーに nozzle_cap + ラベル「ノズルキャップ位置の設定」/ 記録ボタン + nozzle_cap.js あり・job-console/job-form なし / 「未記録」表示
- `tests/webui/routers/test_pages.py::TestMachineControlCapButton`（2 件）
  - paste で `mc-move-to-cap` 表示 / machine_type 行削除でも 500 にならずボタン非表示
- `tests/webui/jobs/test_manager.py::TestPresentOnTermination::test_recorded_cap_failure_logs_park_message_without_present`
  — cap 追記後の終了時退避失敗ログに「退避に失敗」を含み "PRESENT"/"M84" を含まない
- `tests/webui/jobs/test_machine_commands.py::TestHandleMachineCommand` に 2 件追加
  - move_to_cap 未記録→log（"キャップ" を含む）+True / 追記後 7126 不達→例外伝播で FAILED

### e2e（軽く 2 件）

- `tests/e2e/test_webui_e2e.py::TestNozzleCapOverRealHttp`
  - `/api/settings/machine` fields に `nozzle_cap.x` / `GET /pasting/nozzle_cap` 200 + 「記録」

## 仕様根拠の対応表

| テスト | 計画書の節 |
|---|---|
| TestMoveToCap | 「G-code 列（正確な合成）」「gcode.py」（`Z0.0`/`F1200.0` 書式、M400/M84 は後置合成） |
| TestMachineType / TestNozzleCap | 「公開インターフェース」config.py（必須キー KeyError/ValueError、nozzle_cap は None 返却） |
| TestParkOrPresent | 「parking.py」挙動①〜④ + 主要設計判断（クリーンアップ経路は例外にしない） |
| TestMachineSession | 「呼び出し 3 箇所の差し替え」（machine_session 2 引数化・破壊的変更） |
| TestNozzleCapFields | 「WebUI」config_store（MACHINE_FIELDS に nozzle_cap.x/y/z） |
| TestMoveToCap (router) | 「API 契約」move_to_cap 行（400 未記録 / 502） |
| TestRecordNozzleCap | 「API 契約」record 行 + 「nozzle_cap.py」手順 2（全軸ホーミング gating）・3（永続化） |
| TestNozzleCapPage / TestMachineControlCapButton | 「WebUI」pages.py / templates（非ジョブページ、paste 限定 #mc-move-to-cap、state.machine_type() は broad except→None） |
| test_manager 追加分 | 「呼び出し 3 箇所の差し替え」manager.py（catch-all 文言に PRESENT/M84 を入れない） |
| test_machine_commands 追加分 | 「WebUI」machine_commands.py（未記録は log のみ、送信失敗は伝播） |
| e2e 2 件 | 「テスト一覧」e2e 行 + 「API 契約」settings 行 |

## 期待される失敗 / 実行結果

- 記述時点では implementer 未合流で赤（import エラー）想定だったが、**確認実行時点で実装が合流済み**。
- `uv run pytest tests/ -q -m "not hardware and not e2e"` → **1500 passed**（回帰なし）
- `tests/e2e/test_webui_e2e.py::TestNozzleCapOverRealHttp` → 2 passed（実 uvicorn + test-fixture）
- `make format` → pass
- `@mark_hardware`（test_nozzle_cap.py の 1 件）は**未実行**（ユーザー実行。実機が動くため Claude は実行しない）

## 実装側に求める修正

なし（実装は全テストを満たしている）。以下は spec-test-author 側で固定した IF 解釈
（変更する場合はテストと同時に）:

- 未記録時の move_to_cap の 400 detail に「ノズルキャップ」、record の未ホーミング 400 detail に
  「ホーミング」、machine_commands の未記録 log に「キャップ」という substring を要求
- machine_session からの委譲は `park_or_present(klipper, machine)` の**位置引数**呼び出しでピン
- park_or_present の `timeout` は route ④ の send_gcode へ転送されることをピン
- ページ断言は `mc-move-to-cap`（計画書ピン）と `nozzle_cap.js` / 「記録」/ 「未記録」の substring
  （記録ボタンの DOM id はピンしていない）

## tests/helpers.py への追加

なし（既存の `mark_hardware` / `PROJECT_ROOT` / `TESTING_DATA_DIR` で足りた。
Klipper は自前 HAL クラスのため mocker.Mock、Machine は tmp_path 実 toml、
WebUI 系は test-fixture 7126 非リッスンポートへの実接続で 3rd-party モックなし）

## 注意点（合流後の確認者向け）

- `@mark_hardware` の record テストは**実 configs/kurousagi/machine.toml に [nozzle_cap] を書き込む**
  （計画書「kurousagi に [nozzle_cap] は追加しない（実機でユーザーが記録する）」と整合する意図的挙動。
  実行後に git diff へ現れる）
- webui 側の cap 記録シナリオは repo fixture を汚さず、各テスト内で tmp の
  `configs_root/kurousagi/machine.toml` へ `[nozzle_cap]` を追記する方式（conftest コピーの
  PRESENT/M84 ログ断言を壊さない）
- `TestPresentOnTermination` の既存 3 テストは cap 未記録フォールバック経路のまま green を確認済み
