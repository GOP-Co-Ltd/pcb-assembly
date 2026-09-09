# 点塗布・多視点への `src/ml/` 適応 レビュー

対象: `feature/2026-09-08/ml-point-dispense-model`（`git diff --cached main`）
計画書: `memory/agents/implementation-planner/ml-point-dispense-model.md`

## verdict: request-changes

指摘は 2 件の must-fix と 6 件の should-fix。いずれも src の書き直しではなく、
テストの追加 1 件・配線 1 行・doc 同期・防御の追加で閉じる。実装の骨格
（多視点 model、bucket 鍵、ReLU 化と `mean_bias_initial`、診断 2 種、predict 検査）は
計画・裁定どおりで、層の分離もドメイン非依存も守られている。

## must-fix

### M1. 全 sample 飽和時に、追加した飽和診断の「値」が運用者へ届かない

- 対象: `src/ml/training/loop.py:547-552` と `src/ml/training/task.py:221-253` の噛み合わせ
- 問題: `reduce` の docstring は「空の写像を返すと運用者が受け取るのは Trainer の
  『monitor がありません』だけになり、真の原因である飽和が読み取れなくなるため」と
  書いている。しかし `loop.py:548` の `raise` は `state.validation_metrics`（554）と
  `self._logger.log_metrics`（567）より**前**にある。全 sample 飽和した epoch では
  6 項目は計算されたまま捨てられ、例外文に出るのは `sorted(metrics)`＝**key 名だけ**。
- 再現: 平均が全 sample 0 になる model で Trainer を 1 epoch 回す →
  `ValueError: validation の集計に monitor 'relative_error_score' がありません:
  ['positive_target_count', 'saturated_positive_count', ...]`。値は出ない。
  初回 validation で起きるので、それ以前に log された epoch も存在しない。
- 根拠: `test_a_fully_saturated_evaluation_still_reports_why` は `reduce` 単体を見ており、
  Trainer 経路で値が surface することは誰も見ていない。
- 対処案（どちらか）: 例外文へ値を載せる / `log_metrics` を monitor 検査より前に出す。
- 確信度: 高 ／ 深刻度: 中〜高
- 注: orchestrator 裁定の文言（「`reduce` が常時返す」「Trainer は変えない」）には
  形式上従っている。裁定の意図をどこまでと読むかは orchestrator の判断。

### M2. `ViewDropout.view_indices_for` の中核 2 挙動をテストが観測していない（変異 2 件が実測で生存）

- 対象: `src/ml/data/batch.py:90-95`、`tests/ml/data/test_batch.py::TestViewDropout`
- 実測: `ViewDropout.view_indices_for` をクラスごと差し替える pytest plugin
  （`src/` は無改変、`-p` で読み込み）で 2 変異を当て、いずれも `tests/ml` 全体が
  **1296 passed / 1 skipped** のまま生存した。
  - 変異 A: `count = generator.randint(minimum_view_count, available_view_count)`
    → `count = self.minimum_view_count`。batch ごとの view 数ランダム選択が消え、
    既定 `minimum_view_count=1` では**常に 1 view**へ落ちる
  - 変異 B: `generator.sample(...)` を loop の外へ出し、batch 内の全 sample が
    同じ view 部分集合を使う
- 根拠: `TestViewDropout` 10 本は「長さが揃う」「昇順・重複なし」「範囲内」「再現性」
  「epoch 差」「sample_ids 依存」しか見ていない。
  `test_the_kept_count_stays_between_the_minimum_and_what_is_available` は
  `2 <= len(indices[0]) <= 5` の**範囲 assert のみ**で、計画の
  「値の範囲だけを見るテストは置かない」に反する唯一の箇所。
- 影響: 変異 A が入るとユーザー決定 3（1〜5 view のどれでも使える）の学習側が
  壊れるのに suite は緑。
- 対処案: 固定 seed / epoch 列で `len(row)` が 2 値以上を取ることと、
  同一 batch 内で row が一致しないことを厳密に固定する。
- 確信度: 高（実行で確認）／ 深刻度: 中
- 注: 実装ノートの「最終 161/161 killed、真の survivor 0 件」と食い違う。

## should-fix

### S1. 設計 doc が実装と矛盾したまま

- `docs/image-based-dispense-calibration.md:155`「mean head の最終出力には Softplus を適用する」
- `docs/image-based-dispense-calibration-ml-plan.md:341, 374`（Softplus）、
  `:236-239`（最小 32 px / 最大 1024 px / stride 32）、`:298`（scale `[0.8, 1.2]`）、
  `:741`（許容 op 一覧に `Softplus`）、`:1056`（最小 32 px、最大辺 1024 px）
- 本 MR がこれら全ての値を変えている。担当は `code-simplifier`（docs 同期を兼務）。
- 確信度: 高 ／ 深刻度: 中

### S2. `MeanSaturationDiagnostic.measure` が docstring の前提を強制しない

- 対象: `src/ml/evaluation/regression.py:296-320`
- docstring は「先に `GaussianPredictions.validate` を通す」と書くが `measure` は呼ばない。
  兄弟の `ZeroTargetMetrics.measure` は委譲して `(None, reason)` を返す。
- 長さが `(n,)` と `(1,)` の組み合わせだと broadcast して例外も出ず、
  `saturated_positive_count > positive_target_count` になって fraction が 1.0 を超えうる。
- 確信度: 高（コード読解）／ 深刻度: 低〜中

### S3. `MultiViewImageEncoder` が mask の B / V 取り違えを検出しない

- 対象: `src/ml/model/multiview.py:54-64`、`_reject_invalid_inputs`（100-115）
- 実測: `images [2,3,6,32,32]` と `mask [3,2,1,32,32]` を渡すと、`flatten(0,1)` 後は
  どちらも 6 になるので `ImageEncoder` の shape 検査も通り、**誤った view へ mask を
  当てたまま `(2, 16)` を返す**（コンテナで実行確認）。
- 「同じ不整合へ検出器を重ねない」裁定とは衝突しない。`ImageEncoder` からは
  平坦化後の `B*V` しか見えず、B と V の一致は multiview にしか観測できない。
- `blocks.py:314` と同じく SymInt のまま `valid_pixel_mask.shape[:2] != images.shape[:2]`
  で比較すれば export も壊れない。
- 確信度: 高（実行確認）／ 深刻度: 低（`MultiViewPaddedBatch.pad` 経由なら起きない）

### S4. `ImageConstraints.validate_for_encoder` が `encoder.validate()` へ委譲しない

- 対象: `src/ml/data/image.py:115-128`
- 同じ file の `validate_augmentation` は `augmentation.validate()` へ委譲している
  （実装ノート 6）。`validate_for_encoder` は委譲しないので、`stem_strides=(0,)` のような
  壊れた config は `total_stride == 0` → `16 < 0` が偽 → **None（合格）** を返す。
- 確信度: 中（検証済み config を渡す運用なら起きない）／ 深刻度: 低

### S5. `ml.data` → `ml.model` の依存を本 MR で新設した

- 対象: `src/ml/data/image.py:32`（`from ml.model.blocks import ImageEncoderConfig`）
- 層テストは両者 RUNTIME なので通るが、data 層が model 層を知る向きは本 MR が初めて。
  `ImageEncoderConfig.validate_for_constraints(constraints)` として model 側へ置けば
  既存の向き（model → data）のまま済む。
- 計画書の公開 IF 案が明示的にこの向きを選んでいるので蒸し返しではなく記録として。
- 確信度: 中 ／ 深刻度: 低

### S6. `AugmentationRange` の上限側に合成検証が無い

- 対象: `src/ml/data/image.py:93-113`
- `validate_augmentation` は下限だけを見る。`smallest_source_size * maximum_scale`
  が `maximum_size` を超える設定は sample を落とさない代わりに `_applied_scale` の
  `limit` で黙って clip され、**設定した振り幅が出ない**。E6 は下限側だけを扱うので
  計画どおりではあるが、非対称は残る。
- 確信度: 高 ／ 深刻度: 低

## nit

