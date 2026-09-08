# 点塗布・多視点への `src/ml/` 適応 — 仕様テスト

計画書: `memory/agents/implementation-planner/ml-point-dispense-model.md`（T1〜T38 の対応表が正典）。

**現況: `tests/ml` は 1264 passed / 1 skipped / 0 failed。pyright 0 errors。**
ベースライン（main）は 1160 passed / 1 skipped なので、正味 +104 本。
docformatter / ruff / ruff-format はいずれも Passed。

途中、`GaussianRegressionTask.reduce` の飽和診断（S3）を先に赤で置いて実装側へ渡した。
実装が入って緑になっている。

## 書いたファイル

| ファイル | 種別 | 内容 |
| --- | --- | --- |
| `tests/ml/data/test_image.py` | 変更 | サイズ契約の新既定値、`validate_augmentation` / `validate_for_encoder`、`PreprocessedMultiViewSample` |
| `tests/ml/data/test_batch.py` | 変更 | stride 8、bucket 鍵、view 数コスト、`MultiViewPaddedBatch`、`ViewDropout` |
| `tests/ml/model/test_multiview.py` | 新規 | 多視点 encoder / regressor / ONNX export |
| `tests/ml/model/test_heads.py` | 変更 | ReLU の厳密 0 と勾配 0、`mean_bias_initial` |
| `tests/ml/evaluation/test_regression.py` | 変更 | `ZeroTargetMetrics` / `MeanSaturationDiagnostic` |
| `tests/ml/evaluation/test_slices.py` | 変更 | `DiagnosticReport.zero_target` / `mean_saturation`、`capture_order` |
| `tests/ml/export/test_runtime.py` | 変更 | `predict` の入力名・要素型・軸数検査 |
| `tests/ml/export/test_benchmark.py` | 変更 | 同上の副作用（orchestrator が編集許可） |
| `tests/ml/training/test_task.py` | 変更 | `reduce` の飽和診断（orchestrator の裁定） |
| `tests/ml/test_architecture.py` | 変更 | `ml.model.multiview` を RUNTIME 層へ登録 |

`tests/ml/helpers.py` と `tests/ml/support.py` は変更不要だった。`src/**` は一切触っていない。

## T1〜T38 の対応表

「どう壊すと落ちるか」は変異を想定した検出根拠。変異実験そのものは合流後の別フェーズ。

