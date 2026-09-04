# MR5 dataset / toolhead offset — 簡素化ノート

レビュー（`memory/agents/code-reviewer/pasting-mr5-dataset-toolhead.md`、verdict approve）の should-fix と安全な nit に対応。
サブエージェントの permission 継承バグのため、orchestrator（メイン）が直列で適用した。公開 IF は追加のみで既存シグネチャ・on-disk 形状は不変。

## 対応した should-fix

1. `validate_view` を本番経路で使用: `PasteDatasetRecorder.__init__` が views を検証し、不正なら `ValueError`（呼び出し側 invariant）。テスト `test_rejects_invalid_view_before_recording`
2. `strict_applied_mode` のハードコードを `get_args(AppliedDispenseMode)` + `"mixed"` から導出
3. `ToolheadOffsetProcedure(..., settle_time=1.0)` を追加し `measure` の静定待ちを注入可能に（`DatasetCapturer.settle_time` と対称）。テスト fixture は `settle_time=0.0` で実 sleep を排除

## 対応した nit

- `ToolheadOffsetDiagnostics.from_outcomes(requested, failures, samples)` classmethod を追加し、web `jobs/pasting/toolhead_offset.py` の `_diagnostics` を削除
- `PixelRect` の重複定義を `pcbasm.vision.image.PixelRect` に一本化（`vision/crop.py`、`dataset/metadata.py`。`posctrl/copper.py` の定義は MR 範囲外で据え置き）
- テスト helper の `# type: ignore[arg-type]` を `AppliedDispenseMode` 型注釈で除去
- docstring の半角スペース混入 2 箇所
- `src/pcbasm/pasting/README.md` に dataset / toolhead offset を追記

## 見送った nit（理由）

- `_g1_moves` helper の `tests/helpers.FakeKlipper` への集約: MR4 も `tests/helpers.py` を触る可能性があり衝突を避けた。MR6 以降で `test_applicator.py` の同 helper と一緒に集約する
- `_calibration_result` / `_board_result` の共通化: 定数を helpers に露出させる必要があり結合が増える
- `PasteDatasetWriter.__init__` の公開 ctor: 計画どおり（IF 変更になる）
- web dataset の `initial_purge_ul` 検証順、`paste_height == "auto"` の assert、parse 厳格化: 挙動変更または情報のみ
- `docs/image-based-dispense-calibration*.md` の旧モジュール名: MR4 と同 docs を触るため MR6 で更新
