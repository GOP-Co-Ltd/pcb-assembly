# paste_volume train / evaluate: step 2 + step 3

commit: `b507c37`（step 2）、`97d1a62`（step 3）。先行して `f64f217`（codespell 対応、下記）。
branch `feature/2026-09-09/paste-volume-train-evaluate`。

## 実測値

### step 2: v1 encoder の諸元

| 項目 | 実測 | 仕様書 §2 |
| --- | ---: | ---: |
| parameter 数 | **395,048** | 395,048 |
| parameter tensor 数 | 43 | - |
| `total_stride` | 8 | 8 |
| `output_features` | 96 | 96 |
| GMAC（53px x 5 view） | **0.159780** | 0.16 |
| GMAC（159px x 5 view） | **1.307149** | 1.31 |
| `validate_for_constraints(ImageConstraints())` | `None` | - |

`apply_fine_tune_freeze` は **21 tensor / 86,368 要素**を凍結し、残る学習対象は
**308,680 要素**（最終 stage・pooling 後 Linear・2 head・learnable padding pixel）。
`padding_pixel.requires_grad` は freeze 後も True。

### step 2: trunk ReLU の死（仕様書 §2 申し送り 2）

**0/100。** 観測は「全 0 でない入力を与えたとき head 出力が sample 間で動くか」で、
`log_variance` の batch 内 max - min が 0 かどうかを見る。平均側は weight を 0 から
初期化しているので学習前は必ず bias 一定になり、trunk の生死を映さない。

- 入力は 4 sample x 5 view x 53px の一様乱数（生成器を model の初期化 seed と分離。
  固定しないと観測した差が初期化由来か入力由来か分けられない）
- 自己検査: trunk の bias を -1e6 にすると同じ観測器が spread 0.0 を返す

**`hidden_features=128` の再検討は不要**（仕様書の 0/100 と一致）。

### step 3: 合成 8 sample の過学習（seed 0）

| 項目 | 値 |
| --- | ---: |
| 初期 MAE | **0.067857**（= mean bias 0.15 一定の予測。真値 0.10〜0.30 の 8 点） |
| 最終 MAE（200 step 後） | **0.002133** |
| 比 | **0.0314**（判定は 1/10 未満） |
| NLL の 40 step ごとの平均 | -1.933 / -2.316 / -2.940 / -4.013 / -4.445（単調減少） |

seed を振った実測（下記の学習率設定で 5 seed）: 比は 0.0046〜0.0314 で全 seed が
1/10 を下回る。block 平均の単調性は 5 seed 中 4 seed で成立（seed 3 のみ 1 箇所で反転）。
**テストは seed 0 固定。**

### step 3: compile parity

`CompileParityResult.measure`（`backend="inductor"`、`fullgraph=True`、
`FLOAT32_PARITY_TOLERANCES`、padding が生じる 2 sample batch）:
**`passed=True`、`checked_gradient_count == 43`（全 parameter tensor）**。所要 11.8 秒。

`checked_gradient_count` を見るのは、突き合わせた勾配が 0 本だと
`within_tolerance = all([])` が空全称で真になり parity が素通りするため。

## 計画外の判断ログ

1. **`build_paste_volume_model` は `initial_weights` の読み込みも `fine_tune` の freeze も
   しない。** 計画書の `run_training` が「build → load → freeze」の順を持っているので、
   順番の所有者を entrypoint 側に一本化した。config の 2 field は step 5 が読む。
2. **1.5M parameter / 1.5 GMAC の上限定数を `model.py` へ置かなかった。** 計画書では
   step 5（`run_training` の 4 番）が判定するので、そこで必要になった時点で置く。
   テスト側には `GIGA_MULTIPLY_ACCUMULATE_BUDGET = 1.5` として仕様書の上限を書いてある。
3. **`apply_fine_tune_freeze` は期待する parameter 名が見つからなければ例外にする。**
   `ml.model` 側の属性名が変わったとき「1 つも凍結しない freeze」が黙って成功するのを
   防ぐため。凍結対象は state_dict のキーの接頭辞で決めていて、これは checkpoint と
   export の公開契約（`TrainingTask.model` の docstring）なので名前依存でよいと判断した。
   最終 stage の番号は名前から実測する（stage 数を定数で持たない）。