| # | テスト | どう壊すと落ちるか | 観測点が決定的である理由 |
| --- | --- | --- | --- |
| T1 | `test_covers_the_point_dispense_crop_range_without_clipping[53-0.5]`、`TestPreprocessedSamplePreprocess` | `minimum_size` を 32 へ戻す | 出力 shape の厳密一致 `ImageShape(26, 26)` |
| T2 | 同 `[53-2.0]` / `[159-1.0]` / `[27-1.0]` | `maximum_size` を 159 未満へ、`maximum_pixels` を 106² 未満へ | 同上（4 ケースとも厳密一致） |
| T3 | `test_rejects_inconsistent_constraints[minimum_size=0]` | `validate` の 1 分岐目 | 理由文の**完全一致**（`== expected`） |
| T4 | 同 `[maximum_size=8]` | 2 分岐目 | 同上。`maximum_pixels` 分岐へ落ちない値を選定 |
| T5 | 同 `[maximum_pixels=100]` | 3 分岐目 | 同上 |
| T6 | 同 `[stride=0]` | 4 分岐目 | 同上 |
| T7 | `test_rejects_a_range_that_shrinks_the_smallest_source_below_the_minimum` | 合成ガードの削除（E6） | 理由文に `13` と `16` |
| T8 | `test_accepts_a_range_that_keeps_...` + `test_the_boundary_is_the_floor_of_the_scaled_size[32/31]` | 常に理由を返す変異、境界の off-by-one | 32→通る / 31→落ちる の対で境界を挟む |
| T9 | `TestImageConstraintsValidateForEncoder` 2 本 | 検査の削除、不等号の向き | 理由文に `32` と `16`。`total_stride == minimum_size` の境界も別ケース |
| T10 | `test_applies_the_same_geometric_transform_to_every_view` / `test_the_shared_valid_mask_matches_the_single_view_preprocessing` | view ごとに別 parameter を引く | 45 度回転下で `torch.equal` の厳密一致 |
| T11 | `test_standardizes_across_the_views_with_one_set_of_statistics` | view 別標準化 | view 別なら差は厳密に 0。閾値 0.5 を挟んで対立仮説と分離 |
| T12 | `test_rejects_views_whose_sizes_disagree` | 検査の削除 | 理由文に両方の shape 文字列 |
| T13 | `test_rejects_an_empty_view_sequence` / `test_rejects_a_view_without_any_image` / `test_rejects_views_with_different_image_counts` | 同上 | 理由文の識別部（`1 個以上` / `画像` / `枚数`） |
| T14 | `test_separates_mixed_crop_sizes` | 面積項の削除（E4 で空洞化） | `len(plan) == 3` の**厳密一致** + batch 内均一 |
| T15 | `test_separates_different_view_counts` | `view_count` を bucket 鍵から外す | `len(plan) == 2` の厳密一致 |
| T16 | `test_the_pixel_budget_counts_every_view[1/5]` | コストから `view_count` を落とす | V=1 で 2 batch / V=5 で 10 batch の厳密一致 |
| T17 | `test_produces_a_batch_view_channel_height_width_tensor` | rank の取り違え | `tuple(shape)` 完全一致 |
| T18 | `test_rejects_a_batch_whose_view_counts_disagree` | 検査の削除 | 例外型 + `view 数` |
| T19 | `test_pads_a_uniform_crop_to_the_stride_and_leaves_padding_in_the_mask` / `test_the_default_stride_matches_the_image_constraints` | stride 既定の巻き戻し（E3） | shape `(…, 56, 56)` と有効画素の**厳密な枚数** |
| T20 | `test_is_invariant_to_the_order_of_the_views` | 平均を先頭 view / 重み付き和へ | 差の最大値に固定 tolerance 1e-5 |
| T21 | `test_repeating_one_view_matches_the_single_view_result` | 平均を総和へ | 総和なら 5 倍ずれる |
| T22 | `test_rejects_images_without_a_view_axis` / `test_rejects_a_mask_without_a_view_axis` | 入口検査の削除 | 理由文が multiview のもの（`[B, V, C, H, W]`） |
| T22' | `test_the_shared_encoder_reports_a_channel_count_mismatch` 他 2 本 | multiview 側へ検査を重ねる退行 | **`ImageEncoder` の理由文**を期待。前段が後段を隠す構造を作らせない |
| T23 | `test_the_learnable_padding_pixel_receives_a_gradient` | learnable padding の配線切れ | `.grad` が None でなく非零 |
| T24 | `test_mean_is_exactly_zero_when_the_pre_activation_is_negative` | Softplus への巻き戻し | `torch.equal(mean, zeros)` の厳密比較 |
| T24' | `test_a_saturated_sample_has_exactly_zero_gradient_to_its_features` | 同上 | 飽和 sample の勾配が厳密 0、非飽和が非零（片方だけでは一律潰し変異と区別できない） |
| T25 | **書けなかった。** 下記「テスト化できなかった行」参照 | — | — |
| T26 | `test_log_variance_clamps_exactly_onto_the_configured_bounds` | 巻き添えで clamp が緩む | 上下限との厳密一致（`== -3.0 or == 2.0`） |
| T27 | `TestMeanSaturationDiagnostic::test_counts_the_two_populations_separately` | 真値による切り分けの削除 | 6 件の既知構成で全 field 厳密一致 |
| T28 | 同上 + `test_a_blank_only_population_is_not_reported_as_a_positive_problem` | 2 母集団の合算 | `saturated_zero_count` と `saturated_positive_count` を別々に固定 |
| T29 | `test_a_population_without_any_zero_target_does_not_divide_by_zero` | 分母 guard の削除 | 例外にならず 0.0 |
| T30 | `test_selects_only_the_samples_whose_target_is_zero` | 母集団の取り違え | `sample_count == 3` の厳密一致 |
| T31 | `test_measures_the_absolute_error_without_dividing_by_the_target` | 既存 metric の流用 | 全 field 有限 + 4 値の厳密な期待値（p95 = 2.9 等） |
| T32 | `test_reports_no_zero_target_metrics_when_every_target_is_positive` | 空集団で誤った metric を作る | `zero_target is None` |
| T33 | `test_the_overall_metrics_still_exclude_every_blank` | `valid_sample_mask` の契約変更 | `invalid_sample_count == 2` が blank 件数と一致 |
| T34 | `TestDiagnosticReportCaptureOrder` | 既存機構の回帰 | slice 数 4 と各 `sample_count == 3` |
| T35 | `test_keeps_the_batch_view_and_spatial_axes_symbolic` | `dynamic_shapes` の黙殺（`int()` 混入） | `dimensions == ('batch','view','6','height','width')` の**厳密一致** |
| T36 | `test_the_exported_graph_runs_for_other_view_counts_and_resolutions[3 ケース]` | view 軸・空間軸の固定化 | ORT 出力 shape `(B, 1)` |
| T37 | `test_rejects_an_example_whose_view_axis_has_a_single_view` | `validate_for` の該当分岐 | 理由文に `大きさ 1 の軸` と `軸 1` |
| T38 | `tests/ml/test_architecture.py` の `RUNTIME_MODULES` | 層の逸脱 | 既存機構 |

