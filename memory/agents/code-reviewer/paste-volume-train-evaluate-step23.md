# paste_volume train / evaluate step 2 + step 3 レビュー

対象: `git diff fb65491..4bd6a82 -- src tests`
（`f64f217` codespell / `b507c37` step 2 model / `97d1a62` step 3 task / `4bd6a82` メモ）。
`4bd6a82` より後の commit（別 agent の step 0/1 修正）は対象外。

レビューは repo を書き換えずに行った。`4bd6a82` を `.review-mutants/base/` へ凍結複製し、
変異は `.review-mutants/mut/<name>/` へ複製してから `--workdir` + `PYTHONPATH` で差し替えた。
複製は削除済み。作業ツリーは無変更（並行 agent が step 0/1 修正中のため）。

## verdict: request-changes

指摘の大半は「実装が間違っている」ではなく「**検査・記録が主張を支えていない**」型。
src の振る舞い自体で誤りと判定したものは無い（後述の「良かった点」参照）。

---

## must-fix

### M1. `test_evaluates_without_building_a_graph` は原理的に落ちない（7 回目の同じ型）

- 対象: `src/ml/paste_volume/task.py:317-324`（`evaluation_step`）、`:385`（`_observation_of`）、
  `tests/ml/paste_volume/test_task.py:983-996`
- 問題: `_observation_of` が `mean.detach()` / `log_variance.detach()` を掛けるので、
  `observation.mean.requires_grad` は `torch.no_grad()` の有無に関わらず必ず `False`。
  テストが見ている 2 つの assert は `no_grad` を 1 段も観測していない。
- 根拠（測定）: `evaluation_step` から `with torch.no_grad():` を外した変異で
  `test_model.py` + `test_task.py` は **74 passed / 2 skipped（全緑）**。
  同じ入力で detach 前の tensor を直接見ると `requires_grad=True, grad_fn=ReluBackward0`
  ——グラフは実際に作られている。
- 申し送り（`plan-implementer/...-step23.md` の変異表）は
  「`evaluation_step` の `torch.no_grad()` を外す → `test_evaluates_without_building_a_graph`
  が落ちる」と書いているが、**この主張は再現しない**。
- なぜ深刻か: `evaluation_step` は Trainer の評価 loop から validation split の全 batch に
  対して呼ばれる。グラフを捨て損ねると保持メモリが split サイズに比例して伸び、
  step 8 の 5 fold 実走で OOM か速度劣化として初めて出る。
  加えてこのリポジトリが 6 回踏んだ「検出力ゼロの assert が黙って緑」の 7 例目。
- 直し方（案）: detach より前を観測する。`task.model` へ `register_forward_hook` を挿し、
  hook が受け取る出力の `requires_grad` が `evaluation_step` 中は `False`、
  `training_step` 中は `True` であることを対で見る（公開 API だけで組める）。
- 確信度: 高（測定済み）。深刻度: 高

### M2. `PasteVolumeTask.compile_forward` に振る舞いのテストが 1 件も無く、no-op compile が全緑

- 対象: `src/ml/paste_volume/task.py:368-383`、`tests/ml/paste_volume/test_task.py:1140-1183`
- 問題: `compile_forward` を `self._forward = self._model`（＝ compile しない）へ変えても
  **74 passed / 2 skipped（全緑）**。既存 2 件のうち
  `test_keeps_the_state_dict_keys_after_compiling_the_forward` は
  「`task.model` が元 module のまま」「`state_dict` に `_orig_mod` が無い」しか見ておらず、
  no-op はどちらも満たす。`test_rejects_compile_options_that_do_not_validate` は
  `options.validate()` の転送だけ。
- parity テスト `test_the_inductor_backend_matches_eager_forward_loss_and_gradient`
  （`test_task.py:1159-1183`）は **`PasteVolumeTask` を通っていない**。
  `CompileParityResult.measure(_model(), ...)` に生 model を渡しており、
  `ml.model.multiview` 側の parity を測っているだけで、task の compile 経路は素通り。
  計画書 step 3 の検証項目「eager と `torch.compile` の forward / loss / gradient parity」は
  task 層では未達。
