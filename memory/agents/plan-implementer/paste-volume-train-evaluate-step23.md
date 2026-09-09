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
   | 固定 lr 1e-3、clip 1.0、AdamW | 0.022〜0.624（2 seed が 1/10 を超える） |
   | **cosine lr 1e-3 → 0、clip 1.0、AdamW** | **0.0046〜0.0314（全 seed 合格）** |

   理由は負の対数尤度の条件で、残差が縮むほど `exp(-log 分散)` が大きくなって平均側の
   勾配が跳ねる。実 Trainer は `ReduceLROnPlateau` で同じ問題に対処している。
7. **`PasteVolumeTask.training_step` は batch の形を検算しない。** `PasteVolumeBatch` は
   `validate()` を持たず、形は `PasteVolumeCollator.collate` の構築の仕方で決まる。
   食い違いは model の入力検査が捕まえるので、同じ不整合に検出器を 2 つ置かない
   （`MultiViewImageEncoder._reject_invalid_inputs` の docstring と同じ作法）。

## step 5 / step 8 への申し送り（重要）

**encoder は「全体の明るさの差」を第 1 GroupNorm でほぼ落とす。** 過学習テストの
合成 batch を作るときに実測した:

- sample ごとに**振幅だけ**を変えた入力（`0.2 + 2 * target + noise`）では、200 step
  学習しても MAE が初期値からほとんど動かない（8 sample を見分けられない）
- post 側 channel に**面積の違う明領域**を置く（空間構造で差を付ける）と学習する

GroupNorm は sample ごとに channel + 空間で正規化するので、入力全体の DC 成分と
振幅 scale は group 平均・分散として除かれ、group 内の channel 間の比だけが残る。

**これは step 7 のベースライン再測定と正面からぶつかる。** 最も強い素朴特徴は
`abs_diff_mean`（相関 +0.890）と `brightness_diff`（-0.786）で、どちらも **1 枚の画像の
大域統計**そのもの。pre 3 channel と post 3 channel は同じ group に混ざるので比としては
残るが、CNN がこの素朴回帰（MAE 0.0340 / R^2 0.697）を超えられるかは、この正規化を
通しても pre/post の差が読めるかに懸かる。**step 8 で成功条件 3（定数予測 0.0698 超え）
すら満たさない場合、encoder の正規化が第 1 容疑者。** その場合の打ち手候補は
(a) `conditioning` に大域輝度差を足す、(b) 第 1 stem だけ GroupNorm を外す、
(c) group 数を 1 にする（= LayerNorm 相当で channel 間の比を保つ）。
**いずれも仕様書 §2 が「精度比較なしに構成を変えない」としているので、実測を添えて
提案する形にする。**

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
| `evaluation_step` の `torch.no_grad()` を外す | 自然な別の形 | `test_evaluates_without_building_a_graph` |
| `compile_forward` が `self._model` を差し替える | 自然な別の形 | `test_keeps_the_state_dict_keys_after_compiling_the_forward` |

## 検証結果

`make ml-docker-check`（format → 型検査 → `tests/ml`）:

- format（pre-commit 全 hook）: **pass**（再整形なし。2 回連続で無変更を確認）
- 型検査（pyright、`src/ml tests/ml scripts/ml_smoke.py`）: **pass**（指摘 0 件）
- `tests/ml`: **pass**（1538 passed, 1 skipped）

`make test` / `make run` / `pytest -m hardware` は実行していない。