### 計画に無かった追加分（実装と並行して確定した仕様）

| # | テスト | 根拠 |
| --- | --- | --- |
| S1 | `TestViewDropout` 10 本 | orchestrator 裁定の `ViewDropout` 公開 IF。決定論・batch 内 V 均一・昇順重複なし・大域乱数非依存・下限超過の理由文字列 |
| S2 | `TestPredict` の軸数 / 要素型 / 入力名検査 3 本 + `test_benchmark.py` 2 本 | ユーザー決定 5。**`推論に失敗しました` が出ないこと**で「`ml` が弾いた / ORT が弾いた」を区別 |
| S3 | `test_a_fully_saturated_evaluation_still_reports_why` / `test_a_healthy_run_reports_no_saturated_positive_target` / `test_reduce_keys_match_the_regression_metric_fields` | orchestrator 裁定。飽和診断 6 項目を metric の可否に関わらず常に返す。**正常時も同じ key で 0.0 を出す**のが監視の契約（失敗時だけ現れる指標は推移を追えない） |
| S4 | `test_the_mean_head_is_never_born_dead` / `test_rejects_a_mean_bias_that_is_not_a_positive_finite_value` | dying ReLU への手当て（下記 F1） |
| S5 | `test_the_default_side_and_pixel_limits_agree_on_a_square` | 新既定値で 2 制約が同時に接することの明示 |

## 実装側へ差し戻した発見（すべて orchestrator 経由で通達済み）

### F1. ReLU 化で平均 head が初期化時点で恒久的に死ぬ（must-fix、対応済み）

`tests/ml/support.build_synthetic_model(seed=0)` で **Adam 200 step 回しても平均が厳密 0 のまま**。
`_mean.bias` の初期値が負だと batch 全体が同じ負の前活性へ落ち、平均 head の weight / bias への
勾配も 0 になって回復しない。E5 は「飽和 sample の**入力**への勾配が 0」までは見たが、
**「全 sample 飽和 → parameter 勾配 0 → 恒久停止」を見ていなかった。**
→ `GaussianHeadConfig.mean_bias_initial: float = 1.0`（正の有限値を要求）で決着。

**回帰検出器は `test_the_mean_head_is_never_born_dead`。** bias の符号は初期化のコイントスなので
1 seed では 50% ですり抜ける。**32 seed** で回している。ここを 1 seed へ縮める変更は
テストの検出力を半減させるので入れないこと。

### F2. export は batch 軸を `images` と `conditioning` の両方へ宣言しないと失敗する

計画の E2 は `images` / `valid_pixel_mask` / `conditioning` のうち mask 付きで検証していて、
conditioning の batch 軸を宣言していなかった。片方だけだと

```
Received user-specified dim hint Dim.DYNAMIC(min=None, max=None),
but tracing inferred a static shape of 2 for dimension inputs['images'].shape[0].
```

で export そのものが失敗する。`GaussianRegressionHead._joined_inputs` が両者の batch size を
突き合わせるため。`tests/ml/model/test_multiview.py` の `EXPORT_OPTIONS` にコメント付きで固定した。