4. **`ZeroTargetMetrics` の metric に `zero_target_` の接頭辞を付けた（計画書に明記なし）。**
   `ZeroTargetMetrics` は `sample_count` / `weight_sum` / `mean_absolute_error` /
   `one_standard_deviation_coverage` の 4 つを `GaussianRegressionMetrics` と同じ名前で
   持つ。接頭辞なしで同じ写像へ入れると blank 16 件の値が全体の値を黙って上書きする。
   `reduce` が返す本数は 6 + 14 + 6 = **26**（接頭辞を外すと 22）で、テストが本数を固定する。
5. **`_as_float_mapping` を `ml.training.task` から複製した。** 計画書 R6 が許容した複製。
   `ml` コア側の private を import しないため。接頭辞引数を足してある。
6. **過学習テストの学習率を cosine で減衰させた（計画書に明記なし）。** optimizer は
   `ml.training.loop` と同じ AdamW（lr 1e-3 / weight decay 1e-4）と `clip_grad_norm_(1.0)`
   にそろえてあるが、**固定学習率では 200 step で収束しない**。実測:

   | 設定 | 5 seed の最終 MAE / 初期 MAE |
   | --- | --- |
   | 固定 lr 1e-3、clip なし、Adam | 0.95〜2.39（学習していない） |
   | 固定 lr 1e-3、clip 1.0、AdamW | 0.022〜0.624（**5 seed 中 4 seed が 1/10 を超える**） |
   | **cosine lr 1e-3 → 0、clip 1.0、AdamW** | **0.0046〜0.0314（全 seed 合格）** |

   **訂正（2 巡目レビューの再測）。** 当初この表に「2 seed が 1/10 を超える」と書いたが
   数え間違いで、実測は **4/5**。seed 0-4 の固定 lr は
   **0.1482 / 0.0223 / 0.6238 / 0.4071 / 0.1719**（1/10 に収まるのは seed 1 だけ）。
   cosine 側は 0.0314 / 0.0046 / 0.0176 / 0.0129 / 0.0221。
   結論（cosine が必要）は変わらないが、判断の強さは変わる。2/5 なら「seed を選べば
   済む」だが、4/5 なら「固定 lr では成立しない」。

   理由は負の対数尤度の条件で、残差が縮むほど `exp(-log 分散)` が大きくなって平均側の
   勾配が跳ねる。実 Trainer は `ReduceLROnPlateau` で同じ問題に対処している。
7. **`PasteVolumeTask.training_step` は batch の形を検算しない。** `PasteVolumeBatch` は
   `validate()` を持たず、形は `PasteVolumeCollator.collate` の構築の仕方で決まる。
   食い違いは model の入力検査が捕まえるので、同じ不整合に検出器を 2 つ置かない
   （`MultiViewImageEncoder._reject_invalid_inputs` の docstring と同じ作法）。

## step 5 / step 8 への申し送り（重要）

### GroupNorm と大域統計（2 巡目レビューで訂正済み）

**この節は 1 度誤った内容で書いた。以下が実測に基づく訂正版で、`orchestrator/`
メモの同名の節が正典。**

当初の申し送りは「encoder 冒頭の GroupNorm が入力全体の明るさ・振幅の差をほぼ落とすので、
step 7 の最強の素朴特徴 `abs_diff_mean` / `brightness_diff` が乗る経路が塞がっている。
成功条件 3 を満たさなければ正規化が第 1 容疑者」だった。**2 巡目レビューが実測で反証した。**

`PasteVolumeTask` + 200 step + cosine、8 sample、最終 MAE / 初期 MAE:

| 合成入力 | group 数 8 | group 数 1 |
| --- | ---: | ---: |
| 面積（空間構造。現行の `_task_batch`） | 0.0314 | - |
| **6 channel 共通の振幅** ∝ 真値 | **0.8421**（定数予測の床、5 seed とも） | **0.8421** |
| **post 3 channel だけ**の輝度 ∝ 真値 | **0.0210** | 0.0180 |

1. 「6 channel 共通の振幅だけの差は学習できない」という実測そのものは正しい
2. **`group_norm_groups=1` にしても直らない（0.8421 のまま）。当初挙げた打ち手 (c)
   「group 数を 1 にする（= LayerNorm 相当）」は、この現象に対して測定上まったく効かない**
3. **pre/post の contrast は GroupNorm を生き残る**（post だけ明るくした batch は
   0.0210 で学習する）。**step 7 の `brightness_diff = mean(post) - mean(pre)` と
   `abs_diff_mean = mean(|post - pre|)` はどちらも 6 channel 共通の大域 scale ではなく
   pre 対 post の contrast。この 2 特徴が乗る経路は塞がっていない**