- なぜ深刻か: `src/ml/training/loop.py:444` が `self._task.compile_forward(...)` を呼ぶ唯一の
  入口。ここが no-op でも Trainer は成功し、`trainer=gpu`（compile ON）の run は
  黙って eager になる。計画書 R4 の「first-step 時間と遭遇 shape 数を MLflow へ記録して
  compile の実効を測る」も、compile が効いていない前提が崩れると読めない。
- 直し方（案）: `compile_forward` 後の `training_step` / `evaluation_step` の loss が
  eager と一致することを 1 件見る（実測: eager 0.03182936 / compiled 0.03182936 で一致し、
  `_forward is not _model` になる）。parity 測定を task 経由に寄せてもよい。
- 確信度: 高（測定済み）。深刻度: 高

### M3. `training_step` / `evaluation_step` が `batch.conditioning` を渡していることを誰も確かめていない

- 対象: `src/ml/paste_volume/task.py:304-306`、`:321-323`
- 問題: `batch.conditioning` を `torch.zeros_like(batch.conditioning)` に差し替えても
  **74 passed / 2 skipped（全緑）**。
  `batch.target` を conditioning に渡す（真値 leak）変異でも落ちたのは 1 件だけで、
  しかも落ちた理由は `blocks == sorted(blocks, reverse=True)` の単調性が seed 依存で
  たまたま崩れたためであり、leak を検出したからではない。
- なぜ深刻か: `log(有効 pixel_per_mm)` は仕様書 §2 が明示した唯一の物理 scale 入力
  （「画像から見かけの大きさを学びつつ、物理scaleを明示的に利用できる構成」）。
  合成 batch では conditioning が全 sample 同値なので、落としても学習は成立してしまう。
  実データでは augmentation の scale が乗って sample ごとに変わる（`batch.py:150-152` の
  `math.log(sample.entry.pixel_per_mm * scale)`）ので、
  ここが壊れた run は step 8 の精度低下としてしか現れない。
- 直し方（案）: conditioning だけを変えた 2 batch で出力が変わることを 1 件見る
  （平均側は学習前 bias 一定なので `log_variance` 側で観測する。`_head_output_spread` と同じ手口）。
- 確信度: 高（測定済み）。深刻度: 中〜高

### M4. 申し送りの「固定学習率では 5 seed 中 2 seed が 1/10 に届かない」が再現しない（実測 4 seed）

- 対象: `memory/agents/plan-implementer/paste-volume-train-evaluate-step23.md` 判断ログ 6、
  `memory/agents/orchestrator/paste-volume-train-evaluate.md`「計画外の判断（レビュー対象）」
- 根拠（測定、`_overfit` と同一設定: AdamW lr 1e-3 / wd 1e-4 / clip 1.0 / 200 step /
  batch seed 11 / model seed 0-4）:

  | seed | cosine あり | 固定 lr |
  | ---: | ---: | ---: |
  | 0 | 0.0314 | **0.1482** |
  | 1 | 0.0046 | 0.0223 |
  | 2 | 0.0176 | **0.6238** |
  | 3 | 0.0129 | **0.4071** |
  | 4 | 0.0221 | **0.1719** |

  cosine 側の min/max（0.0046 / 0.0314）も固定側の min/max（0.022 / 0.624）も申し送りの
  表と一致するので、**測った集合は同じで、1/10 を超えた本数の数え間違い**。実際は 4/5。
- 影響: 結論（cosine が必要）は変わらない。ただし step 0/1 の M2 と同じく、
  step 8 が読む記録に誤った数字が残る。cosine を入れた判断の妥当性の強さも変わる
  （2/5 なら「seed を選べば済む」だが 4/5 なら「固定 lr では成立しない」）。
- 確信度: 高（測定済み）。深刻度: 中（記録の誤り）

### M5. GroupNorm の申し送りが過度に一般化されており、提案された打ち手 (c) は実測で効かない

- 対象: `plan-implementer/...-step23.md`「step 5 / step 8 への申し送り（重要）」、
  `orchestrator/...`「GroupNorm が大域統計を落とす（step 8 の第 1 容疑者）」、
  `tests/ml/paste_volume/test_task.py:855-864`（`_task_batch` docstring）
- 根拠（測定、`PasteVolumeTask` + 200 step + cosine、8 sample、最終 MAE / 初期 MAE）:

  | 合成入力 | group 数 8 | group 数 1 |
  | --- | ---: | ---: |
  | 面積（現行の `_task_batch`） | 0.0314 | - |
  | **6 channel 共通の振幅** ∝ 真値 | **0.8421**（= 定数予測の床、5 seed とも 0.842） | **0.8421** |
  | **post 3 channel だけ**の輝度 ∝ 真値 | **0.0210** | 0.0180 |