- N1. docformatter の連結痕（不自然な空白）がテスト 3 file に 5 箇所。
  `tests/ml/data/test_image.py:298`、`tests/ml/model/test_heads.py:112,161`、
  `tests/ml/model/test_multiview.py:104,486`。`src/ml` は clean。
  既存 file にも同型の痕があるので本 MR 固有ではない。`</content>` 等の混入は無し。
- N2. `tests/ml/model/test_multiview.py:279` の `assert bool((mean >= 0).all())` は
  ReLU では恒真。同 file の `test_matches_the_encoder_and_head_applied_in_order` が
  実質を見ているので害は無い。
- N3. `MultiViewGaussianRegressor` は `GaussianImageRegressor` と `__init__` /
  `forward` が字面まで同一（型注釈だけ違う）。抽象を足さない方針とのトレードオフ。
- N4. `_validate_batch_inputs` の検査順が変わり、mask の device 検査が
  dtype / shape 検査より前になった。両方壊れた入力で返る理由が変わる。
- N5. `GaussianHeadConfig.mean_bias_initial` の既定 1.0 は、この用途の真値スケール
  0.05〜0.2 µL に対して 5〜20 倍。docstring に「用途のスケールへ上書きする」旨が
  あるとドメイン側が迷わない。
- N6. `BatchShape.view_count` に検証が無い（`view_count=0` はコスト 0 になり
  無制限に詰まる）。`height` / `width` も未検証なので既存作法どおり。

## ユーザーの重点観点への回答

1. **公開 IF**: 計画・裁定と一致。計画に無い src の追加は `_target_shape` /
   `_transformed_images` / `_valid_pixel_mask` / `_validate_batch_common` /
   `_reject_mixed_placement` / `_reject_invalid_mask` / `_as_float_mapping` の
   私有ヘルパーだけで、いずれも単視点／多視点の共有のため。スコープ逸脱なし。
2. **層**: `RUNTIME_MODULES` へ `ml.model.multiview` 追加済み。実 import は
   `ml.model.blocks` / `ml.model.heads` のみで RUNTIME 内に閉じる。向きの件は S5。
3. **変異 3 件の判断**: いずれも妥当。`int()` は ONNX の `dim_param` が検出器に
   ならないので `torch.export(strict=False)` の対テストが正しい。`.amax` は
   「順序不変 + 同一 view 反復」が平均を一意に決めないという診断が正しく、
   `test_aggregates_the_views_by_their_arithmetic_mean` で解消済み。
4. **trunk ReLU を別 MR へ回した判断: 妥当**。計画 論点 2 の production encoder
   （stride8_A、`output_features=96`、`hidden_features=128`、入力 `[8,5,6,53,53]`）で
   100 seed 実測し、trunk 出力の batch 全滅 0/100、encoder feature の sample 間分散 0 が
   0/100、head 平均が sample 間一定 0/100、平均の batch 全滅 0/100。
   実装者が見た 1/40 は `test_multiview.py` の合成 encoder（`output_features=16`、
   `hidden_features=8`）固有で production を代表しない。
   ただし encoder の形はドメイン設定なので、`ml` 側にこの安全性を固定するテストは
   置けない。**encoder 形を決めるドメイン MR で 1 度実測する**申し送りが要る。
5. **ドメイン非依存**: 53 / 5 view / µL は `src/ml` に無い（grep 確認）。
   `ImageEncoderConfig` は形の既定値を持たないまま。`mean_bias_initial` の既定だけ N5。
6. **既定値変更**: `tests/ml/support.py` は `stride=BATCH_STRIDE` を明示しており無影響。
   契約 pin 側は更新済み。`test_is_limited_by_the_pixel_budget` が新既定では機構を
   観測できなくなる点に気付いて制約を明示した対応（spec ノート F3）は正しい。
7. **テスト品質**: モック 0、`src` の private 参照 0、`class TestXxx` 集約。
   範囲だけを見るテストは M2 の 1 本と N2 の 1 行。
   `test_the_mean_head_is_never_born_dead`（32 seed）は有効な検出器で、
   bias 初期化を無効化する変異で **29 failed** を確認した。
8. **スコープ**: 逸脱なし。単視点 `PaddedBatch` へのテスト追加は変異実験由来の補強で、
   src の挙動は変えていない。

## ユーザーへの質問

1. **blank と log 分散の下限の相互作用**。ReLU で真値 0 を厳密に当てられるようになった
   結果、blank sample の NLL は `0.5 * log_variance` だけになり、`log_variance_minimum`
   （既定 -14）まで押し下げるのが最適になる（per-sample で -7）。log 分散 head は
   trunk を正の真値の sample と共有するので、blank 比率が高いと分散推定が歪みうる。
   blank の loss weight を下げる、あるいは blank を分散学習から外す運用を想定しているか。
2. **`GaussianHeadConfig.mean_bias_initial` の既定 1.0** は真値スケール 0.05〜0.2 µL に
   対して 5〜20 倍。ドメイン側が必ず上書きする前提でよいか（N5）。
3. **M1 の扱い**。「Trainer は変えない」裁定を維持したまま、例外文へ飽和診断の値を
   載せる／`log_metrics` を monitor 検査より前に出す、のどちらを採るか。

## 検証結果

この機体には pcbnew / picamera2 が無いため `make test-no-hardware` は collect できない。
コンテナ（`pcb-assembly-ml-ml-1`）で実行した。実機テストは実行していない。

- `pytest tests/ml -m "not hardware and not e2e"`: **pass**（1296 passed / 1 skipped / exit 0）
- `pyright src/ml tests/ml scripts/ml_smoke.py`: **pass**（0 errors / 0 warnings）
- `make format`: 未実行（`.git` ロック競合を避けるため。実装者側で pre-commit 全 hook Passed の報告あり）
- 追加で実施した変異 3 件（`src/` は無改変、pytest plugin でクラス差し替え）
  - `ViewDropout` の view 数ランダム選択を無効化 → **survivor**（1296 passed）
  - `ViewDropout` の view 部分集合を batch 共通化 → **survivor**（1296 passed）
  - `mean_bias_initial` の bias 初期化を無効化 → **killed**（29 failed）
- 追加で実施した実測 2 件（一時 script、実行後に削除。working tree は clean）
  - production encoder 100 seed の trunk / feature / 平均の全滅回数 → いずれも 0
  - mask の B / V 取り違えが通ることの確認 → 通った（S3）

---

# 2 巡目レビュー（差し戻し対応後）

## verdict: request-changes

1 巡目の must-fix 2 件・should-fix 6 件・nit 6 件は**すべて解消を確認した**（うち 4 件は
変異を再実行して killed を実測）。差し戻しの対応そのものに異論は無い。

request-changes は新規 3 件による。**src の変更は不要で、テスト 1 本と docs 2 箇所**。

## 1 巡目の指摘の解消状況

| # | 状態 | 確認方法 |
| --- | --- | --- |
| M1 | **解消** | `loop.py:548-565` で log が raise の前。`test_a_fully_saturated_run_logs_why_before_it_fails` が Trainer 経路で値の到達を見ている。例外文を key 名だけへ戻す変異を当てて **killed** を実測 |
| M2 | **解消** | 私の 2 変異 + `count = available` の 3 件を再実行し、いずれも新テストで **killed**（M2-A → `test_the_kept_count_varies_between_batches` が `{2} == {2,3,4,5}` で落ちる／M2-B → `test_each_sample_gets_its_own_subset_of_views` が `1 == 3` で落ちる／M2-A2 → 4 本が落ちる） |
| S1 | **部分解消** | 指摘した行はすべて同期済み。ただし下記 N-2 / N-3 が残る |
| S2 | 解消 | `MeanSaturationDiagnostic.measure` が `validate()` を通し `ValueError`。`reduce` と `DiagnosticReport.build` はどちらも先に `validate()` を見ているので raise しない（確認済み） |
| S3 | **解消** | `shape[:2]` の SymInt 比較。検査を落とす変異を当てて `test_rejects_a_mask_whose_batch_and_view_are_swapped` で **killed** を実測 |
| S4 | 解消 | `validate_for_constraints` が先に `self.validate()`。`test_reports_a_broken_encoder_instead_of_passing_it` が理由文を完全一致で固定 |
| S5 | 解消 | 下記「S5 の判断」参照 |
| S6 | 解消 | 上限側検証あり。境界 parametrize が 512.0 / 512.5 / 513.0 で `floor` と `ceil` を割る |
| N1 | 解消 | 変更 file の連結痕は 0。`test_loop.py:863` は本 MR の変更行でないことを diff で確認 |
| N2 | 解消 | 恒真 assert 削除 |
| N3 | 未対応（意図どおり） | 抽象を足さない方針 |
| N4 | 未対応（nit のまま） | 検査順の入れ替わり |
| N5 | 解消 | docs の申し送り 1 に明記 |
| N6 | 解消 | `BatchShape.validate()` を 1 field 1 ケースで固定 |