**したがって GroupNorm は step 8 の第 1 容疑者ではない。** 面積経路も contrast 経路も
生きている。成功条件 3 を満たさなかった場合の原因帰属を正規化へ短絡させないこと。
残る打ち手候補は (a) `conditioning` に大域輝度差を足す / (b) 第 1 stem の GroupNorm を外す。
いずれも仕様書 §2 が「精度比較なしに構成を変えない」としているので、実測を添えて提案する。

### 計算量 gate をどの shape で測るか（step 5 が読む）

実測 GMAC（`measure_paste_volume_model`、1 sample あたり）:

| 入力 | GMAC |
| --- | ---: |
| 53x53 x 1 view | 0.031966 |
| 53x53 x 5 view | 0.159780 |
| 159x159 x 5 view | 1.307149 |
| **512x512 x 1 view** | **2.677027** |
| 512x512 x 5 view | 13.385085 |

仕様書 §2 は「512x512 入力で 1.5 GMAC 以下」と「159px x 5 view が計算量上限に最も近い構成」
（1.31）を**両方**書いていて自己矛盾している。512 で測ると全 run が学習前に拒否され、
159px で測ると gate は原理的に発火しない。

**orchestrator 裁定: gate は点塗布 crop の上限（159px x 5 view）で測る。model は縮めない。**
512 の記述は多視点化以前の単一 view 時代の残りで、`ImageConstraints.maximum_size=512` は
前処理の契約上限であって点塗布 crop の実際の上限ではない。仕様書 §2 本体の訂正は
orchestrator が別途行う。

**step 5 の `run_training` は
`measure_paste_volume_model(model, height=159, width=159, view_count=5)` の
`giga_multiply_accumulate` を 1.5 と比べること。** テスト側の同じ判定は
`test_model.py` の `test_stays_within_the_computation_budget_at_the_largest_crop`。

### freeze 忘れを run 記録から見えるようにする（step 5 が読む）

`build_paste_volume_model` は `initial_weights` の読み込みも `fine_tune` の freeze も
しない（判断ログ 1）。**この前提が破れると「fine-tune が効かない run」が黙って生まれる。**
`experiment=fine_tune` で `model.fine_tune=true` を立てた run は、step 5 が
`apply_fine_tune_freeze` を呼び忘れても全層 fine-tune として完走し、parameter 数も
metric も何も変わらない。

**`ModelSize.trainable_parameter_count` を MLflow param へ記録すること**
（freeze あり 308,680 / freeze なし 395,048）。freeze 忘れが run 記録から見える唯一の
観測点になる。build 側の契約は
`test_leaves_every_parameter_trainable_even_when_fine_tuning` が固定した。

### trunk の死の記録の粒度

「trunk ReLU の死は 0/100」は **batch 全体・全 unit が死ぬ完全な死だけ**を測った値。
128 unit のうち 1 unit でも生きていれば spread は 0 にならない（レビュー実測: 1 unit だけ
生かすと spread 7.651e-5 で「生きている」判定になる。seed 0 の実際の生存数は 71/128）。
**「trunk はほぼ生きている」までは含意しない。**

## その他の申し送り

1. **`pre-commit run -a` は untracked file を検査しない**（`git ls-files` を見るため）。
   新規 file は `git add` してから format を回さないと、commit した瞬間に
   `make ml-docker-check` が落ちる。step 2 でこれを踏み、formatter の再整形を
   `b507c37` へ amend した（未 push のローカル commit）。
2. **codespell が step 0/1 の申し送り（`...-step01.md`）の pyright 出力の綴りを拾って
   `make ml-docker-check` を落としていた。** pyright 出力の引用だったので言い換えた
   （`f64f217`）。この commit は step 2 の前に置いてある。
3. **docformatter は日本語の段落を再流ししたが、文字化けは起きていない。**
   整形前後で非 ASCII 文字の多重集合が一致することを 4 file すべてで確認した
   （`memory/MEMORY.md` の「docformatter が日本語を化けさせる」対策）。
4. `RUNTIME_MODULES` へ `ml.paste_volume.model`（step 2）と `ml.paste_volume.task`
   （step 3）を追記した。`DEPENDENCY_FREE_MODULES` は torch を読むので対象外。

## 変異テストで検査が働くことを確認した箇所