- 読み取り:
  1. 「振幅だけの差は学習できない」という実測そのものは正しい（5 seed とも定数予測の床）。
  2. **`group_norm_groups=1` にしても直らない**（0.8421 のまま）。
     6 channel 全部に同じ scale が掛かった信号は、group 数に依らず sample ごとの
     正規化で消える。申し送りの打ち手 **(c)「group 数を 1 にする（= LayerNorm 相当で
     channel 間の比を保つ）」は、この現象に対しては測定上まったく効かない。**
     残る (a) conditioning に輝度差を足す / (b) 第 1 stem の GroupNorm を外す は有効な候補。
  3. **pre/post の contrast は GroupNorm を生き残る**（post だけ明るくした batch は 0.0210 で学習する）。
     step 7 の最強素朴特徴 `brightness_diff = mean(post) - mean(pre)`（相関 -0.786）と
     `abs_diff_mean = mean(|post - pre|)`（+0.890）は、どちらも
     **6 channel 共通の大域 scale ではなく pre 対 post の contrast**。
     申し送りは両者を「1 枚の画像の大域統計」と括って step 8 の第 1 容疑者に据えているが、
     この 2 特徴が乗る経路は測定上ふさがっていない。
- なぜ深刻か: orchestrator メモは「成功条件 3 すら満たさない場合、encoder の正規化が
  第 1 容疑者」と裁定を記録済みで、step 8 の結果解釈がこの記述に直結する。
  効かない打ち手を候補に残し、生きている経路を潰れていると書いた記録のまま step 8 に入ると、
  結果の原因帰属を誤る。
- 直し方: 上の 3 点で申し送りと `_task_batch` docstring を書き直す
  （docstring の「振幅だけを変えても … 消える」は「6 channel 共通の振幅は」に限定する）。
- 確信度: 高（測定済み、5 seed）。深刻度: 高（step 8 の解釈に直結）

---

## should-fix

### S1. `build_paste_volume_model` が `fine_tune` / `initial_weights` を無視する契約が両方向とも無検査

- 根拠（測定）: `build_paste_volume_model` の中で `if config.fine_tune: apply_fine_tune_freeze(model)`
  を実行する変異を入れても **74 passed（全緑）**。逆向き（現状の「何もしない」）を固定する
  テストも無い。`grep -rn "fine_tune=True\|initial_weights" tests/ml` は 0 件。
- `PasteVolumeModelConfig.fine_tune` の docstring は
  「True なら最終 stage / MLP / head / padding だけを更新する」と、**この module が実装していない
  振る舞いを断定している**（`initial_weights` の docstring は「読み込みは entrypoint 側の責務」と
  明記しているのに、`fine_tune` にはその但し書きが無い）。
- ユーザー質問「前提が破れたときに黙って fine-tune が効かない run が生まれないか」への答え:
  **生まれる**。`experiment=fine_tune`（計画書 step 4 の TOML）で `model.fine_tune=true` を
  立てた run が、step 5 が freeze を呼び忘れると全層 fine-tune として完走し、
  parameter 数も metric も何も変わらない（`ModelSize.trainable_parameter_count` を
  記録していれば 308,680 vs 395,048 で気づけるが、step 2 はそれを記録していない）。
- 提案: (a) docstring を `initial_weights` と同じ形（適用者は entrypoint）へそろえる、
  (b) `build_paste_volume_model(PasteVolumeModelConfig(fine_tune=True))` が
  全 parameter `requires_grad=True` を返すことを 1 件固定する、
  (c) step 5 へ「`measure_paste_volume_model` の `trainable_parameter_count` を MLflow param へ」
  を申し送る（freeze 忘れが run 記録から見える唯一の観測点になる）。
- 確信度: 高（測定済み）

### S2. `MODEL_FAMILY` が無検査

- 根拠（測定）: `MODEL_FAMILY = "paste-volume-v2"` へ変えても **74 passed（全緑）**。
  `tests/ml` のどこからも参照されていない。