## 依頼された独立確認

### `learning_rate` / `epoch_seconds` の意味変更は無害か → 無害

- **消費者ゼロ。** `epoch_seconds` / `train_samples_per_second` を読む箇所は repo 全体で
  `loop.py` 自身の log 呼び出しだけ（`grep` を `src/` `tests/` `scripts/` `docs/` へ実施）。
  時間予算の見積もりに使う経路は無い
- **deadline は無関係。** `deadline_monotonic = started_monotonic + config.deadline_seconds`
  （`loop.py:393-396`）で、判定は `_deadline_passed` が `time.monotonic()` と直接比較する
  （`loop.py:802`）。`epoch_seconds` は入らない
- **むしろ過去のレビューが望んだ方向。**
  `memory/agents/code-reviewer/ml-core-4-training-experiment.md:219` が
  「`train_samples_per_second` の分母 `epoch_seconds` に validation とチェックポイント
  書き出しの時間が入る」を積み残しとして挙げていた。今回の移動は checkpoint 書き出しを
  分母から外すので、退行ではなく部分的な改善
- **第 3 の意味変更は無い。** `log_metrics(step=state.progress.global_step)` は
  `with_completed_epoch()` より前へ移ったが、`with_completed_epoch()` は
  `epoch` / `epochs_completed` / `next_batch_index` / `batch_plan` だけを変え
  **`global_step` を変えない**（`checkpoint.py:115-123`）。step 軸は移動前後で同一

### 事故（`git restore` による巻き戻し）の残骸 → 無し

- 1 巡目で読んだ src の内容がすべて残っていることを確認
  （`mean_bias_initial` / `_mismatched_inputs` / `MeanSaturationDiagnostic` の 2 箇所 /
  `PreprocessedMultiViewSample` / `nn.ReLU()`、および `validate_for_encoder` が
  `image.py` から消えて `blocks.py` へ移っていること）
- `MUTATION` / `mutplugin` / `_probe` 等の痕跡は `src/` `tests/` `docs/` に 0
- working tree に未追跡ファイル無し、`git diff`（unstaged）も空
- `tests/ml/data/test_image.py` から `_encoder_config` と `ImageEncoderConfig` の
  import が**両方**消えており、移設に伴う死んだヘルパーも残っていない

### S5 の判断（`ml.model` → `ml.data`、`TYPE_CHECKING`）→ 妥当

- 前提の訂正を受け入れる。`ml.model` → `ml.data` も本 MR 以前には無かった
- 消費側（encoder）が入力契約を知る向きのほうが自然で、`ml.training.data` → `ml.data.split`
  という既存の向きとも一貫する
- `TYPE_CHECKING` に置く判断も妥当。`ml.model.blocks` が実行時に `ml.data.image` を読むと
  `ml.model.*` 全体が torchvision を道連れにする。`_loaded_dependencies` は実行時 import を
  測るので、層テストが通ることと実際の import 重量は別問題という指摘は正しい
- 循環も無い（`image.py` 側の import は削除済み）

## 新規指摘

### N-1（must-fix）`MultiViewPaddedBatch` の画像配置を観測するテストが 1 本も無い

- 対象: `tests/ml/data/test_batch.py::TestMultiViewPaddedBatchPad`
- **実測（契約由来変異の前向き検証として実施）**: `pad` の docstring の主張
  「配置位置は sample 内の全 view で共有する」を反転する 2 変異を作った。
  - MUT-P2（view ごとに配置をずらす）→ **killed**（`test_places_every_view_of_a_sample_at_the_same_position`）
  - MUT-P1（mask は正しい位置、image だけ常に左上へ置く）→ **`tests/ml` 全体 1311 passed で生存**
- 根拠: `TestMultiViewPaddedBatchPad` の全テストが `valid_pixel_masks` だけを観測しており、
  `batch.images` は `shape` しか見ていない（grep で確認）。単視点側には
  `test_valid_mask_marks_exactly_the_placed_pixels` があるのに、多視点側に対応物が無い
- 影響: この欠陥が実在すると mask が「画像の無い位置」を有効と記録し、
  `replace_invalid_pixels` が本物の画素を learnable padding pixel へ置換する。
  学習は進むが黙って壊れる
- 補強案: 既知の画素値を置き、`batch.images` の非零位置が `valid_pixel_masks` と
  全 view で一致することを見る
- 確信度: 高（実行確認）／ 深刻度: 中
- 注: M2 と同型（新設した公開機構に対する検証済み survivor）なので、1 巡目と同じ区分にした。
  直すのはテスト 1 本

### N-2（should-fix）docs の v1 encoder 表が、同じ doc の新しいサイズ契約と矛盾する

- `docs/image-based-dispense-calibration-ml-plan.md:372-387` の v1 encoder は
  **総 stride 32 / 出力 160 次元**のまま。同じ doc の 233-239 行は本 MR で
  「最小 16 px / stride 単位 8 px」へ更新済み
- **この組み合わせは本 MR が新設した `ImageEncoderConfig.validate_for_constraints` が
  弾く**（`minimum_size 16 < total_stride 32`）。doc が自分の中で矛盾している
- さらに 388 行以降の申し送りは「上記 v1 encoder（`output_features=96`、
  `hidden_features=128`）」と書いており、上記の表（160）と一致しない
- 計画書 論点 2 の stride8_A（24/32/48/96、total_stride 8、out 96）へそろえるのが自然
- 確信度: 高 ／ 深刻度: 中

### N-3（should-fix）docs に多視点が 1 語も無い

- `多視点` / `[B, V` / `view_count` / `ViewDropout` / `view dropout` は
  両 doc とも **0 件**（grep）
- ml-plan の §2 入出力（327-334 行）は `image_6ch: float32 [B, 6, H, W]` /
  `valid_pixel_mask: bool [B, 1, H, W]` の単視点のまま
- 要件 doc は逆向きに古い。198 行「複数視点入力は初期達成条件には含めない」、
  210 行「初期実装では基準位置の central view だけを撮影する」、
  403 行「初期 WebUI は offset (0,0) の view 0 だけを撮影する」。
  MR !207 の収集方式変更とユーザー決定 3 に反する
- S1 は私が列挙した行だけを直した形になっており、**本 MR の主題そのもの**（多視点 I/O、
  view dropout、batch 内 V 均一）が doc に入っていない。次のドメイン MR が読む先として問題
- 要件 doc 側を変えるかは製品判断なのでユーザーへ回す
- 確信度: 高 ／ 深刻度: 中

### N-4（nit）失敗 epoch の log が再開時に重複する

- monitor を欠いて raise する epoch でも log が残るようになった一方、その epoch は
  `with_completed_epoch()` にも `_save(state, "latest")` にも到達しない。
  同じ checkpoint から再開すると**同じ `global_step` へ 2 回 log される**
- MLflow は同一 step の重複を許すので実害は小さい。移動前は起こらなかった副作用として記録
- 確信度: 高 ／ 深刻度: 低

### N-5（nit）`test_each_sample_gets_its_own_subset_of_views` は seed に暗黙依存

- `minimum_view_count=1` / `available=5` で引いた `count` が 5 になる seed では、
  部分集合が全 sample 同一になり主張が観測できなくなる（現 seed では 3 種類出るので機能する。
  外れた場合は失敗するので気付けはする）
- `len(indices[0]) < 5` を先に 1 行置くと自己説明的になる
- 確信度: 中 ／ 深刻度: 低

## sweep 盲点分析と契約由来変異への評価

**結論: 分析は正しく、対策も採るべき。ただし 3 点の補強が要る。**
特に (3) は今回もう 1 件の実害（M1）を生んでいる。

### 妥当な点