| 変異 | 種別 | 落ちるテスト |
| --- | --- | --- |
| `apply_fine_tune_freeze` の走査を空にする（何も凍結しない） | 検査器を壊す | `test_freezes_the_stem_and_every_stage_but_the_last`、`test_leaves_the_last_stage_the_head_and_the_padding_pixel_trainable`、`test_stops_the_gradient_at_the_frozen_parameters`（3 件） |
| `_head_output_spread` が常に 1.0 を返す（trunk 死の判定を常に真に） | 検査器を壊す | `test_the_same_observation_finds_a_trunk_that_is_dead` |
| `_padding_pixel_gradient` が常に 1.0 を返す | 検査器を壊す | `test_the_same_observation_sees_no_gradient_without_padding` |
| `_block_means` が定数列を返す | 検査器を壊す | `test_drives_the_negative_log_likelihood_and_the_error_down` |
| `ml.model.heads` の `nn.init.zeros_(mean_layer.weight)` を外す | 自然な別の形 | `test_starts_every_sample_at_the_configured_mean_bias` + 既存の `tests/ml/model/test_heads.py` 2 件 |
| freeze 対象から stem を落とす | 自然な別の形 | 上の freeze 3 件 |
| `ZeroTargetMetrics` の接頭辞を外す | 自然な別の形 | `test_reports_the_overall_metrics_and_both_diagnostics`、`test_keeps_the_zero_target_metrics_apart_from_the_overall_ones`（22 == 26 で落ちる）、`test_reports_the_diagnostics_when_no_regression_metric_can_be_measured`（3 件） |
| `reduce` が主要 metric を測れないとき空の写像を返す | 自然な別の形 | `test_reports_the_diagnostics_when_no_regression_metric_can_be_measured` |
| `evaluation_step` の `torch.no_grad()` を外す | 自然な別の形 | ~~`test_evaluates_without_building_a_graph`~~ **この主張は誤り（下記 2 巡目レビュー対応 M1）** |
| `compile_forward` が `self._model` を差し替える | 自然な別の形 | ~~`test_keeps_the_state_dict_keys_after_compiling_the_forward`~~ **この主張は誤り（下記 M2）** |

## 検証結果

`make ml-docker-check`（format → 型検査 → `tests/ml`）:

- format（pre-commit 全 hook）: **pass**（再整形なし。2 回連続で無変更を確認）
- 型検査（pyright、`src/ml tests/ml scripts/ml_smoke.py`）: **pass**（指摘 0 件）
- `tests/ml`: **pass**（1538 passed, 1 skipped）

`make test` / `make run` / `pytest -m hardware` は実行していない。

## 2 巡目レビュー（`code-reviewer/...-step23.md`）への対応

`f11db2c` の作業ツリーへ、must-fix 5 件と should-fix 11 件をすべて当てた。
触った file は `src/ml/paste_volume/model.py` / `task.py` /
`tests/ml/paste_volume/test_model.py` / `test_task.py` / `helpers.py` の 5 本
（helpers.py は S11 のため orchestrator が追加で割り当て、追記のみ）。

### 検出力ゼロだった 3 件の直し方（M1 / M2 / M3）

いずれも「観測点が原理的に主張を支えない」型。**detach / no-op / 定数化のあとでは
観測できない**という共通の理由だったので、`task.model` へ `register_forward_hook` を
挿し、**detach より前**を見る観測器 `_recorded_forwards(task, observe)` を 1 つ置いた。

- **M1**: `_observation_of` が detach するので、返り値の `requires_grad` は
  `torch.no_grad()` の有無に依らず必ず False。hook が受け取る出力の `requires_grad` を
  評価経路で `[False]`、学習経路で `[True]` と対で固定した
- **M2**: `torch.compiler.is_compiling()` を hook の中で読む。eager 実行では False、
  `compile_forward` 後は `training_step` / `evaluation_step` とも True。
  **backend は `eager`**（dynamo を通したかだけを見たいので inductor の build を要求しない）。
  loss が eager と一致することも別に 1 件置いた
- **M3**: `PasteVolumeCollator` が実際に組んだ batch を `_collated_batch` で 1 本取り、
  `conditioning` だけを +1.0 した batch と `log_variance` を比べる。
  学習経路と評価経路の両方を見るので、片方だけ差し替える変異も落ちる（S7 も同時に解消）

### 変異マトリクス（今回、`.mutants/<name>` の複製に対して実測）

baseline は `test_model.py` + `test_task.py` で **99 passed / 2 skipped**
（レビュー時の 74/2 から +25 本）。