**計画書の実測が網羅的でない実例。** 出荷用の export ヘルパーを書くときは同じ罠に注意。

### F3. 新既定値では `maximum_pixels` が単独では効かない

`maximum_size=512` / `maximum_pixels=512²` だと、`max(h,w)² >= h*w` が常に成り立つので
面積側の制約が最大辺側より先に効く入力が**存在しない**。
`test_is_limited_by_the_pixel_budget` は既定値のままだと機構を観測できなくなるので、
`ImageConstraints(maximum_size=512, maximum_pixels=65_536)` を明示して機構を残した。
既定値で 2 つが同時に接することは `test_the_default_side_and_pixel_limits_agree_on_a_square` で別に固定。

### F4. `MultiViewPaddedBatch.pad` は `ValueError`、`ViewDropout` は理由文字列

異常系の返し方が 2 通りある点は意図どおりと判断した。`pad` は呼び出し側の invariant 違反
（既存 `PaddedBatch.pad` と同じ作法）、`ViewDropout` は実データで起こりうる不整合
（1 view で収集した session に `minimum_view_count=3` を当てる）。**混同しないこと。**

## テスト化できなかった行と理由

### T25「前活性が正の入力で `mean` が前活性と一致する」

**書けない。** 前活性は `_mean`（private 属性）の出力で、公開インターフェースから取り出せない。
`state_dict()` のキーを触るのは private 名への依存なので採らなかった。

代替として、ReLU と Softplus を分離する観測点を 2 本に分解して置いた。

- 負側: 出力が**厳密に 0**（`test_mean_is_exactly_zero_when_the_pre_activation_is_negative`）
- 正側: 入力への勾配が**非零**、負側は**厳密に 0**（`test_a_saturated_sample_has_exactly_zero_gradient_to_its_features`）

Softplus はどちらも満たさないので、T25 が守ろうとした「ReLU が恒等写像である」性質は
実質的に固定できている。ただし「ReLU である」と「正側が恒等な任意の活性化」は区別できない。

### 飽和探索に既定設定が使えない

`mean_bias_initial=1.0` では 500 seed 探しても飽和する特徴量が見つからない（それが狙い）。
ReLU の厳密 0 を観測するテストだけ `mean_bias_initial=1e-3`（正の有限値なので `validate` は通る）
を使っている。**この定数を消すと T24 / T24' が観測対象を失う。**

### `ModelSize.measure` の `AttributeError`（E7）

範囲外。多視点 model は mask が `Tensor | None` なので計測側は必ず実 mask を渡す必要がある、
という制約だけが残っている。別 MR。

## 次 MR の候補

1. **`saturated_positive_fraction` を学習の停止条件・警告に使う**（orchestrator が本 MR 範囲外と裁定）
2. `ImageConstraints.stride` と `PaddedBatch.pad(stride=)` / `plan_pixel_budget_batches(stride=)` の
   配線一本化（計画 R4）。今は既定値をそろえてテストで固定しているだけで、片方だけ変えれば黙ってずれる
3. 理由コード（`Literal`）と人間向け文面の分離（計画 R6）。本 MR で理由文の部分一致 assert が
   さらに 15 本ほど増えた
4. `ModelSize.measure` の `None` 耐性（E7）
5. attention pooling を平均 pooling と同じ validation split で比較（計画 論点 3）

## 変異実験への申し送り

合流後に回すとき、**次の 4 か所は変異で落ちにくいので重点的に見てほしい**。

- `ImageConstraints.validate_augmentation` の `math.floor`：`round` へ変えても
  27×0.5 と 53×0.5 の 2 ケースは同じ判定になる。境界ケース（31 / 32）が唯一の検出器
- `_bucket_key` の `aspect` 項：点塗布 crop はすべて正方形なので、production では常に 0。
  `test_separates_different_aspect_ratios`（既存、64x1024 / 1024x64）だけが守っている
- `MultiViewImageEncoder.forward` の `unflatten(0, (batch_size, view_count))`：
  B と V を入れ替えても B=V の example では通る。T20 / T21 は B≠V で書いてある
- `ViewDropout.view_indices_for` の `sorted(...)`：`generator.sample` は多くの場合すでに
  昇順に近いので、`test_every_row_is_sorted_and_free_of_duplicates` が唯一の検出器