- 仕様書 §2 が `paste-volume-resnet-small-v1` で固定した識別子で、step 5 の
  `StudyIdentity.build(model_family=...)` と export manifest / promotion の突き合わせ鍵になる。
  黙って変わると Optuna study と過去 run の対応が切れる。
- 確信度: 高（測定済み）

### S3. `log_variance_minimum` / `log_variance_maximum` が無検査（仕様書 §2 の clamp 範囲）

- 根拠（測定）: 既定を `-30.0 / 30.0` へ変えても **74 passed（全緑）**。
  `test_feeds_the_head_with_the_pooled_features_and_one_condition`（`test_model.py:160-165`）は
  `input_features` / `conditioning_features` / `hidden_features` しか見ていない。
- 仕様書 §2 は `log_variance_volume_ul2 = clamp(raw_logvar, -14, 5)` を確定値として書いており、
  blank の NLL が下限へ張り付く挙動（§2 申し送り 3、成功条件 5 の観測点）は
  この値そのものに依存する。他の §2 確定値（parameter 数・total_stride・GMAC・
  `mean_bias_initial`・group 数）はすべて固定されているのに、ここだけ抜けている。
- 参考: 他の config 誤りは実測で全部捕まった。
  `group_norm_groups=4`（parameter 数も GMAC も total_stride も不変）→ 1 件失敗、
  `hidden_features=64` → 3 件、`blocks_per_stage=(2,3)` → 5 件、
  `stem_strides=(1,4)`（parameter 数・total_stride 不変）→ GMAC 2 件が失敗。
  **ユーザー質問「parameter 数 395,048 の完全一致が config を間違えたときに落ちるか」への答え:
  落ちる。** parameter 数だけで漏れる誤りも GMAC / group 数の否定テストが拾う。
  唯一漏れているのが clamp 範囲。
- 確信度: 高（測定済み）

### S4. `apply_fine_tune_freeze` の 3 つの番犬のうち 2 つは削除しても全緑

- 対象: `src/ml/paste_volume/model.py:157-166`
- 根拠（測定）:
  - stem の存在検査を削除 → **74 passed（全緑）**
  - padding pixel の存在検査を削除 → **74 passed（全緑）**
  - stage の存在検査だけが `test_rejects_a_model_whose_parameter_names_do_not_match`
    （`test_model.py:357-363`、`match="residual stage"`）で固定されている
- ただし**番犬自体は機能している**: `_STEM_PREFIX` / `_PADDING_PIXEL_NAME` を誤った値へ
  変える変異ではどちらも 4 件落ちる。問題は「番犬を消す」変異が見えないこと。
  申し送り 3 が「`ml` 側の属性名が変わったとき『1 つも凍結しない freeze』が黙って成功するのを
  防ぐため」と目的を書いた仕掛けなので、その仕掛け自体を固定したほうがよい。
- 提案: `_RenamedRegressor` を 3 種（stem だけ欠く / padding だけ欠く / stage を欠く）に
  parametrize して 3 本の理由文を固定する。
- 確信度: 高（測定済み）

### S5. `reduce` の `predictions.validate()` 早期 return が無検査、かつ理由を捨てる

- 対象: `src/ml/paste_volume/task.py:356-357`
- 根拠（測定）: この 2 行を削除しても **74 passed（全緑）**。
- 併せて設計上の食い違い: この分岐は空の写像を返すので、運用者が受け取るのは
  Trainer の「monitor がありません」だけになる。**同じ docstring が
  「空の写像だけを返すと運用者が受け取るのは Trainer の『monitor がありません』になって
  真の原因が読めない」と書いて `MeanSaturationDiagnostic` を必ず返す設計にしている**のに、
  1 つ上の分岐がまさにその形になっている。
- 確信度: 高（測定済み）

### S6. 過学習テストは「encoder へ勾配が届いている」ことを示していない

- 対象: `tests/ml/paste_volume/test_task.py:1003-1020`
- 根拠（測定、同じ `_overfit` / 同じ batch / seed 0）:

  | 学習対象 | 最終 MAE / 初期 MAE | 合格 |
  | --- | ---: | --- |
  | 全 395,048 要素（現行） | 0.0314 | 合格 |
  | `apply_fine_tune_freeze` 後（308,680 要素） | 0.0127 | 合格 |
  | encoder 全凍結（trunk + head 12,802 要素） | 0.1658 | 不合格 |
  | **2 個の head だけ（258 要素）** | **0.0009** | **合格（全条件中で最良）** |

  凍結した 128 次元のランダム特徴に対する 128→1 の線形写像 2 本だけで 8 sample を
  記憶できるので、「8 sample を覚えきれる」は encoder が学習していることの証拠にならない。