| 変異 | 落ちたテスト |
| --- | --- |
| `evaluation_step` の `torch.no_grad()` を外す | **1**: `TestPasteVolumeTaskGradientGraph::test_evaluates_without_building_a_graph`（`assert [True] == [False]`） |
| `compile_forward` を `self._forward = self._model` へ | **1**: `TestPasteVolumeTaskCompile::test_routes_both_steps_through_the_compiled_forward`（`[False, False] != [True, True]`） |
| `conditioning` を `zeros_like` へ差し替え | **1**: `TestPasteVolumeTaskWithCollatedBatch::test_feeds_the_conditioning_into_the_model` |
| `MODEL_FAMILY` を改名 | **1**: `TestModelFamily::test_names_the_documented_model_family` |
| clamp 範囲を -30 / 30 へ | **1**: `TestPasteVolumeModelConfig::test_clamps_the_log_variance_to_the_documented_window` |
| `apply_fine_tune_freeze` の stem 検査を削除 | **1**: `test_rejects_a_model_whose_parameter_names_do_not_match[stem-stem]` |
| 同 padding pixel 検査を削除 | **1**: 同 `[padding_pixel-learnable padding pixel]` |
| `reduce` の `predictions.validate()` 早期 return を削除 | **1**: `TestPasteVolumeTaskReduce::test_returns_an_empty_mapping_for_misaligned_observations` |
| `build_paste_volume_model` が `fine_tune` で freeze する | **1**: `TestBuildPasteVolumeModel::test_leaves_every_parameter_trainable_even_when_fine_tuning` |
| `build_paste_volume_model` が常に freeze する | **4**: 上の 1 件 + `test_the_same_observation_sees_gradients_without_the_freeze` + `TestPasteVolumeTaskEncoderGradient::test_flows_gradient_into_the_first_stem_convolution` + compile parity（`checked_gradient_count` が 22 へ落ちる） |

**レビューで「0 件」だった 10 変異がすべて落ちるようになった。**

### should-fix の対応

- **S1**: `fine_tune` の docstring を `initial_weights` と同じ形（適用者は entrypoint）へ。
  build が 1 つも凍結しないこと、`initial_weights` の path を開かないことを各 1 件固定。
  step 5 への申し送り（`trainable_parameter_count` を MLflow param へ）は上記の節
- **S2 / S3**: `MODEL_FAMILY` と clamp 範囲を仕様書 §2 の確定値として固定
- **S4**: 3 部位を 1 つずつ欠く stub（`_StubRegressor`）へ parametrize し、
  3 本の理由文を固定。番犬そのものを消す変異が見えるようになった
- **S5**: 早期 return を 1 件固定し、`reduce` の docstring にこの分岐だけ診断も返さない
  理由（集計の失敗ではなく観測値の組み立ての誤り）を足した
- **S6**: 過学習テストの docstring を「head まで含めた経路が勾配を通して収束すること」へ
  書き直し（実測: head 2 本 258 要素だけでも比 0.0009 で合格する）。
  **encoder へ勾配が届くことは新設の
  `TestPasteVolumeTaskEncoderGradient::test_flows_gradient_into_the_first_stem_convolution`
  が測る**（`_encoder._encoder._stem.0.0.weight` の勾配が 0 でないこと。実測 2.96e-2。
  encoder を凍結する変異で `grad is None` になり落ちる）
- **S7**: `_collated_batch` で collator の出力を task へ通す 2 件を新設（M3 と同じ経路）
- **S8**: 上記「計算量 gate をどの shape で測るか」の節へ実測 GMAC 表と裁定を記録。
  `test_model.py` の budget 定数のコメントにも同じ裁定を書いた
- **S9**: 上記「trunk の死の記録の粒度」の節と `TestTrunkActivation` の docstring
- **S10**: 本 MR の diff 内の 6 件を直した（`test_model.py:3 / :96 / :119`、
  `test_task.py:3 / :862 / :1061`）。**直し方: 段落ごとに 1 物理行・インデント込み 72 文字
  以内**に収めると docformatter が再流ししない。長い段落は空行で分割する
- **S11**: `paste_volume_model(seed)` と `PADDING_PIXEL_NAME` / `STEM_CONVOLUTION_NAME` を
  `tests/ml/paste_volume/helpers.py` へ移し、2 file から使うようにした（追記のみ）

### 未対応・orchestrator へ返すもの

- **`test_task.py` の docformatter 崩れ 5 件（`:343` `:812` `:813` `:834` `:847`）と
  `helpers.py` の 2 件（`:190` `:254`）はレビュー時点で「step 0/1 由来・本 MR の diff 外」
  と分類されたもので、今回は触っていない。** `a9736e2` の S8 対応で拾い切れていない
- **`docs/image-based-dispense-calibration-ml-plan.md` は orchestrator が編集中**なので
  commit に含めていない