- 原因 1〜3 の切り分けは正しい。私が当てた 2 変異は実際に docstring の主張
  （「共通の view 数を**選び**」「**sample ごとの** view 番号列」）から作ったもので、
  ソースの字面からは出てこない
- 原因 3（カタログとテストが盲点を共有すると sweep は 100% を報告する）が最も価値がある。
  **変異スコアはカタログに対する相対値でしかない**という一般則を正しく言い当てている
- 原因 2（仕様側に「どう壊すと落ちるか」が無い機構が最も危険）は実務で使える優先順位付け

### 補強 1: 「docstring 1 文 1 変異」は主張を取りこぼす

- **主語を method docstring に限ると空振りする。** `MultiViewImageEncoder.forward` の
  docstring は「``[B, V, C, H, W]`` を ``[B, output_features]`` へ変換する」の 1 文だけで、
  ここを反転しても 1 巡目の実 survivor（`.mean` → `.amax`）は出ない。
  その主張は**module docstring**の「集約は平均とする」にある。
  規則は「module + class + method の主張文」を対象にする必要がある
- **1 文に主張が複数入る。** 「Batch 内で共通の view 数を選び、sample ごとの view 番号列を
  返す」は 1 文で 3 主張（batch 内で共通／view 数を*選ぶ*＝固定でない／sample ごと）。
  文ではなく**述語**を単位にする。実務上は読点で割って修飾語ごとに数える

### 補強 2: 反転は一意でない。**他の主張をすべて真に保つ最小の反転**を採る

- 「sample ごと」の反転には「全 sample 同じ」（＝ M2-B、1 巡目で生存）と
  「順序を崩す」（既存テストが殺す）がある。機械的な反転器は後者を出しかねない
- M2-A2（`count = available`）が実例。**4 本が落ちたが、そのうち
  `test_differs_between_epochs` / `test_depends_on_the_sample_ids` は
  「view 数を選ぶ」以外の主張が壊れたせいで落ちている。**狙った主張を検査していない
- 規則に「反転後も他の主張がすべて成り立つこと」を条件として書き足す。
  これを満たさない反転は killed になっても情報量がゼロ

### 補強 3（最重要）: 関数内の変異では M1 の類型が原理的に取れない

- M1（`log_metrics` が `raise` の後ろにあって値が捨てられる）は
  **`reduce` 自体は完全に正しく、呼び出し側が結果を捨てていた**という欠陥。
  `reduce` へどんな変異を当てても検出できない
- 契約由来変異でも取れない。`reduce` の docstring の主張は反転すれば殺されるし、
  `Trainer` 側の docstring にはこの順序への言及が（対応前は）無かった
- **カタログへ「呼び出し側での文の順序入れ替え」と「戻り値の破棄」を明示的に足す。**
  対象は「純関数の戻り値を、副作用のある呼び出し側がどう使うか」の境界に限れば数は少ない
- 判定の目安: **公開関数の戻り値が観測可能な出力（log / 例外文 / 成果物）へ到達することを、
  その関数のテストではなく呼び出し側のテストで固定する。**
  今回の `test_a_fully_saturated_run_logs_why_before_it_fails` がまさにその形で、正しい

### 補強 4: 生存した契約由来変異は「テスト不足」と「観測不能」を先に切り分ける

1 巡目の類型 C（観測点が機構に対して鈍い）はカタログ網羅性とは直交して残る。
`.mean` → `.amax` は、順序不変と同一 view 反復という**入力配置**では平均を一意に決められない
のであって、テストの assert が弱いのではない。変異が生き残ったとき、
**まず入力配置で差が出るかを問う**手順を入れる。

### 前向き検証の結果（この規則を別 module へ当てた）

規則の有効性を後知恵でなく確かめるため、**100% killed と報告されている
`MultiViewPaddedBatch.pad`** へ契約由来変異を当てた。docstring の
「配置位置は sample 内の全 view で共有する」から 2 変異を生成し、
**1 件が新しい survivor だった**（N-1）。

規則は機能する。ただし N-1 が示すように、**契約文から作った変異が生き残ったときに
「観測点そのものが無い」（image を誰も見ていない）ことがある**ので、補強 4 の手順は必須。

## ユーザーへの質問（1 巡目からの持ち越しと新規）

1. **要件 doc の多視点の扱い（N-3）。** `docs/image-based-dispense-calibration.md` は
   「複数視点入力は初期達成条件には含めない」「初期実装では central view だけ」と
   書いたままで、MR !207 の収集方式変更およびユーザー決定 3 と食い違う。
   初期達成条件を多視点込みへ更新してよいか、それとも「収集は多視点／初期モデルは
   central view のみ」という段階分けを維持するか
2. 1 巡目の質問 1（blank と log 分散下限）は docs の申し送り 3 へ論点として記録された。
   最初の学習 run で blank 比率と coverage の関係を確認する、という扱いでよいか

## 検証結果（2 巡目）

コンテナ（`pcb-assembly-ml-ml-1`）で実行。実機テストは実行していない。

- `pytest tests/ml -m "not hardware and not e2e"`: **pass**（1311 passed / 1 skipped / exit 0）
- `pyright src/ml tests/ml scripts/ml_smoke.py`: **pass**（0 errors / 0 warnings）
- `make format`: 未実行（`.git` ロック競合回避。orchestrator 側で pre-commit 全 hook Passed の報告あり）
- 変異の再実行（`src/` 無改変、pytest plugin でクラス／module 属性を差し替え）

| 変異 | 結果 | 殺したテスト |
| --- | --- | --- |
| M2-A（count を `minimum_view_count` 固定） | killed | `test_the_kept_count_varies_between_batches` |
| M2-A2（count を `available_view_count` 固定） | killed | 4 本 |
| M2-B（`sample` を loop 外へ hoist） | killed | `test_each_sample_gets_its_own_subset_of_views` |
| S3（B/V 一致検査の削除） | killed | `test_rejects_a_mask_whose_batch_and_view_are_swapped` |
| M1-reason（例外文を key 名だけへ戻す） | killed | `test_a_fully_saturated_run_logs_why_before_it_fails` |
| MUT-P2（view ごとに配置をずらす） | killed | `test_places_every_view_of_a_sample_at_the_same_position` |
| **MUT-P1（image だけ左上へ置く）** | **survived** | — （N-1） |

M1 の順序変異（log を raise の後ろへ戻す）は method 本体の書き換えが要るため実行していない。
`test_a_fully_saturated_run_logs_why_before_it_fails` が
`logger.metrics_for("validation/saturated_positive_fraction") == [(...)]` を要求しており、
log が raise の後ろなら空列になるので、検出は読解で確定できる（確信度: 高）。

一時 plugin / script は実行後に削除済み。working tree に残骸なし。

---

# 3 巡目レビュー（N-1 / N-2 / N-3 / N-5 対応後）

## verdict: request-changes

2 巡目の指摘は N-4 を除きすべて解消を確認した（N-4 は現状維持の判断が妥当）。
`src/` は 2 巡目から 1 行も変わっていないことを diff で確認済み。

request-changes は**新規 must-fix 1 件（検証済み survivor 2 件）**と docs 3 件による。
やはり src の変更は不要で、テスト 1〜2 本と docs の追記。

## 2 巡目の指摘の解消状況

| # | 状態 | 確認方法 |
| --- | --- | --- |
| N-1 | **解消** | MUT-P1（多視点。image だけ左上）を再実行し **killed（2 failed）**。MUT-P3（単視点。同型）も **killed（2 failed）**。どちらも `training=False` / `True` の**両ケースが落ちる**ので、配置が退化して観測不能になっていない |
| N-2 | **解消** | 数値を計画書 E1 と突き合わせて一致を確認（総 stride 8 / 出力 96 / 395,048 param / feature map 4×4・7×7・20×20 / GMAC 0.032・0.261 / `Linear(97, 128)`）。「総 stride は `minimum_size` 以下」の明記も `validate_for_constraints` の不等号と一致 |
| N-3 | **概ね解消** | 下記の取りこぼし 3 件を除き、doc の記述は実装と一致（bucket 鍵・cost 式・`[B, V, 6, H, W]`・mask の broadcast・配置位置の共有・`pixel_per_mm` に view 軸なし・batch 内 V 均一・ViewDropout・1〜5 view の推論）。**既存要件を落としていない**ことも確認（旧「初期モデルは 1 組の塗布前後画像から推定できることを必須とする」は「達成条件は 1 view でも成立させる」として保持） |
| N-4 | 現状維持を**是認** | 下記 |
| N-5 | 解消 | `len(indices[0]) < 5` あり |

