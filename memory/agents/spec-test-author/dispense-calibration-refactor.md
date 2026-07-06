# 吐出量キャリブ リファクタリング — Commit 1（実行中パラメータ編集基盤）

承認済み計画: `/home/gop/.claude/plans/fluffy-sleeping-truffle.md`
担当範囲: `tests/` のみ（`src/` は並列の plan-implementer が担当）。本コミットは
catalog/manager/router の「汎用的な仕組み」に集中（dispense_calibration 固有の
removal_z_offset 追加・①②③ フローは Commit 2-4）。

## 書いたテスト一覧

### catalog（`tests/webui/jobs/test_catalog.py`）

`TestValidateRuntimeParams`（`validate_runtime_params` の patch セマンティクス）:
- test_returns_only_supplied_runtime_keys_without_default_filling — 正常系（patch: default 充填なし）
- test_empty_patch_returns_empty_dict — エッジ（空 patch は no-op = {}）
- test_multiple_runtime_keys_are_all_coerced — 正常系（複数 editable キー）
- test_int_param_accepts_integral_float — 正常系（int 規則 4.0→4）
- test_int_param_rejects_fractional_float — 異常系（4.5 は拒否）
- test_float_param_accepts_int — 正常系（int→float）
- test_fixed_param_is_rejected — 異常系（runtime_editable=False の board_width）
- test_unknown_key_is_rejected — 異常系（未知キー）
- test_type_mismatch_is_rejected — 異常系（"abc" を float へ）
- test_negative_removal_z_offset_is_rejected — 異常系（負 offset）
- test_zero_removal_z_offset_is_accepted — エッジ（既定 0.0 = 全退避）
- test_positive_removal_z_offset_is_accepted — 正常系

`TestRuntimeParamsProperty`（`JobDefinition.runtime_params`）:
- test_lists_only_runtime_editable_names — 正常系
- test_is_empty_when_no_runtime_editable_params — エッジ（空）

### manager（`tests/webui/jobs/test_manager.py`）

`TestUpdateCurrentParams`（`JobManager.update_current_params` + ライブ反映）:
- test_update_without_active_job_raises_value_error — 異常系（アクティブ無し）
- test_update_after_terminal_raises_value_error — 異常系（終端後）
- test_returns_validated_runtime_subset — 正常系（戻り値 = validated dict）
- test_fixed_param_update_raises_value_error — 異常系（board_width 固定）
- test_live_params_reflect_update_on_next_read — 正常系（ctx.params の次読込で新値）
- test_record_params_reflect_update_for_get_current — 正常系（record.params 追従）
- test_persist_true_saves_next_form_default — 正常系（persist=True で既定保存）
- test_persist_false_does_not_save_next_form_default — 正常系（persist 省略はライブのみ）

ライブ反映は「gate 前後で ctx.params["line_length"] を 2 回読み reads に記録する
合成ジョブ」で公開面から観測（private 直接参照なし）。1 回目 = 旧値、編集を挟んで
2 回目 = 新値、を `assert reads == [10.0, 20.0]` で固定。

### router（`tests/webui/routers/test_jobs.py`）

`TestUpdateCurrentParams`（`PUT /api/jobs/current/params`、body `{values, persist}`）:
- test_without_active_job_returns_400 — 異常系（アクティブ無し → 400）
- test_runtime_editable_update_returns_200_and_reflects_in_current — 正常系
  （200 + `{"params": {...}}`、GET /jobs/current が新値を映す）
- test_fixed_param_update_returns_400 — 異常系（board_width 固定 → 400）
- test_invalid_patch_returns_400[未知キー / 型不正] — 異常系（parametrize）
- test_negative_removal_z_offset_returns_400 — 異常系（負 offset → 400）
- test_persist_true_updates_next_form_default — 正常系（persist=True で既定保存）

router テストは既存の「app.state.catalog へ合成ジョブを register し、confirm prompt で
WAITING_INPUT に留める」手法を踏襲。`_register_runtime_editable` ヘルパで board_width
（固定）/ line_length / removal_z_offset（共に runtime_editable）を持つ hidden 合成
ジョブを登録（`uses_machine=False` で機械フロー非依存）。**実 dispense_calibration には
依存しない**（計画書の指針どおり：removal_z_offset 負値は自作 JobDefinition で検証）。

## 仕様根拠の対応表