- 併せて、入力が真値と無相関（sample ごとの乱数のみ）の batch でも **seed 3 では合格する**
  （比 0.0241。seed 0/1/2/4 は 0.836〜0.842 で不合格）。
- 一方で **ユーザー質問「cosine 減衰があると学習していない model でも通ってしまわないか」
  への答えは「通らない」**: images を `zeros_like` に差し替えた変異では
  比 0.842（= 定数予測の床 0.05714/0.06786）で確実に落ちる（2 件失敗）。
  cosine は最適化の条件を良くしているだけで、判定の敷居を下げてはいない。
- 提案: 過学習テストは現状維持でよいが、「encoder へ勾配が届く」は
  `test_model.py` の freeze / 非 freeze 対（`test_stops_the_gradient_at_the_frozen_parameters` /
  `test_the_same_observation_sees_gradients_without_the_freeze`。これは実測で効いている）が
  担っていることを docstring に書き分けたほうがよい。
  過学習テストの docstring「8 sample を覚えきれること」を「head まで含めた経路が
  勾配を通して収束すること」と読める文へ直す。
- 確信度: 高（測定済み）

### S7. task 層のテストが `PasteVolumeCollator` の出力を 1 度も通していない

- 対象: `tests/ml/paste_volume/test_task.py:849-901`（`_task_batch`）
- `PasteVolumeBatch` を手で組んでいるので、
  - 「bucket 違い 2 種を続けて通せる」（`:1023-1044`）は bucket 機構を通っておらず、
    単に shape の違う tensor を 2 回流しているだけ
  - `conditioning = math.log(PIXEL_PER_MM)` という規約を literal で書き直しているので、
    collator 側が `log(pixel_per_mm * scale)` から別の量へ変わっても気づけない
  - `valid_pixel_mask` の極性（True = 有効）も literal 再宣言
- `tests/ml/paste_volume/helpers.py` に合成 session の材料があり、
  `PasteVolumeCollator.collate` を通した batch を 1 本だけ task へ流す経路は組める。
  M3（conditioning 無検査）とまとめて塞げる。
- 確信度: 高（読み取り + 測定）

### S8. 仕様書 §2 の「512 × 512 入力で 1.5 GMAC 以下」が測られていない（実測は超過）

- 対象: `tests/ml/paste_volume/test_model.py:43`（`GIGA_MULTIPLY_ACCUMULATE_BUDGET = 1.5`）、
  `:260-268`（`test_stays_within_the_computation_budget_at_the_largest_crop` は 159px × 5 view）
- 実測（`measure_paste_volume_model`）:

  | 入力 | GMAC |
  | --- | ---: |
  | 53 × 53 × 1 view | 0.031966 |
  | 53 × 53 × 5 view | 0.159780 |
  | 159 × 159 × 5 view | 1.307149 |
  | **512 × 512 × 1 view** | **2.677027** |
  | 512 × 512 × 5 view | 13.385085 |

- 仕様書 §2 は「512 × 512入力で1.5 GMAC以下であることを実装時に計測する」と
  「159 px × 5 viewが計算量上限に最も近い構成」（1.31 GMAC）を**両方**書いていて自己矛盾している。
  `ImageConstraints()` は `maximum_size=512` / `maximum_pixels=262144`（= 512²）なので
  512 × 512 は前処理が実際に出しうる形。
- 実害: 計画書 step 5 の 4 番「1.5 GMAC の上限を超えたら学習を始めずに理由を返す」が
  **どの shape で測るのか未決のまま step 5 へ渡る**。512 × 512 で測る実装にすると
  全 run が学習前に拒否され、159px で測る実装にすると gate が原理的に発火しない。
  実装者の判断ログ 2 は「step 5 で必要になった時点で置く」としているが、
  この矛盾は step 2 で測った側が記録すべき。
- 提案: 申し送りへ上表を残し、「gate は点塗布 crop の上限（159px × 5 view）で測る」
  という解釈を明記する（または仕様書 §2 の 512 × 512 の記述を訂正する）。
- 確信度: 高（測定済み）