### 単視点側にも同じテストを足した判断 → 妥当

依頼された 2 件の妥当性を実測で確認した。

- 私の 2 巡目の記録「単視点側には `test_valid_mask_marks_exactly_the_placed_pixels` があるのに
  対応物が無い」は**誤り**だった。あのテストは `valid_pixel_masks.sum()` を数えるだけで
  画像の位置を見ていない。訂正を受け入れる
- MUT-P3（単視点。image だけ左上）を私の側でも再現し、**補強前は survive、補強後は
  killed（2 failed）**であることを確認した。新発見は正しい
- テストの作りも妥当。定数 `0.5` で埋めることで `ne(0.0)` が配置と厳密に一致し、
  `torch.rand` が偶然 0 を出す取りこぼしを避けている。冒頭の
  `assert not batch.valid_pixel_masks.all()` は「padding が在る配置であること」を
  先に固定していて、退化した入力で主張が空虚になるのを防いでいる

### N-4（失敗 epoch の重複 log）の現状維持 → 是認。ただし前提 1 に訂正

理由 3（塞ぐには Trainer に新しい状態が要る）が決定的で、AGENTS.md 原則 2 と整合する。
理由 2 も事実。**理由 1 だけは、記録した条件のまま信じると誤る。**

- 「重複した 2 行は同じ内容」は metric については概ね成り立つが、
  **`epoch_seconds` と `train_samples_per_second` は wall clock なので必ず異なる**
- したがってノートの条件「解釈の邪魔になったら理由 1 の前提が崩れていないかを確かめる」は、
  **前提が最初から部分的に崩れている**ことを明記しておかないと、将来の読み手が
  「同内容のはず」を出発点にして時間 metric の差を異常と誤読する
- 条件文を「metric 行は同内容。ただし時間 2 項目は必ず異なる」へ具体化すれば足りる

## 新規指摘

### N3-1（must-fix）`MultiViewPaddedBatch` の**配置位置そのもの**を観測するテストが無い

- 対象: `tests/ml/data/test_batch.py::TestMultiViewPaddedBatchPad`
- **実測（`src/` 無改変、pytest plugin で `pad` を差し替え）**
  - MUT-P4（`training` に関わらず常に左上へ置く。image / mask とも）
    → `tests/ml` 全体 **1315 passed で生存**
  - MUT-P5（`training` を無視して常に中央へ置く）
    → `tests/ml` 全体 **1315 passed で生存**
- 契約は doc に明記されている。
  `docs/image-based-dispense-calibration-ml-plan.md:321-322`
  「collate 時は batch 内の最大高さ・幅を 8 の倍数へ切り上げ、training では画像の配置位置を
  ランダム、evaluation では中央にして padding する」。この節は本 MR で
  `[B, V, 6, H, W]` を返す多視点経路の記述へ更新されている
- 単視点側には対応する観測点が 2 つある
  （`test_centres_each_image_during_evaluation` / `test_training_placement_is_not_the_evaluation_centre`）。
  **新設した多視点側にだけ無い**
- 今回足した `test_places_the_image_where_the_mask_says_it_is` は image と mask の
  **相対**一致しか見ないので、両方を同じだけずらす変異は通る。
  `test_training_placement_is_seed_deterministic` は再現性だけ、
  `test_places_every_view_of_a_sample_at_the_same_position` は view 間の一致だけを見る
- 補強案: 単視点側の 2 本と同じ観測点を多視点側にも置く
  （評価時に中央であること、学習時の配置が評価時と一致しないこと）。1〜2 本で足りる
- 確信度: 高（実行確認）／ 深刻度: 中
- 注: N-1 / M2 と同型（新設した公開機構に対する検証済み survivor）なので同じ区分にした。
  これが私の持っている最後のブロッキング項目

### N3-2（should-fix）要件 doc の「目標」が複数視点節と矛盾

- `docs/image-based-dispense-calibration.md:30`
  「複数視点の画像を**将来**利用できるデータ形式にする。」が未更新
- 同じ doc の 複数視点節（191-215）は「1 パッドを複数視点で撮影し、複数視点のまま推定する」
  へ変わっている。doc 冒頭の目標リストだけが旧方針のまま
- 確信度: 高 ／ 深刻度: 中（読み手が最初に読む節なので）

### N3-3（should-fix）「達成条件」節に view 数の条件が無い

- 複数視点節 214-215 は「**達成条件は 1 view でも成立させる**」と宣言しているが、
  達成条件節（702-）は ±10% と coverage 68.3% を書くだけで view 数に触れない
- 参照が片方向なので、達成条件節だけを読む人には 1 view 条件が伝わらない
- 「この条件は 1 view の入力でも満たすこと」の 1 文で閉じる
- 確信度: 高 ／ 深刻度: 低〜中

### N3-4（should-fix）「5 view」が doc のどこにも定義されていない

- 複数視点節は「5 view は中心と対称な 4 方向」「推論時は 1 view から 5 view まで」と
  5 を既知として使うが、収集節 227 は「view 数は session 単位の設定とし」と書くだけで
  **既定値も上限も定義していない**。offset の大きさ（1 mm）も 199 行に項目名があるだけ
- 「session 単位の設定」と「1〜5」という固定範囲が同居していて、どちらが契約か読めない
- 収集節に既定（5 view = 中心 + 対称 4 方向、offset 1 mm）を 1 行置けば、
  複数視点節の 5 と 1〜5 が anchor される
- 確信度: 中 ／ 深刻度: 低

### N3-5（nit）ml-plan の Normalization 節に旧 encoder の channel 数が残る

- `docs/image-based-dispense-calibration-ml-plan.md:439`
  「採用する全 channel 数 24、32、48、96、**160** は 8 で割り切れる。」
- 160 は削除した 3 段目の出力 channel で、新しい表（24 / 32 / 48 / 96）では使わない
- 確信度: 高 ／ 深刻度: 低

### N3-6（nit）metadata schema の例が view 1 件のまま

- `docs/image-based-dispense-calibration.md:575-585` の `views` 配列は要素 1 件
  （`number: 0`）。配列形なので表現力は足りているが、多視点が既定になった以上、
  2 件並べたほうが例として自己説明的
- 確信度: 高 ／ 深刻度: 低

## 変異手順への提言（今回の 2 件が示したもの）

実装者が足した規則「**対になる実装には同じ変異を必ず当てる**」は正しい方向だが、
今回の MUT-P4 / P5 はその規則では出てこない。2 点の補強を勧める。

### 補強 A: 規則の向きが片側になっている

現行の規則は「**新設側に当てた変異を既存側にも当てる**」（N-1 → MUT-P3 の発見はこの向き）。
MUT-P4 / P5 は**逆向き**で、「既存側にある観測点が新設側に無い」型。
規則を「対になる実装は**両方向**へ当てる」あるいは
「**対の実装どうしでテストの観測点集合を突き合わせる**」へ広げる必要がある。
後者のほうが安く、今回なら
「単視点は配置位置を 2 本で見ているのに多視点は 0 本」が表を作った時点で見える。

### 補強 B（より根本的）: 共有ヘルパーへ当てた変異は、呼び出し側ごとのテスト欠落を隠す

`_placement` は `PaddedBatch.pad` と `MultiViewPaddedBatch.pad` の**共有関数**。
ここへ変異を当てると、単視点側のテスト
（`test_centres_each_image_during_evaluation` 等）が必ず殺す。
**多視点側に観測点が 1 本も無くても、sweep は killed を報告する。**

2 巡目の原因 3（カタログとテストが盲点を共有する）の変種だが、原因は
カタログの中身ではなく**変異を当てる場所**にある。

