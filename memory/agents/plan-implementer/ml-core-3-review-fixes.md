# コア ML 基盤 MR3: code-reviewer 差し戻しへの対応

レビュー本体: `memory/agents/code-reviewer/ml-core-3-model-evaluation.md`
裁定: `memory/agents/orchestrator/ml-core-3-model-evaluation.md`「code-reviewer の指摘への裁定」

## 対応内容

### must-fix

| # | 対応 |
| --- | --- |
| M1 | `_reliability_bins` を `valid_sample_mask() & (sample_weight > 0)` で先に絞り、bin 内の 3 統計を `_reliability_bin` で `sum(w*x)/sum(w)` に変更。bin の境界は `_bin_edges` の等件数のまま。`ReliabilityBin` の docstring も「件数ベース」→「帯は件数で切るが統計は重み付き」に更新 |
| M2 | `tests/ml/model/test_heads.py` の死んだ `torch.manual_seed(41)` を削除（乱数は `_regressor()` 内の `manual_seed(23)` が決める旨をコメント化）。200 → 300 step にし、判定を最終値ではなく history 中の最良値へ変更。閾値は `best_error < first_error * 0.1` |

### should-fix

| # | 対応 |
| --- | --- |
| S1 | `_weighted_percentile` の計算定義は変更なし。docstring に「重み = 複製回数と読み替えた場合とは一致しない」「一様重みで `torch.quantile` に一致する側を契約に採る」「重みが正の sample が 1 件以上あることを前提」を明記。非一様重みの値を `test_fixes_the_percentile_of_non_uniform_weights` で固定し、`test_does_not_match_replicating_samples_by_their_weight` で複製非等価も固定 |
| S2 | `GaussianPredictions` / `PaddedBatch`（`src/ml/data/batch.py`）/ `PreprocessedSample`（`src/ml/data/image.py`）を `@attrs.frozen(eq=False)` へ。field 側の `eq=False` は削除。identity 比較を `test_compares_by_identity` で固定 |
| S3 | `_numeric_buckets` が sample 0 件の区間を落とすように変更 |
| S4 | `_boundary_labels` を新設。`.4g` 固定をやめ、同一次元の境界が互いに区別できる最小桁数まで桁を増やす |
| S5 | `_UsableSamples` / `_usable_samples` を新設し、`gaussian_regression_metrics` と `fit_gaussian_log_variance_offset` の「validate → valid_sample_mask → 有効 0 件 → weight 合計」を 1 か所へ集約 |
| S6 | (a) `reliability_bin_count` 0 / -1 で bin 0 個、(b) `maximum_relative_difference` を実比較経路で 0.5（両側 0 の出力は 0.0）と固定、(c) `_DriftingModel` / `_FirstCallParameterModel` で `mismatched_gradient_parameters` / `missing_gradient_parameters` を実比較経路から非空にする、(d) `test_loss.py` の非有限 parametrize に `sample_weight` を追加 |
| S7 | compile 失敗の理由文言を「compile 済み model の実行に失敗しました（backend=...）」へ |
| S8 | `docs/image-based-dispense-calibration-ml-plan.md` §3 の「正規化誤差 e」を「相対誤差 `e = (mean - target) / target`」に改め、実装名 3 つを併記。primary gate の式は変更なし |
| S9 | `0.0 <= coverage <= 1.0` を、重み付き手計算値（RMSE `sqrt(7/5)`、coverage `4/5`）へ置換。時間計測テストは専用 model `_TimingModel` で cold compile を作り、「3 区間の合計が呼び出し全体の実測時間を超えない」ことと `compile_setup_seconds > compiled_seconds` を検証 |

### nit

- `blocks.py:90` を `nn.ReLU(inplace=True)` に揃えた
- `_as_output_tuple` → `_require_output_tuple`
- `_weighted_percentile` の `span <= 0` 分岐を削除（到達不能）
- `CompileOptions.validate()` を追加。backend / mode の空文字を拒否し、`compare_eager_and_compiled` が
    tolerances と同様に `ValueError` を投げる（呼ばないと死にコードになるため実際に呼んでいる）
- `padding_pixel` の docstring 根拠を「MR4 の部分 fine-tuning」→「勾配が流れていることを外から観測できるよう」へ。
    「読み取り専用」とは書いていない
- slice の reason を `f"{dimension}={value}: {reason}"` と slice 名付きにした

## 計画外の判断ログ

1. **`_reliability_bin` の重み付き平均を `torch.stack` の 1 回の除算でまとめた。**
    `regression._weighted_mean` は private で、cross-module import は pyright の
    `reportPrivateUsage`（warning）を出す。public 化は公開 API の追加になるため避け、
    3 統計をまとめて計算する形にした。定義が overall / slice と一致することは
    `test_uses_the_same_definitions_as_the_overall_and_slice_metrics` で固定してある
2. **`_boundary_labels` は while ループ。** `_bucket_edges` の境界が狭義単調増加なので
    float64 を丸めずに書ける 17 桁までに必ず終了する。上限を書いた for + fallback にすると
    到達不能な分岐が残るためループにした
3. **空 slice の reason 文言（nit）。** S3 で空 bucket を落とした結果、
    「予測が 1 件もありません」を返す slice はもう発生しない。文言そのものは
    top level（予測全体が空）では正しいので変えず、代わりに slice の reason を
    slice 名付きにして文脈が分かるようにした
4. **`CompileOptions.validate()` を実際に呼ぶようにした。** 呼ばなければ死にコードになる。
    backend 名が空という呼び出し側のバグは tolerances と同じく `ValueError`。
    存在しない backend 名は従来どおり理由文字列で返る（挙動変更なし）

## 他 implementer への IF 変更通知

- `GaussianPredictions` / `PaddedBatch` / `PreprocessedSample` の `==` は identity 比較になった
    （従来は「全フィールド eq=False」で中身の違う instance が等しくなる壊れ方をしていた）。
    3 class とも repo 内に等価比較の利用箇所はなし
- `CompileOptions.validate() -> str | None` が増え、`compare_eager_and_compiled` が
    空 backend / 空 mode で `ValueError` を投げるようになった
- `DiagnosticSlice.reason` の文字列が `"<次元>=<値>: <理由>"` 形式になった
- 数値次元の slice ラベルは `.4g` 固定ではなく、境界が区別できるまで桁を増やす

## 既知の制約・残課題

- 過学習テストの閾値 `0.1` は seed 6 通り（23 / 0 / 1 / 2 / 7 / 99）の実測で最悪比 0.029 だったことが根拠。
    余裕は 3.4 倍
- 時間計測テストは `_TimingModel` が他テストと compile cache を共有しないことに依存する。
    この class を他テストで使うと cold compile でなくなり、
    `compile_setup_seconds > compiled_seconds` の余裕が縮む
- MR5 への申し送り（`state_dict()` のキーが private 名を含む）は orchestrator のメモのまま未対応

## 検証結果

- `make format`: pass（2 回連続で実行し 2 回目は無変更）
- `make type`: pass（pyright 0 errors, 0 warnings）
- `make test-no-hardware`: pass（3125 passed / 140 deselected / 112.96s。対応前は 3107 passed）
- 実機テスト（`make test` / `pytest -m hardware`）は不実行（ユーザー実施）