### S9. trunk 死の観測器が捕まえるのは「batch 全体の完全な死」だけ

ユーザー質問「死んでいるのに spread が出る経路、死んでいないのに spread が 0 になる経路」への回答。

- 対象: `tests/ml/paste_volume/test_model.py:112-134`、`:271-294`
- 実測:
  - 100 seed の spread は min 3.257e-3 / p05 4.653e-3 / median 1.851e-2 / max 4.076e-2、
    0 は 0 件。**`== 0.0` の完全一致は float の境界に乗っておらず余裕がある**（良い）
  - 入力（`torch.rand` の一様乱数）でも encoder feature は sample 間で動く
    （次元ごとの max-min の平均 6.592e-2 / 最大 1.838e-1）。
    仕様書 §2 の「全 0 でない feature を与えたとき」の条件は満たしている（良い）
  - **死んでいるのに spread が出る経路は無い。** trunk が全 unit・全 sample で死ぬと
    hidden が全 0 になり `log_variance` は bias 一定になる（実測: `_kill_the_trunk` 後の
    `log_variance` の unique 値は 1 個、`mean` も 1 個）
  - **ただし「ほぼ死んでいる」は見えない。** 128 unit のうち 1 unit だけ生かすと
    spread 7.651e-5 で、観測器は「生きている」と判定する。seed 0 の実際の生存数は 71/128
  - **死んでいないのに spread が 0 になる経路はある**: clamp 窓を潰す
    （`log_variance_minimum=-1e-6, log_variance_maximum=1e-6`）と、trunk が生きていても
    spread は 0.000e+00 になる。ただしこの向きは本体テストが**落ちる**方向なので
    黙って緑にはならない（自己検査だけが偽の理由で通る）。S3 と併せると、
    clamp 範囲を無検査で変えられる現状はこの経路を開いたままにしている
- 結論: 仕様書 §2 申し送り 2 が要求する「hidden 層が batch 全体で死ぬ」の検出としては
  **観測器は妥当**。「0/100」という記録が「trunk はほぼ生きている」まで含意すると
  読まれないよう、申し送りに「完全な死のみを測った」と付記するのが安全。
- 確信度: 高（測定済み）。深刻度: 低〜中（記録の粒度）

### S10. docformatter が日本語を崩した箇所が新規 6 件

`memory/MEMORY.md` の `docformatter-corrupts-japanese` に該当。step 0/1 の S8 と同じ型が再発。

- `tests/ml/paste_volume/test_model.py:3`「…を実測で 留める。」
- 同 `:96`「…初期化由来か入力由来か 分けられない。」
- 同 `:119`「…bias 一定になり、 trunk」
- `tests/ml/paste_volume/test_task.py:3`「…sample ID の 並びが」
- 同 `:862`「…正規化して消すので、 空間構造で差を付けないと」
- 同 `:1061`「全 有効 mask では …」

（`test_task.py` の `:85 / :283 / :382 / :383 / :491 / :626 / :627 / :648 / :661 / :782` は
step 0/1 由来で本 MR の diff 外。並行 agent の S8 修正と衝突しないよう分けて直すこと。）

- 確信度: 高。`ruff format --diff` / `docformatter --diff` はいずれも無変更なので、
  手で直しても再整形はされない（`docformatter --diff` は本 5 file で 0 件）

### S11. テスト helper の重複

- `_model(seed=0)` が `test_model.py:81-86` と `test_task.py:840-846` に逐語で 2 本。
- `PADDING_PIXEL_NAME = "_encoder._encoder._padding_pixel"` も両 file に。
- `tests/ml/paste_volume/helpers.py` が既にあり、計画書もここを共有場所として指定している
  （「共有して衝突するのは 2 ファイルだけ … `tests/ml/paste_volume/helpers.py`」）。
- 確信度: 高（読み取り）

---

## nit

- **N1.** `zero_target_count`（`MeanSaturationDiagnostic` 由来、真値 0 の件数）と
  `zero_target_sample_count`（`ZeroTargetMetrics` 由来、同じく真値 0 の件数）が
  同じ 26 本の中に並び、`test_reports_the_diagnostics_when_no_regression_metric_can_be_measured`
  は両方に `== 4` を書いている。今は衝突していない（実測で確認）が、
  `ZeroTargetMetrics` が将来 `count` field を得ると `zero_target_count` が衝突する。
  本数 26 の assert が捕まえるので機構としては守られている。