→ 規則: **共有ヘルパーへの変異は、各呼び出し側の本体へ inline してから当てる**
（call-site ごとに 1 変異）。今回の MUT-P4 / P5 はまさにこの形で作った。
対象は「2 箇所以上から呼ばれる私有ヘルパー」に限れば数は少ない
（`_placement` / `_ceil_to` / `_validate_batch_common` / `_target_shape` /
`_transformed_images` / `_valid_pixel_mask` / `_fraction` 程度）。

### 5 ステップ手順への追記案

既存の 5 ステップに、次の 1 行を判定の目安として足す。

> **共有ヘルパーの変異が killed でも、その機構が全呼び出し側で守られている証拠にはならない。**
> 呼び出し側が 2 つ以上あるヘルパーは、call-site ごとに変異を当てる。

## 検証結果（3 巡目）

コンテナ（`pcb-assembly-ml-ml-1`）で実行。実機テストは実行していない。

- `pytest tests/ml -m "not hardware and not e2e"`: **pass**（1315 passed / 1 skipped / exit 0）
- `pyright src/ml tests/ml scripts/ml_smoke.py`: **pass**（0 errors / 0 warnings）
- `make format`: 未実行（`.git` ロック競合回避。orchestrator 側で pre-commit 全 hook Passed の報告あり）
- `src/` の diff が 2 巡目から不変であることを hash で確認
- 成果物汚染（`</content>` 等）なし。変更した docs / tests に docformatter の連結痕なし
- `scripts/ml_smoke.py:37` の `_DUMMY_SHAPES = ((64, 64), (1024, 256))` は
  ml-plan 65 行の記述と一致（サイズ契約とは別物の generic dummy なので更新不要）

### 変異の実測

| 変異 | 結果 | 殺したテスト |
| --- | --- | --- |
| MUT-P1（多視点。mask は正位置、image だけ左上） | **killed（2 failed）** | `test_places_the_image_where_the_mask_says_it_is[False/True]` |
| MUT-P3（単視点。同型） | **killed（2 failed）** | 同上（単視点側） |
| MUT-P2（多視点。view ごとに配置をずらす） | killed | `test_places_every_view_of_a_sample_at_the_same_position` |
| **MUT-P4（多視点。常に左上へ置く）** | **survived（1315 passed）** | — （N3-1） |
| **MUT-P5（多視点。`training` を無視して常に中央）** | **survived（1315 passed）** | — （N3-1） |

一時 plugin は実行後に削除済み。working tree に残骸なし。

---

# 4 巡目レビュー（N3-1 / 補強 A・B / docs 5 件 対応後）

## verdict: approve

1〜3 巡目の must-fix 3 件・should-fix 12 件・nit 10 件はすべて解消を確認した。
残るのは nit 2 件だけで、いずれもマージを止めるものではない。

`src/` は 3 巡目から不変（diff hash `57d6618f…` が一致、10/10 の報告と整合）。

## 3 巡目指摘の解消状況

| # | 状態 | 確認方法 |
| --- | --- | --- |
| N3-1 | **解消** | MUT-P4（常に左上）→ **killed（2 failed）**、MUT-P5（`training` 無視）→ **killed（1 failed）** を実測。新テストは 56 px canvas の 32 px 画像で `[12,12]` 有効 / `[11,11]` 無効を厳密に見ており、左上固定と中央固定の両方を分離できている |
| N3-2 | 解消 | 目標リストが「複数視点のまま推定する」「1 view でも推定できる」の 2 項へ。旧「将来利用できるデータ形式にする」は消えている |
| N3-3 | 解消 | `### 推定精度` に「既定 5 view で必須、同じモデルが 1 view でも動作することを併せて確認、1 view の精度は下回ってよいが推論が成立しない状態は許容しない」を追記。複数視点節への相互参照あり |
| N3-4 | 解消 | `## データ収集` が「中心 view 1 点 + 中心から 360/n 度ずつ回した n 方向、既定 n = 4、距離 1 mm」を定義し、**「したがって既定の view 数は 1 + 4 = 5」**と明示。`n`（方向数）と `view_count`（総数）の取り違えも塞がっている |
| N3-5 | 解消 | channel リストが `24、32、48、96` へ（160 削除） |
| N3-6 | 解消 | schema の `views` が 5 件。offset は `(0,0) / (1,0) / (0,1) / (-1,0) / (0,-1)` で、定義した「90 度 × 4 方向、距離 1 mm」と一致 |
| N-4 理由訂正 | 解消 | 時間 metric が必ず食い違うことを前提にした記述へ |

### docs の事実関係（追加確認）

- view 幾何は MR !207（未 merge）の
  `PasteDatasetSettings.view_count` / `view_offset_mm` と field 対応が取れている。
  !207 は DTO に既定値を持たないので、doc 側で既定を決める形は矛盾しない
- 精度条件の「5 view 必須 / 1 view 動作確認」は、`ml` 側の実装
  （view 軸 dynamic の ONNX、平均 pooling、ViewDropout）とすべて整合する
- 相互参照の anchor は percent-encoded 形式。この doc で link を使うのは新 2 箇所だけなので
  既存様式との衝突は無い

## 補強 A / B の取り込み → 妥当。B の効果を実測で確認した

- **A（対の実装で観測点集合を突き合わせる）**: 表の形になっていて、
  両方向の穴（`MUT-P3` は新設 → 既存、`MUT-P4/P5` は既存 → 新設）が同じ表に載る
- **B（共有ヘルパーは call-site ごとに inline して当てる）**: 効いている。
  **CS-TI を再現したところ、単視点と多視点の call-site テストが 2 本とも落ちた**
  （`_transformed_images` の回転を落とす変異 → 2 failed）。
  共有ヘルパー本体への変異が 1 巡目から killed だったこととは独立に、
  call-site ごとの検出器が実在することを確認できた
- CS-CT3 も再現して killed（`test_the_single_sample_check_counts_the_stride_aligned_size`）

### CS-TI が「本 MR で最も分かりにくい穴」という評価に同意する

mask が `_valid_pixel_mask` という**別経路**で作られるため、画像側の変換を落としても
mask を見るテストは全て通る。N-1（配置）と CS-TI（回転）は、
**「mask は見るが image を見ない」という同一の構造欠陥が、batch 層と前処理層の
両方に独立に存在していた**もの。層をまたいで同型が出た以上、
今後 image / mask の 2 経路を持つ機構を足すときは最初からこの観測点を置くのがよい。

## `allclose(atol=1e-6)` への緩和 → 妥当

実測（テストと同一の入力: `torch.randint` 生成、40 px、seed 3 / 4）。

| 対象 | max abs diff | `torch.equal` |
| --- | ---: | --- |
| 単視点 40 px（テストが使う条件） | 2.384e-07 | **False** |
| 多視点 40 px（同上） | 2.384e-07 | **False** |
| 回転を落とす変異との差 | **3.3941** | — |

- `torch.equal` は実際に成立しないので、緩和しないとテストが書けない。判断は正しい
- 2.384e-07 は float32 の 1 ULP（2^-22）相当。回転は画素値の**置換**なので
  `sample_layer_norm` の総和順序が変わり、丸めが変わる。原理的に不可避
- docstring の「残る 1e-7 台の差は標準化の float32 丸め」「変異の差（3.4）とは 7 桁離れている」は
  **実測と一致**。根拠の書き方として十分
- サイズ依存の実測は 39 / 40 / 64 px で残差あり、41 / 53 / 56 px で厳密 0。
  報告にあった「奇数サイズでは 0、偶数では残る」は成り立たない。
  **docstring にはこの主張が無いので出荷物としては問題ない**が、ノート側の
  表現は「サイズと入力に依存して 1 ULP 残ることがある」へ直したほうがよい

## 新規に探した穴（結果は空振り。記録として残す）

A ルールを `PreprocessedSample` ↔ `PreprocessedMultiViewSample` にも当てた。
多視点にしかない観測点「invalid 領域が 0 になる」
（`test_zeroes_the_invalid_region_of_every_view`）の単視点版が無いように見えたので、
call-site 変異 **CS-SLN1**（単視点の `preprocess` が `sample_layer_norm` へ
`valid_mask` を渡さない）を実行した。

→ **killed**（`test_rotation_marks_the_corners_invalid` が捕捉）。私の仮説は外れ。
単視点側の観測点は存在していて、テスト名から読み取りにくかっただけ。

