# Group B/C（pcbasm 移送 + webui バックエンド再編）後の簡素化パス

対象: `git diff fix/20260707/webui-flaky --name-only` の src/ 31 ファイル。
公開 IF（HTTP API / pcbasm `__all__` / JobManager 公開メソッド / JS 消費レスポンス）不変。

## 適用した簡素化（3 点、いずれも webui/jobs）

1. **`JobContext.__init__` の未使用 `params` 引数を削除**
   （`context.py` / `manager.py`）。受け取って即 `del` していた引数。
   JobContext はブリッジの `live_params()` を都度読む設計で、初期値の
   注入は manager が `JobRecord` へ行う。生成者は JobManager のみ
   （テスト含め直接コンストラクトなし）で、外部影響なし。
2. **`_run_paste_solder` の `enabled_designators` 中間 set を除去**
   （`jobs/pasting.py`）。`align_designators = set(enabled_designators)` の
   コピー 1 回だけに使われていたため、直接 set 内包に統合。
3. **`_calibrate_rotations_per_ul` の死んだローカル累積を除去**
   （`jobs/pasting.py`）。`rotations_per_ul` / `dispense_accel` ローカルは
   全 return 経路で直前の `computed_*` と同値（初期化値
   `results.* or calib.*` が返ることは構造上ない）。採用時に
   `adopted = attrs.evolve(results, ...)` を 1 回組んで直接返す形にし、
   初期化 10 行 + break 後の trailing return を削除。挙動・文言不変。

## 見送った点（変更しない判断）

- `board_ops.py` の抽出ヘルパ 2 本: `setup_board` は省略可能 2 引数、
  `align_component_groups` は `on_failure` フック 1 本のみで過剰
  パラメータ化なし。
- `pages.py` `_dispense_calibration_context` の未使用 `state` 引数:
  `_JOB_FEATURE_CONTEXT` dict ディスパッチの統一シグネチャに必要。
- `pasting_view.py` の公開名群: Group E のテスト再編が参照する前提
  （C-2 メモの申し送り）のため維持。
- `JobContext` の `machine_name`/`source_pcb`/`board_store` の
  Optional/デフォルト: 「未配線」セマンティクスを持つ契約で、
  絞り込みは外科的範囲を超えるため維持。
- `_run_paste_solder` で `resolve_pad_settings` が `select_enabled_pads`
  内部と二重に走る点: 解消には pcbasm 公開 IF の変更が要るため対象外。
- routers（common/dependencies/pasting 3 分割/system/machine_control）、
  catalog、manager 本体、pcbasm 側 8 ファイル: 残骸・不要 import・
  二重 docstring なし。`</content>` 混入も grep で 0 件。

## 検証

- `make format` / `make type`（0 errors）/ `make test-no-hardware`
  （1476 passed）/ `make test-e2e`（41 passed）全グリーン
- `make test`（hardware）は指示により未実行