| テスト | 計画書の根拠 |
|---|---|
| validate_runtime_params の patch（default 充填なし） | 「default 充填はしない（patch なので）」/「与えたキーだけ返る」 |
| 固定キー拒否 | 「runtime_editable=False のキー（固定パラメータ）を渡すと ValueError」 |
| 未知キー拒否 | 「未知キーを渡すと ValueError」 |
| 型不一致・int 規則 | 「型不一致は ValueError。int 系は float→int を .is_integer() 判定」 |
| 負 removal_z_offset 拒否 | 「removal_z_offset に負値を渡すと ValueError」/「負値はパラメータ検証で拒否」 |
| runtime_params property | 「JobDefinition.runtime_params（runtime_editable な name の集合）」 |
| update_current_params 無active ValueError | 「アクティブジョブが無ければ ValueError」 |
| 戻り値 = validated dict | 「runtime_editable な値を渡すと適用され、戻り値が validated dict」 |
| board_width 拒否 | 「runtime_editable=False（例 board_width）を渡すと ValueError」 |
| ctx.params ライブ反映 | 「実行中ジョブの ctx.params（ライブビュー）に新値が反映」/「毎アクセス再読込」 |
| record.params 反映 | 「JobRecord.update_params で GET /jobs/current が新値を映す」 |
| persist=True で既定保存 | 「persist=True で次回フォーム既定（save_job_param_defaults 相当）にも保存」 |
| PUT 200/400 | 「アクティブ無し 400 / 固定・未知・型不正・負 offset 400 / 正常 200」 |
| GET /jobs/current 反映 | 「GET /api/jobs/current が新値を映す」 |

## 検証結果（仕様 first なのに全緑＝実装が先行済み）

- `make format`: pass（ruff-format / docformatter が新規テストを整形済み）
- `make type`: pass（0 errors, 0 warnings）
- 新規テスト（catalog 14 + manager 8 + router 7 = 29 件）: **全 29 件 pass**
- 影響ファイル全体（test_catalog + test_manager + test_jobs）: **138 件 pass**

仕様 first を意図していたが、plan-implementer の Commit 1 実装（catalog の
`validate_runtime_params` / `runtime_editable` / `runtime_params`、manager の
`update_current_params` / `live_params` / `_JobRuntime` ライブストア、router の
`PUT /api/jobs/current/params`、`_coerce_param` 内 `removal_z_offset` 負値拒否）が
**既に完了していた**ため、書いた時点で赤にならず緑になった。公開 IF は契約と完全一致。

実装確認した要点:
- `src/webui/jobs/catalog.py`: `validate_runtime_params`（L140-）/ `runtime_params`
  property（L70-）/ `_coerce_param` の負 offset 拒否（`spec.name == "removal_z_offset"`
  キーで判定、L211-、**汎用＝合成 JobDefinition でも効く**）。
- `src/webui/jobs/manager.py`: `update_current_params` / `live_params` / `record.params`
  追従が実装済み（テストが緑なので公開挙動は契約どおり）。
- `src/webui/routers/jobs.py`: `PUT /jobs/current/params`（ValueError→400）実装済み。

## 期待される失敗

なし（Commit 1 スコープは実装完了済み）。Commit 2-4（dispense_calibration への
removal_z_offset 追加、`_removal_z`、`_CalibrationCancelled`、② タール、point-of-use
読み直し）は本コミットの範囲外で、別途 pasting テストとして書く想定。

## 実装側に求める修正

なし。Commit 1 の公開 IF は契約どおりで、テストはそのまま回帰ガードとして機能する。

## tests/helpers.py への追加

なし（3rd-party モック不要。合成 JobDefinition + 実 JobManager/JobCatalog/AppState/
TestClient で検証。fake は使っていない）。

## 落とし穴メモ（後続コミットのテスト作者向け）

- ライブ反映テストは「ジョブ関数内で 2 回 ctx.params を読み、結果を list に記録」する
  公開面観測に徹する（`_params` / `_params_lock` の private は触らない）。
- router の no-active は **400**（abort の 409 とは別）。manager が ValueError を投げ
  router が 400 化する経路。混同しない。
- removal_z_offset 負値拒否は `_coerce_param` が param 名で判定するため、実
  dispense_calibration を起動せずとも合成 JobDefinition で異常系を網羅できる
  （機械フロー / Klipper 不通に依存しない＝決定的）。