## nit（2 件。いずれもマージを止めない）

### N4-1 `torch.allclose` の既定 `rtol=1e-5` が効くので、実効許容幅は docstring の 18 倍

- 対象: `tests/ml/data/test_image.py:680, 743`
- `torch.allclose(a, b, atol=1e-6)` の許容は `atol + rtol * |b|` で、標準化後の値は
  最大 ~1.8 なので**実効 1.81e-5**。docstring は 1e-6 を根拠として挙げている
- 変異の差 3.4 とは依然 5 桁離れているので検出力は落ちない。ただしこのチームは
  「書いた数値＝守っている数値」を繰り返し要求してきたので、`rtol=0` を明示すると
  docstring の 1e-6 が実際の契約になる
- 同じ file の既存 3 箇所（`TestSampleLayerNorm`、421 / 436 / 453 行）も既定 rtol のままなので、
  本 MR 固有ではない。合わせるなら別 MR でよい
- 確信度: 高 ／ 深刻度: 低

### N4-2 `test_is_normalized_over_the_valid_region` は名前と観測点がずれている

- 対象: `tests/ml/data/test_image.py`（`TestPreprocessedSamplePreprocess`）
- 名前は「valid region に限定した標準化」を主張するが、入力は `NO_AUGMENTATION` で
  mask は全 true。実際に観測しているのは「全域で mean 0 / std 1」だけ
- valid region 限定の振る舞いを実際に守っているのは
  `test_rotation_marks_the_corners_invalid`（CS-SLN1 を殺したのはこちら）
- 本 MR で追加した行ではない（既存テスト）。名前を実態へ寄せると、
  今回のような観測点の棚卸しで誤読しなくなる
- 確信度: 高 ／ 深刻度: 低

## 検証結果（4 巡目）

コンテナ（`pcb-assembly-ml-ml-1`）で実行。実機テストは実行していない。

- `pytest tests/ml -m "not hardware and not e2e"`: **pass**（1320 passed / 1 skipped / exit 0）
- `pyright src/ml tests/ml scripts/ml_smoke.py`: **pass**（0 errors / 0 warnings）
- `make format`: 未実行（`.git` ロック競合回避。orchestrator 側で pre-commit 全 hook Passed の報告あり）
- `src/` の diff hash が 3 巡目と一致
- 成果物汚染（`</content>` 等）なし、変更した docs / tests に docformatter の連結痕なし

### 変異の実測

| 変異 | 結果 | 殺したテスト |
| --- | --- | --- |
| MUT-P4（多視点。常に左上へ置く） | **killed（2 failed）** | `test_centres_each_image_during_evaluation`（多視点）ほか |
| MUT-P5（多視点。`training` を無視して常に中央） | **killed（1 failed）** | `test_training_placement_is_not_the_evaluation_centre`（多視点） |
| CS-CT3（`alone` から `_ceil_to` を落とす） | **killed（1 failed）** | `test_the_single_sample_check_counts_the_stride_aligned_size` |
| CS-TI（`_transformed_images` の回転を落とす） | **killed（2 failed）** | 単視点 / 多視点の call-site テスト**両方** |
| CS-SLN1（単視点 call-site が mask を渡さない。私の追加仮説） | killed（1 failed） | `test_rotation_marks_the_corners_invalid` |

一時 plugin / script は実行後に削除済み。working tree に残骸なし。

## 4 巡を通した所見（次 MR への申し送り）

見つかった穴は最終的に 3 つの型に収束した。次のドメイン MR で最初から潰せる。

1. **戻り値が呼び出し側で捨てられる**（M1）。関数内変異では原理的に取れない。
   公開関数の戻り値が観測可能な出力へ届くことを、呼び出し側のテストで固定する
2. **対になる 2 経路で観測点集合が非対称**（M2 / N-1 / MUT-P3 / MUT-P4 / MUT-P5）。
   表にして突き合わせる
3. **同じ機構が 2 経路で作られていて、片方しか観測していない**
   （N-1 と CS-TI の image / mask）。層をまたいで同型が 2 回出た

---

# 5 巡目レビュー（`nn.init.zeros_(mean_layer.weight)` 追加後）

## verdict: approve

src の修正は正しく、実測で裏づけが取れた。残りは should-fix 1 件と nit 2 件で、
いずれもマージを止めない。

## src 変更（`heads.py` のみ）の評価 → 正しい

`nn.init.zeros_(mean_layer.weight)` を bias の正初期化と対で入れる形。

### 独立実測（`hidden_features=128` / `input_features=96`、300 seed、実 head 経路）

| feature の regime | bias | weight = torch 既定（旧） | weight = 0（現） |
| --- | ---: | ---: | ---: |
| 1 本を複製 + 微小ノイズ | 1.0 | 0/300 | 0/300 |
| 同上 | 0.2 | **63/300 = 21%** | — |
| 同上 | 0.05 | **133/300 = 44%** | **0/300** |
| 標準正規 | 0.05 | 12/300 = 4% | **0/300** |
| 全 0 | 0.05 | 9/300 = 3% | **0/300** |
| 全 regime | 1e-6 | — | **0/300** |

- **bias だけでは足りないという診断は正しい。** 0.05 で 44% が初期化時点で全滅する
- **weight 0 は構造的な保証になっている。** bias を 1e-6 まで下げても 0/300。
  初期の前活性が bias そのものになるので、正でありさえすれば活性領域に入る。
  マジックナンバーではないという主張はそのとおり
- **勾配も流れる。** 前活性が正なので ReLU の微分が 1 になり
  `grad_z · hidden` が weight へ伝わる。`test_the_mean_head_still_learns_from_a_zero_weight`
  が「3 step 後に sample 間で出力が割れる」ことで固定している
- `log_variance` 側を触っていないのも正しい。ReLU を通らないので死なない

### 副作用の確認

- **1 step 目だけ、平均経路から trunk への勾配が 0 になる**（`grad_hidden = grad_mean * weight = 0`）。
  trunk は log 分散経路からは勾配を受けるので停止はしない。2 step 目には weight が
  非零になって解消する。最終出力層の 0 初期化は一般的な手法（ResNet の zero-init-γ 等）で、
  ここで問題になる規模ではない
- **`zeros_` が守るのは初期化時点だけ。** 学習中に全 sample が死ぬ経路は残る。
  それは `MeanSaturationDiagnostic` と M1 の log 経路が受け持つ設計で、変更なし

### 変異の再実測

| 変異 | 結果 |
| --- | --- |
| HD-W1（`zeros_` を削除＝torch 既定へ戻す） | **killed（3 failed）**。`test_the_mean_head_is_never_born_dead[False-0.05]` が **seed 0** で落ちる |
| HD-W2（weight は 0 のまま bias を 0 にする） | **killed（40 failed）** |

HD-W1 が seed 0 で落ちるので、検出器は先頭で発火する。
なお `[True-0.05]`（全 0 feature）側は HD-W1 で落ちない（実測 3% なので
seed 0〜31 に該当が無い）。**検出を担っているのは `[False-*]`（randn feature）側**で、
これは docstring の「死亡率が高いのは後者」という記述と整合する。

## テスト 6 本の移し替え → 観測力はほぼ保たれている。1 点だけ失われた

依頼された「conditioning が平均へ効くことを誰も見なくなっていないか」を変異で検証した。

| 変異 | 結果 |
| --- | --- |
| MUT-C1（`_joined_inputs` が conditioning の中身を 0 で潰す） | **killed（2 failed）**: `test_conditioning_changes_the_prediction`（heads、log 分散側）と `test_the_conditioning_stays_one_value_per_sample`（multiview） |
| MUT-C2（平均だけ conditioning 抜きの trunk 出力から作る。log 分散は本物） | **survived（1324 passed）** |

- **現実的な変異（MUT-C1）は log 分散側の観測点で捕まる。** conditioning は
  `_joined_inputs` で 1 回だけ連結され、mean と log 分散は同じ trunk を共有するので、
  「conditioning が trunk へ届く」ことを log 分散側で見れば配線は守れる。
  移し替えは機構としては等価
- **ただし「conditioning が平均へ効く」を観測するテストは 0 になった**（MUT-C2）。
  移し替え前は `test_conditioning_changes_the_prediction` がそれを見ていた

