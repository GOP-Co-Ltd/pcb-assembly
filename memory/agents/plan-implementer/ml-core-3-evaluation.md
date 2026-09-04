# MR3 実装メモ: `src/ml/evaluation/` の regression と slices

担当範囲: `src/ml/evaluation/__init__.py` / `regression.py` / `slices.py` と
`tests/ml/evaluation/test_{regression,slices}.py`。
`ml/model/` と `compile_parity` は別 agent。`tests/ml/test_architecture.py` は未更新（別 agent）。

## 計画からの差分

1. **`GaussianPredictions.valid_sample_mask() -> Tensor` を public メソッドとして追加した。**
   計画の公開インターフェース案には無い。`slices._reliability_bins()` が
   「有効 sample だけを予測 standard deviation で並べる」ために同じ判定を要るが、
   `regression.py` の private helper を跨いで使うと `reportPrivateUsage` になる。
   判定規則（非有限 / `mean <= 0` / `target <= 0` / `weight < 0`）を 2 か所に写す方が
   drift しやすいので、契約として公開した。
2. **`ReliabilityBin` の集計は重みなし（件数ベース）。**
   計画は reliability bin の重み付けを規定していない。bin は件数で切るので、
   `mean_predicted_standard_deviation` / `observed_root_mean_squared_error` /
   `one_standard_deviation_coverage` も件数ベースにそろえた。
   重み付きにすると「bin 内の weight 合計が 0」で NaN になる分岐が要り、
   起こり得ないシナリオ向けの処理を増やすことになる。docstring に明記済み。
3. `NumericDimension.validate()` に `bucket_count >= 1` と値の有限性検査を足した。
   計画は「タプル長の不一致」しか挙げていないが、`torch.quantile` を通す前提として必要。
4. `build_diagnostic_report()` の `reliability_bin_count < 1` は `ValueError` にせず
   bin 0 個を返す。計画に規定が無く、例外にする根拠が無いため。

計画の他のシグネチャ（`GaussianPredictions` / `GaussianRegressionMetrics` /
`gaussian_regression_metrics` / `fit_gaussian_log_variance_offset` /
`CategoricalDimension` / `NumericDimension` / `SliceDimension` / `DiagnosticSlice` /
`ReliabilityBin` / `DiagnosticReport` / `build_diagnostic_report`）は案どおり。
orchestrator 裁定どおり `relative_error_*` の呼称を採用した。

## 実装上の決定

- **重み付き percentile。** 昇順の累積重み `C_i` に対し位置 `p_i = (C_i - w_i) / (W - w_i)`。
  `p_0 = 0`、`p_{n-1} = 1` で、`p_{i+1} - p_i = w_i * B_i + A_i * w_{i+1} >= 0`
  （`A_i` = 前方累積、`B_i` = 後方累積）なので単調非減少。
  重み 0 の sample は先に落とす（どの重み付き平均にも寄与しないため）。落とした後
  `n == 1` なら唯一の値を返し、`W - w_i == 0` の 0 除算を避ける。
  重み一様なら `p_i = i / (n - 1)` となり `torch.quantile(interpolation="linear")` と一致。
  これはテスト `test_uniform_weights_match_torch_quantile` で固定した。
- **無効 sample。** 例外にせず除外して `invalid_sample_count` に数える。
  有効 0 件、または有効分の weight 合計 0 のときだけ `(None, 理由)`。
- **数値次元の bucket。** `bucket_count + 1` 個の等間隔 percentile を `torch.quantile` で取り、
  縮退した境界を畳む。値が全て同じなら境界 1 個 → `[v,v]` の単一 bucket。
  ラベルは `.4g` 整形で `[lower,upper)`、最終だけ `[lower,upper]`。
- **reliability bin の境界。** `round(k * n / m)` を起点に、同じ standard deviation が
  続く間だけ前へずらしてから採用する。これで同値が 2 bin に分かれず、
  `upper[k] < lower[k+1]` が保証される（テストで固定）。

## docformatter の摩擦

- 説明部は 1 文ごとに空行で区切ってある。`make format` は 2 回連続で無変更を確認済み。
- docformatter は summary 先頭を大文字化する。`"""log 分散へ..."""` が
  `"""Log 分散へ..."""` に書き換えられたため、`"""Scalar offset を log 分散へ..."""` へ言い換えた。
  今後 docstring を小文字の識別子で始めない。

## 検証結果

- `make format` × 2: 2 回目は全 hook Passed（無変更）
- `make type`（pyright）: 0 errors, 0 warnings
- `uv run pytest tests/ml/evaluation -m "not hardware" -q`: 42 passed
- `ml.evaluation.{regression,slices}` を import しても
  hydra / mlflow / omegaconf / onnx / onnxscript / optuna は読まれない
  （`RUNTIME_MODULES` へ追加してよい）

## 引き継ぎ

- `tests/ml/test_architecture.py` の `RUNTIME_MODULES` へ
  `"ml.evaluation.regression"` と `"ml.evaluation.slices"` を追加すること（担当外のため未実施）。
- `src/ml/evaluation/__init__.py` の docstring は `compile_parity` を既に列挙している。