- **N2.** `apply_fine_tune_freeze` の docstring（`model.py:144-145`）は
  padding pixel を凍結しない理由を「fine-tune 先の Raspberry Pi では撮像条件が変わり
  padding 領域の扱いを学び直す必要があるため（仕様書 §2 の fine-tuning 範囲）」と書くが、
  仕様書 §2 は learnable padding pixel を更新対象に**挙げているだけで理由は書いていない**。
  実装者の推測なら出典を分けたほうがよい。
- **N3.** 仕様書 §2 の fine-tuning 範囲は「residual stage 3 を更新／stem・stage 1・2 を freeze」で、
  v1 は 2 stage しかない。計画書がこれを「最終 stage」と読み替えた経緯が
  code にも test にも残っていないので、仕様書と code を突き合わせた読者には矛盾に見える
  （`FROZEN_PARAMETER_NAMES` は `_stages.0.*` だけを凍結している）。
  `apply_fine_tune_freeze` の docstring に 1 行入れると読める。
- **N4.** `ml.model.loss.validate_gaussian_inputs` は repo 内のどの task からも呼ばれていない
  （`GaussianRegressionTask` も同様なので既存の形の踏襲）。
  `PasteVolumeTask` は判断ログ 7 で「batch の形を検算しない」と決めているが、
  `sample_weight` 合計 0 → loss が nan、といった値の異常は model の入力検査でも捕まらない。
  実データでは weight が `1/session_sample_count > 0` なので現状は起きない。
- **N5.** `tests/ml/paste_volume/test_task.py` が 1,183 行になり、
  module 定数と helper（`:820-946`）が test class の間に挟まっている。
  `TrainingData` の契約と `TrainingTask` の契約で file を分ける余地がある
  （src は 1 module なので tests ミラー規約とはトレードオフ）。
- **N6.** `_as_float_mapping` は `ml.training.task` の複製（計画書 R6 で許容済み）だが、
  ドメイン側だけ `prefix` 引数を持つ。2 本が別々に育つ形になっている。
- **N7.** 仕様書 §2 の「約 150 万 parameter 以下」は直接の assert が無い
  （395,048 の完全一致が含意するので実害なし）。

---

## 良かった点（変異で効いていることを確認した箇所）

- **`ZeroTargetMetrics` の接頭辞の判断は正しく、`reduce` の 26 本に漏れも重複も無い。**
  実測: field 名の衝突は `sample_count` / `weight_sum` / `mean_absolute_error` /
  `one_standard_deviation_coverage` のちょうど 4 本
  （`GaussianRegressionMetrics` ∩ `ZeroTargetMetrics`。`MeanSaturationDiagnostic` は
  他のどちらとも衝突しない）。接頭辞を外すと `values.update` の順序どおり
  **blank 側の値が全体の値を上書きし、本数が 22 へ落ちる**（変異で 3 件失敗、
  うち 1 件が `assert 22 == 26`）。26 本を実際に列挙して重複・欠落 0 を確認した。
  `ZeroTargetMetrics` を丸ごと落とす変異も 3 件で落ちる。
- **`padding_pixel` の勾配観測は分離が良い。** 実測: `invalid_columns=0` で
  厳密に 0.000000e+00、1 列でも 3.03e-2、4 列で 4.36e-2。境界に乗っていない。
  自己検査（padding 無しで 0）と本体の対も成立している。
- **compile parity の `checked_gradient_count == 43` は別経路で満たされない。**
  `_compare_gradients` は「両方 None の勾配」だけを飛ばして数えるので、43 は
  「43 本すべてを実際に突き合わせた」と同義。テストが 43 を literal ではなく
  `sum(1 for _ in _model().parameters())` で導いているので config 変更にも追従する。
  なお padding の有無に依らず 43 になる（`torch.where` は mask 全 True でも
  zeros の勾配を返す）ので、`invalid_columns=4` は 43 の成立条件ではない。
- **freeze の集合は正しく、変異で確実に落ちる。** 実測: 21 tensor / 86,368 要素を凍結、
  学習対象 308,680 要素、`padding_pixel.requires_grad` は True。
  凍結対象を最終 stage 側へ反転させる変異で 3 件、prefix を誤らせる変異で 4 件が落ちる。