### S5-1（should-fix）「conditioning が体積推定（平均）へ効く」を誰も観測していない

- 実測: MUT-C2 が `tests/ml` 全体 1324 passed で生存
- 契約側の根拠: ml-plan の v1 encoder 節が
  「`log(pixel_per_mm)` 1 値を連結する…画像から見かけの大きさを学びつつ、物理 scale を
  明示的に利用できる構成になる」と書いており、効かせたい先は**体積推定＝平均**
- 現状は「mean と log 分散が 1 個の trunk を共有する」というアーキテクチャ不変条件に
  依存して間接的に守っている。その不変条件自体を固定するテストも無い
- **must-fix にしなかった理由**: MUT-C2 は trunk を 2 回通す構造書き換えが要り、
  偶発的な編集では起こらない。これまでの must-fix（M2 / N-1 / N3-1）はいずれも
  1 文・1 トークンの編集で起こりうるものだった。この区別は保つ
- 補強案（3 行）: `test_conditioning_changes_the_prediction` に数 step の学習を足し、
  weight が非零になった後で**平均**も conditioning に応じて動くことを見る。
  既に `_head_trained_*` のヘルパーがあるので流用できる
- 確信度: 高（実行確認）／ 深刻度: 低

### その他の移し替え

- `test_parity` の「出力を特定せず any」への変更は妥当。出力 1 本だけを比較する退行は
  `assert result.passed is False` の側で捕まるので、検出力は落ちていない
- T24 / T24' を学習後の head で観測する形にした点も妥当。`_head_trained_to_partially_saturate`
  は真値 0 の blank を 1 件混ぜて回帰させており、**ReLU 化が狙った状態そのもの**を
  再現している。初期化直後の合成入力より仕様に近い

## docs の数値追記 → 概ね正確。1 箇所だけ実測とずれる

| docs の記述 | 私の実測 | 判定 |
| --- | --- | --- |
| weight 既定 + bias 0.05 で 300 seed 中 134 回 = 45% | 133/300 = 44% | ほぼ一致 |
| 標準正規の feature では 5% | 12/300 = 4% | 一致 |
| **全 0 では 1%** | **9/300 = 3%** | **約 3 倍の過小** |
| weight を 0 にすると 0/300 | 全 regime 0/300（bias 1e-6 まで） | 一致 |
| bias=1.0 なら全滅 0% | 全 regime 0/300 | 一致 |

- 「実装者の 55% / 80% は trunk を通さない測定」という orchestrator の診断も裏づけられた。
  実 head 経路の randn は bias 0.2 で 0%、0.05 で 4% で、報告の「0% と 5%」と一致する
- 「条件で 1 桁以上変わる／低い測定値を見て安全と判断しないこと」という結論は、
  44% 対 3〜4% という実測が支持する。**追記の趣旨は正確**

### S5-2（nit）docs の「全 0 では 1%」は実測 3%

実害は無い（結論は変わらない）が、過小側へずれているので「安全と判断しないこと」という
文の説得力を自分で弱めている。3% へ直すか「数 %」に丸めるのが素直。

### S5-3（nit）テストの docstring が discredited な数値を持っている

- 対象: `tests/ml/model/test_heads.py::test_the_mean_head_is_never_born_dead` の docstring
- 「実測で ``mean_bias_initial=0.05`` の **9 割近く**が初期化時点で死ぬ」と書いてある
- docs は同じ現象を 45%、私の実測は最悪の regime で 44%。
  **このテストが実際に使う 2 つの regime（全 0 / randn）では 3〜4%**
- 「9 割」は orchestrator が既に否定した実装者の旧測定（trunk を通さない経路）の値と思われる。
  docs 側は直っているがテスト側に残っている
- 実害: 将来の保守者が「9 割死ぬなら 32 seed は十分すぎる」と読んで
  `BIRTH_CHECK_SEEDS` を削ると、実際は 3〜4% なので検出器が空になりうる
- 確信度: 高 ／ 深刻度: 低

## 手順 6（契約そのものの妥当性を測る）の評価 → 妥当。3 点の補強を勧める

**この整理は正しく、しかも既存のどの手法でも原理的に取れない型を正しく名指している。**
`validate()` の述語は `mean_bias_initial > 0` で、実装はそのとおりで、
その述語への変異（0 / 負 / nan / 境界）はすべて killed だった。それでも
**述語が「守りたい性質」を含意していなかった。** ソース由来・契約由来・call-site の
いずれも契約を正しいものとして扱うので、この型は出ない。

### 補強 1: 「測る対象」を絞る判定条件を書く

「防ぎたい失敗を数値で測る」を全 guard へ適用すると重い。次の 1 行で絞れる。

> **guard の docstring が述べる目的が、述語の言い換えになっていないなら測る。**

- `mean_bias_initial は正の有限値が必要です` ＋ 目的「全 sample が死んだ領域へ入るのを防ぐ」
  → 述語 ≠ 目的。**測る**
- `stride は正の整数が必要です` ＋ 目的「`ZeroDivisionError` を避ける」
  → 述語 ⇒ 目的。測らなくてよい

### 補強 2: 測ってギャップが出たら、まず「構造で保証する」を試す

今回採った `zeros_` は、閾値を測って `validate()` へ書くのではなく
**述語が目的を含意する形へ機構を変えた**もので、これが最良の解き方。
手順にこの優先順位を明記しないと、次は `validate()` に測定由来のマジックナンバーが
入りかねない。**それは regime 依存なので今回のデータ（44% / 4% / 3%）が示すとおり脆い。**
測定閾値は構造化できないときの次善に置く。

### 補強 3: guard 単体ではなく「guard + 文書化された推奨値」を対にして見る

今回いちばん危なかったのは guard の緩さそのものではなく、
**docs が「0.05〜0.2 へ上書きせよ」と、guard が通してしまう危険域を名指しで推奨していた**
こと。緩い guard と「境界付近の値を勧める文書」の組み合わせは、どちらか単独より悪い。
手順に「guard を測るときは、その値について文書が何を勧めているかも一緒に見る」を足す。

### レビュー側の反省（記録）

**1 巡目の N5 でこの点に触れておきながら、数値で詰めなかった。**
「既定 1.0 は真値スケール 0.05〜0.2 µL に対して 5〜20 倍。ドメイン側が必ず上書きする
前提でよいか」と質問し、「docs に明記した」という回答を受け入れて閉じた。
**問いは数値の問いだったのに、文書の回答で満足した。**
レビュー側の教訓として、`memory/MEMORY.md` 級の一般則にするなら
「レビューで出した問いが数値の問いなら、文書の更新は回答にならない。測るか、測らせる」。

## ReLU 維持の判断 → 妥当

線形化の代案を退けた 2 つの理由に同意する。

1. 線形出力が厳密 0 になるのは 0/4096 で、blank の真値 0 を表現できない。
   ReLU 化の唯一の目的が失われる
2. `valid_sample_mask` が `mean > 0` を要求するので、負の予測は**黙って**集計から落ちる。
   ReLU なら `MeanSaturationDiagnostic` が数えるが、線形には対応する機構が無い。
   「壊れ方が見えなくなる」方向の変更なので採らないのが正しい

## 検証結果（5 巡目）

コンテナ（`pcb-assembly-ml-ml-1`）で実行。実機テストは実行していない。

- `pytest tests/ml -m "not hardware and not e2e"`: **pass**（1324 passed / 1 skipped / exit 0）
- `src/` の変更は `heads.py` のみ（`git diff --cached main --stat -- src/` で確認）
- 変異 4 件: HD-W1 killed（3 failed、seed 0 で発火）／ HD-W2 killed（40 failed）／
  MUT-C1 killed（2 failed）／ **MUT-C2 survived（S5-1）**
- 初期化時全滅率を 300 seed × 3 regime × 複数 bias で独立実測（上表）
- 一時 plugin / script は削除済み。working tree に残骸なし

## 残課題（別 MR でよい）

1. S5-1: 学習後の平均が conditioning に応じて動くことを 1 本足す
2. S5-2 / S5-3: 数値の食い違い 2 箇所
3. 4 巡目の N4-1（`allclose` の既定 `rtol`）と N4-2（テスト名と観測点のずれ）
