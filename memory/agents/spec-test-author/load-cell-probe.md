# load-cell-probe: テスト書き換えメモ（spec-test-author）

計画書: `/home/gop/.claude/plans/claude-probe-gnd-probe-load-cell-probe-zippy-mccarthy.md`
ブランチ: `feature/20260702/load-cell-probe`（コミットは主ループが実施）

## 変更ファイル

### 削除

- `tests/pcbasm/hal/test_probe.py` — ProbeSensor / ProbeGround / ServoGroundProbe 撤去に伴い削除
- `tests/pcbasm/hal/test_servo.py` — Servo 撤去に伴い削除

### 書き換え

- `tests/pcbasm/pasting/test_probe.py` — 新 ProbeExecutor 仕様に全面書き換え
  - `[load_cell_probe]` 欠如 → RuntimeError（substring `"[load_cell_probe]"` 検証）
  - `probe()` が PROBE 送信 → `last_z_result`（-2.0）を返す → `z + lift_height` へ退避
    （`stage.move(z=1.25)`）
  - `settle_time` の dwell（`G4 P500`）が PROBE の後に入る
  - `@mark_hardware` の `test_init`：実 Klipper の config に load_cell_probe があること
  - モックは自前 HAL（Klipper / XYZStage）のみ。`get_config` / `get_status` は
    Klipper 本体と `readonly` の両面に同じ Mock を張り、実装がどちら経由でも通る
- `tests/pcbasm/pasting/test_height.py` — `probe_shift` 撤去
  - `test_probe_shift_offsets_recorded_points` 削除
  - `test_probe_shift_applied_to_move_command` → `test_move_targets_match_recorded_points`
    （shift なしで「move 先 = 記録点」の契約として存続）
  - 残り 2 テストから `probe_shift` パラメータ除去
- `tests/pcbasm/test_config.py` — `TestProbe` から `servo_name` / `revolution_distance` /
  `down_distance` 引数と shift テスト 2 本を削除。バリデータテストは維持
- `tests/webui/test_config_store.py` — `probe.servo_name` / `probe.shift` の read 検証 →
  `probe.min_radius == 0.7` に置換。inline comment 保持テストは `probe.down_distance` →
  `probe.min_radius`。float_pair テスト 2 本削除（float_pair フィールドが消滅）
- `tests/webui/routers/test_settings_api.py` — `probe.shift` 検証削除。PUT は
  `probe.min_radius` / `probe.min_samples` に置換（kurousagi に既存行があり
  「4 行だけ変更」の契約を維持）
- `tests/webui/routers/test_pages.py`
  - settings ページの pair 入力（probe.shift）検証削除
  - `PASTING_JOB_FEATURES` から `probe_gnd_down_adjust` 削除
  - 新 `TestProbeGuidePage`: `/pasting/probe_guide` 200 + `LOAD_CELL_CALIBRATE` /
    `SAVE_CONFIG` / `klipper3d.org`、job-console/job-form 無し、サイドバー掲載、
    `probe_gnd_down_adjust` は sidebar から消え URL は 404
- `tests/webui/routers/test_jobs.py` — `test_probe_gnd_prompt_round_trip_...` 削除
- `tests/webui/jobs/test_pasting.py` — pasting 6 ジョブ化（catalog 14 件）、
  `TestProbeGndDownAdjust` クラス・hardware の down_adjust テスト・
  Apply ホワイトリストの `probe.down_distance` を削除。docstring 追従
- `tests/e2e/test_webui_e2e.py`
  - `test_probe_shift_two_fields_autosave` → `test_probe_lift_height_autosave`
    （設定 autosave の e2e カバレッジを float 単一フィールドで維持）
  - 新 `TestProbeGuideOverRealHttp`: probe_guide 200 + 較正手順表示、
    /pasting から probe_gnd_down_adjust 消滅 + URL 404

`tests/helpers.py` は変更不要（mark_hardware をそのまま利用）。

## 期待される pass/fail（2026-07-02 時点）

- `tests/pcbasm/pasting/test_probe.py` / `test_height.py` / `test_config.py`：
  **pass 確認済み**（implementer のコア移行が反映済み、45 passed）
- `tests/webui/test_config_store.py` / `test_settings_api.py` / `test_pages.py`：
  **pass 確認済み**（104 passed。probe_guide テンプレート実装済み）
- `tests/webui/jobs/test_pasting.py`（66 passed, 5 hardware deselected）/
  `tests/webui/routers/test_jobs.py`（35 passed）：**pass 確認済み**
- 未確認: `tests/e2e/test_webui_e2e.py`（make test-e2e は合流時に主ループが実行。
  py_compile は確認済み）、`@mark_hardware`（実機はユーザー実行）

## 実装側への要求

- 現時点でなし。仕様（計画書の公開 IF・ガイドページ内容・ジョブ撤去）と実装の
  齟齬は検出していない
- probe_guide の内容契約としてピンした substring: `LOAD_CELL_CALIBRATE` /
  `SAVE_CONFIG` / `klipper3d.org`（計画書「WebUI ガイドページ」節が根拠）。
  テンプレート文言を変える場合はこの 3 点だけは残すこと