- **config の実測固定は強い。** parameter 数 395,048（tensor 43 本）・total_stride 8・
  GMAC 0.159780 / 1.307149 はすべて再現。parameter 数を保つ config 誤り
  （`group_norm_groups=4`、`stem_strides=(1,4)`）も group 数の否定テストと GMAC が拾う。
- **過学習テストは「画像を見ていない model」を確実に落とす**（比 0.842 = 定数予測の床）。
  cosine 減衰は判定の敷居を下げていない。
- diff は計画書のレーン A（`model.py` / `task.py` / `test_model.py` / `test_task.py` /
  `test_architecture.py`）に完全に収まっており、要求外の変更は 1 行も無い。
- 成果物汚染（`</content>` 等）は 4 file とも無し。codespell も 0 件。

---

## 変異マトリクス

`.review-mutants/mut/<name>` 上で実測。対象は
`tests/ml/paste_volume/test_model.py` + `test_task.py`（74 passed / 2 skipped が baseline）。

| # | 変異 | 落ちたテスト |
| --- | --- | ---: |
| 1 | `evaluation_step` の `torch.no_grad()` を外す | **0**（M1） |
| 2 | `compile_forward` を `self._forward = self._model` へ | **0**（M2） |
| 3 | `conditioning` を `zeros_like` へ差し替え | **0**（M3） |
| 4 | `conditioning` に `batch.target` を渡す（真値 leak） | 1（単調性が偶然崩れただけ、M3） |
| 5 | `build_paste_volume_model` が `fine_tune` で freeze する | **0**（S1） |
| 6 | `MODEL_FAMILY` を改名 | **0**（S2） |
| 7 | `log_variance_minimum/maximum` を -30 / 30 へ | **0**（S3） |
| 8 | `apply_fine_tune_freeze` の stem 検査を削除 | **0**（S4） |
| 9 | 同 padding pixel 検査を削除 | **0**（S4） |
| 10 | `reduce` の `predictions.validate()` 早期 return を削除 | **0**（S5） |
| 11 | `_STEM_PREFIX` を誤った値へ | 4 |
| 12 | `_PADDING_PIXEL_NAME` を誤った値へ | 4 |
| 13 | `_ZERO_TARGET_PREFIX` を空へ | 3 |
| 14 | `reduce` から `ZeroTargetMetrics` を削除 | 3 |
| 15 | 凍結する stage を最終 stage へ反転 | 3 |
| 16 | `mean_bias_initial` を 1.0 へ | 3 |
| 17 | `hidden_features` を 64 へ | 3 |
| 18 | `blocks_per_stage` を (2,3) へ | 5 |
| 19 | `group_norm_groups` を 4 へ（parameter 数・GMAC 不変） | 1 |
| 20 | `stem_strides` を (1,4) へ（parameter 数・total_stride 不変） | 2 |
| 21 | `training_step` の images を `zeros_like` へ | 2 |

## 検証結果（`4bd6a82` を凍結した複製に対して container 内で実行）

- format: **pass**
  - `ruff check`（0.8.4、repo の pyproject 設定）: All checks passed
  - `ruff format --diff`: 5 files already formatted
  - `docformatter --diff`（`--wrap-summaries=79 --wrap-descriptions=72`）: 差分なし
  - `codespell`: 0 件
  - `pre-commit run -a` は**実行していない**（in-place 書き換えが並行 agent の作業ツリーを
    壊すため、対象 file へ同等の hook を個別に当てた）
- 型検査（pyright、`src/ml` + `tests/ml`）: **pass**（0 errors / 0 warnings / 0 info）
- `tests/ml`（`-m "not hardware and not e2e"`）: **pass**（1536 passed, 3 skipped, 62.9s）
  - 実装者報告の 1538 passed / 1 skipped との差は、凍結複製に `data/` を含めていないため
    実 5 session の opt-in テスト 2 件が skip へ回ったぶん（1536 + 2 = 1538）
- `make test` / `make run` / `pytest -m hardware` は実行していない
- 実行環境: 作業ツリーで並行 agent が step 0/1 の指摘修正中のため、レビュー対象を
  `.review-mutants/base/`（`4bd6a82` 相当）へ凍結し `--workdir` + `PYTHONPATH` で
  差し替えて計測した。`.review-mutants/` は削除済み、repo は無変更
