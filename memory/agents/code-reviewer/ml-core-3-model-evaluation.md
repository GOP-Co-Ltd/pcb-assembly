# コア ML 基盤 MR3（モデル部品と評価）レビュー

対象: `feature/2026-09-04/ml-core-3-model-evaluation` の staged 差分（25 file / 4,214 行）。
突き合わせ先: `memory/agents/implementation-planner/ml-core-3-model-evaluation.md`、
`memory/agents/orchestrator/ml-core-3-model-evaluation.md`、
`docs/image-based-dispense-calibration-ml-plan.md` §2 / §3 / §6。

## verdict: request-changes

must-fix 2 件（うち 1 件は orchestrator が明示的にレビュー依頼した論点 1）。
公開シグネチャは計画書どおりで逸脱なし。計画書の「テスト観点」は全項目がテストになっている。

## must-fix

### M1. reliability bin だけ重みなし集計。weight の設計目的を無効化する（論点 1）

- `src/ml/evaluation/slices.py:235-268`（`_reliability_bins`）、`src/ml/evaluation/slices.py:79-91`
- overall / slice の metric は `sum(w·x)/sum(w)`、`_weighted_percentile` に至っては
    `weight > 0` の sample しか使わない（`regression.py:233`）。reliability bin だけが
    件数ベースで、しかも weight 0 の sample まで満額で数える
    （`valid_sample_mask()` は `sample_weight >= 0` を有効とする。`regression.py:78`）。
- 根拠 1（仕様）: doc §1 の weight は
    `w = 1/(session 内 pad 数) × 1/(pad 内 view 数)`（`docs/image-based-dispense-calibration-ml-plan.md:185-196`）で、
    目的は「各収集 session が同程度の寄与を持つ」こと。実運用の weight は sample ごとに
    桁で違う。reliability bin だけ件数ベースだと、pad 数の多い session が bin を支配し、
    weight 設計が打ち消される。doc §6「calibration 後の coverage を 68.3% と比較し、
    bin 別 reliability も記録する」の bin 別 coverage が overall coverage と別基準になる。
- 根拠 2（実測）: `mean=[1,2,4,100] / target=[2,2,2,2] / log_variance=[0,0,0,3] /
    weight=[1,1,1,0]`、`reliability_bin_count=2` で
    `overall.weight_sum = 3.0`（100 の sample は全 metric に寄与しない）なのに
    bin 2 は `sample_count=1` / `observed_root_mean_squared_error=98.0`。
    寄与 0 のはずの sample が単独 bin を作って壊滅的な数値を出す。
- 根拠 3（命名衝突）: `mean_predicted_standard_deviation` が
    `GaussianRegressionMetrics`（重み付き）と `ReliabilityBin`（重みなし）で同名別定義。
    MR6 で evaluation.json に落とすと同名・別意味のフィールドが並ぶ。
- 実装者の「重み付きにすると bin 内 weight 合計 0 の分岐が要る」は、
    `_weighted_percentile` と同じく `weight > 0` で先に絞れば消える。
    bin の**境界**は計画書どおり等件数のままでよく、bin 内の 3 統計だけを
    `sum(w·x)/sum(w)` にすればよい。
- 確信度: 高（挙動・仕様根拠とも実測／原典確認済み）

### M2. 過学習テストの閾値が実測値に張り付いている（論点 6）

- `tests/ml/model/test_heads.py:205-230`（`test_overfits_a_handful_of_samples`）
- `assert last_error < first_error * 0.5` に対し、実測の比は **0.4621**。余裕 7.6%。
    浮動小数の Adam 軌跡なので、BLAS / torch version / CPU が変われば反転しうる。
    計画書の観点は「MAE が初期比で大きく縮む」だが、実際には半分にしか縮んでいない。
- 併せて `torch.manual_seed(41)`（`test_heads.py:206`）は**効いていない**。直後の
    `_regressor()` が内部で `torch.manual_seed(23)` を呼んで上書きするため。
    outer seed を 0 / 1 / 2 / 7 / 99 に変えても比は 0.4621 で一定であることを実測確認した。
    seed を制御しているように見えて制御していない。
- 「実装が達成できた値に合わせて期待値を置いた」形になっており、
    契約（過学習できること）を検証していない。step 数 / lr を上げて余裕を作るか、
    NLL の単調減少や MAE の絶対値など軌跡に鈍い条件へ変える。
- 確信度: 高（0.4621 と seed 無効化はいずれも再現実行で確認）

## should-fix

### S1. 重み付き percentile が「重み = 複製回数」と一致しない（論点 5）

- `src/ml/evaluation/regression.py:223-256`
- 他の metric（NLL / MAE / RMSE / 相対誤差 mean・std / coverage / 予測 std 平均）は
    すべて `sum(w·x)/sum(w)` で、整数重みなら sample を複製したのと厳密に等価。
    percentile だけこの性質を持たない。実測: 値 `[0.5, 0, 1.0, 3.0]` / 重み `[1,3,1,1]` の
    median は **0.4167**、同じものを 3 複製した 6 sample の median は **0.25**。
- 計画書の risk 節は「定義が一意でないため契約としてテストで固定する」としているが、
    固定されているのは一様重みの場合だけ（`test_regression.py:153-173`）。
    非一様重みの定義は 1 本もテストで固定されていない。
- 定義自体は数学的に妥当なので、(a) 複製等価な定義へ揃える、(b) 現定義のまま
    「複製等価ではない」ことを docstring とテストで明記する、のどちらかを選ぶ判断が要る。
- 確信度: 高（不一致は実測）／中（どちらへ寄せるべきかは設計判断）

### S2. `GaussianPredictions` の等価性が壊れている

- `src/ml/evaluation/regression.py:35-38`
- 4 フィールドすべてが `attrs.field(eq=False)` なので `__eq__` が比較対象を 1 つも持たず、
    中身が全く違う 2 instance が `==` で真になる。実測: 別データの 2 件で `a == b -> True`、
    `hash(a) == hash(b)`、`len({a, b}) == 1`。
    `with_log_variance_offset(2.0)` の戻り値も元と `==` になる。
- ただし MR2 の `src/ml/data/batch.py:40-41`（`PaddedBatch`）が同じ形で既に main に入っている。
    本 MR 固有の新規欠陥ではなく既存パターンの踏襲。
- 修正は class 側で `@attrs.frozen(eq=False)`（identity 比較）にするだけ。
    `PaddedBatch` も同じなので、リポジトリ横断で別タスクにするかは orchestrator 判断。
- 確信度: 高（挙動は実測）

### S3. 数値次元で空 bucket が高頻度に出る

- `src/ml/evaluation/slices.py:195-232`（`_numeric_buckets` / `_bucket_edges`）
- 境界の縮退畳み込みは「**値が等しい境界**」しか潰さないので、区間内に sample が
    1 件も無い bucket が残る。値が重複しがちな次元での randomized 検査（3,000 ケース）で
    **440 件**（約 15%）に `sample_count == 0` の slice が出た。
    例: 値 `(5.0, 0.0)` / `bucket_count=5` → 5 bucket 中 3 個が空。
- 空 bucket は `metrics=None` と reason `"予測が 1 件もありません"` を持つ `DiagnosticSlice` になる。
    reason 文言も「予測全体が空」の意味なので、bucket が空という状況を説明していない。
- 計画書「境界が縮退したら bucket を減らす」の意図から外れている。空 bucket を落とすか、
    観測値のユニーク値から境界を作る。
- 確信度: 高（頻度・例とも実測）／中（実ドメインの連続量次元では出にくい）

### S4. `.4g` 整形で bucket ラベルが衝突する

- `src/ml/evaluation/slices.py:27, 200-207`
- 境界は区別できているのにラベルだけ同じになる。実測: 値
    `(1.000001, 1.000002, 1.000003, 1.000004)` / `bucket_count=4` で
    ラベルが `['[1,1)', '[1,1)', '[1,1)', '[1,1]']`。
- `DiagnosticSlice.value` は MR6 で evaluation.json のキー相当になる見込みで、
    同一次元内で一意でないと slice を識別できない。
- 確信度: 中（再現は確認済み。実ドメインの `pixel_per_mm` / aspect ratio で
    ここまで近い値が並ぶかは不明）

### S5. 有効判定と weight 合計チェックが 2 か所に複製されている

- `src/ml/evaluation/regression.py:122-135` と `src/ml/evaluation/regression.py:195-207`
- `gaussian_regression_metrics` と `fit_gaussian_log_variance_offset` が、
    `validate()` → `valid_sample_mask()` → 有効 0 件判定 → weight 合計判定 を
    理由文字列まで含めてほぼ逐語で重複させている。片方だけ直すと drift する。
- 確信度: 高

### S6. 実装者が計画外に足した挙動がテストで固定されていない

- `src/ml/evaluation/slices.py:244`（`bin_count < 1` → bin 0 個）: テストなし。
    orchestrator が「受け入れ」と裁定した挙動なので、契約としてテストで固定すべき。
- `src/ml/evaluation/compile_parity.py:85-96, 247-260`
    （`maximum_relative_difference = |a-b| / max(|a|,|b|)`、両方 0 なら 0）:
    計画書に無い独自定義で、値を直接検証するテストが無い。
    `TensorDifference` を直接組み立てる `test_compile_parity.py:169-225` は
    `passed` の論理しか見ていない。
- `src/ml/evaluation/compile_parity.py:269-317`: `mismatched_gradient_parameters` /
    `missing_gradient_parameters` が実比較経路で非空になるケースが 1 本も無い
    （`attrs.evolve` で人工的に詰めた result しか無い）。
- `src/ml/model/loss.py:42-46`: 非有限テストの parametrize が
    `mean` / `log_variance` / `target` のみで `sample_weight` を含まない
    （`tests/ml/model/test_loss.py:125-133`）。
- 確信度: 高（テストの有無は grep で確認）

### S7. compile 失敗の理由文字列が warm-up の失敗まで飲み込む

- `src/ml/evaluation/compile_parity.py:165-175`
- `try` の中に `torch.compile()` と warm-up `_run_pass()` の両方が入っている。
    `torch.compile` は遅延なので warm-up を try の外に出すことはできないが、
    呼び出し側の `loss` クロージャの不具合や OOM も
    `"torch.compile に失敗しました（backend=...）"` と報告される。
- 文言を「compile 済み model の実行に失敗しました」に寄せる程度で足りる。
- 確信度: 中

### S8. doc §3 の「正規化誤差 e」表記が未更新

- `docs/image-based-dispense-calibration-ml-plan.md:513`
- orchestrator 裁定 1 は「`relative_error_*` へ改名し、doc §3 の表記も MR3 の
    ドキュメント段階で揃える」。本差分に doc の変更が含まれていない。
    MR3 を閉じる前か、明示的に MR4 以降へ送るかを決める必要がある。
- 確信度: 高（差分に doc が無いことは `git diff --cached --name-only` で確認）

### S9. 限界価値テストが 2 本ある

- `tests/ml/evaluation/test_slices.py:252-259`:
    `assert 0.0 <= single.one_standard_deviation_coverage <= 1.0` は
    bool tensor の平均なので絶対に失敗しない。期待値を計算して固定すべき。
- `tests/ml/evaluation/test_compile_parity.py:263-268`:
    テスト名は "separately from the first compilation" だが、3 つの秒数が
    `> 0` であることしか見ておらず「分離されている」ことを検証していない。
- 確信度: 高

## nit

- `src/ml/model/blocks.py:59` は `nn.ReLU(inplace=True)`、`blocks.py:90` は `nn.ReLU()`。
    同一ファイル内で不揃い（動作は同じ）。
- `src/ml/evaluation/compile_parity.py:234` の `_as_output_tuple` は名前が converter だが
    実際は raise する。`regression.py:22` の `_as_evaluation_vector`（純変換）と語法がずれる。
- `src/ml/evaluation/regression.py:252-253` の `span <= 0` 分岐は到達不能。
    `weight > 0` で絞ったあとの `positions` は狭義単調増加
    （差分は `w_i·B_i + A_i·w_{i+1} > 0`）。AGENTS.md 原則 2 の「起こり得ないシナリオ」に当たる。
- `src/ml/evaluation/compile_parity.py:29-36` の `CompileOptions` だけ `validate()` を持たない
    （他の値オブジェクトは全部持つ）。backend 不正は理由文字列で返るので実害はない。
- `src/ml/model/heads.py:38-40` の既定値 `hidden_features=128` /
    `log_variance_minimum=-14.0` / `log_variance_maximum=5.0` は doc §2 の v1 値そのもの。
    encoder 側は「ml が持つ既定値は `group_norm_groups` だけ」と徹底しているのに head だけ
    v1 値を持つ。計画書がそう定めているので逸脱ではないが、方針は不揃い。
- `state_dict()` のキーが `_encoder._padding_pixel` のように private 名を含む
    （実測確認）。MR5 の checkpoint 再開・MR6 の ONNX export で、内部属性のリネームが
    黙って互換性を壊す。`_` prefix 規約の帰結なので本 MR の瑕疵ではないが、MR5 着手前に
    意識しておく価値がある。
- `src/ml/model/blocks.py:270-276` の `padding_pixel` docstring が
    「MR4 の部分 fine-tuning が更新対象として選べるよう」と未着手 MR を根拠にしている。
    現時点の実際の根拠は「勾配を観測できるようにするため」。
- `src/ml/evaluation/slices.py:160-164`: 空 bucket / 全無効 slice の reason が
    `"予測が 1 件もありません"`。slice 文脈では意味が通らない。

## 8 論点への判定

| # | 論点 | 判定 |
| --- | --- | --- |
| 1 | `ReliabilityBin` の件数ベース集計 | **不可（M1）**。weight の設計目的（session ごとに同程度の寄与）を bin だけが無効化する。bin の**境界**は等件数のままでよく、bin 内 3 統計を `sum(w·x)/sum(w)` にする。`weight > 0` で先に絞れば実装者が懸念した 0 除算分岐は不要 |
| 2 | `_weighted_percentile` の IndexError | **到達不能。バグではない。** 唯一の呼び出し元 `gaussian_regression_metrics` が事前に「有効 sample の weight 合計 > 0」を確認しており（`regression.py:131`）、有効 sample の weight は `>= 0` なので合計が正なら必ず 1 件は `> 0`。`slices.py` は `_weighted_percentile` を直接呼ばず `gaussian_regression_metrics` 経由（`slices.py:158`）。**ガードを足さないこと**（原則 2 の死にコードになる）。docstring に前提条件を 1 行書けば十分 |
| 3 | `padding_pixel` の public property | **受容。ただし「読み取り専用」ではない。** `encoder.padding_pixel.data.fill_(5.0)` で外から書き換わることを実測確認した。`.grad` を見せる以上 `detach()` は返せないので、これは避けられない。代替（`named_parameters()["_padding_pixel"]` で引く）は private 名文字列への結合になるだけで改善しない。docstring の根拠を MR4 から「勾配の観測」に直す（nit）。`feedback_no_private_test` には抵触しない |
| 4 | 3 体並列による不整合 | **重大な不整合なし。** `validate() -> str \| None` の形、`"X は…が必要です: {value}"` のメッセージ文体（MR2 `data/split.py:49` と一致）、module docstring の 1 文 1 段落、`__all__` のソート、`class TestXxx` 集約、`torch.manual_seed` + `torch.Generator` の使い分けはすべて 3 group で揃っている。差異は nit 3 件（`inplace=True`、`_as_*` の語法、`CompileOptions` の validate 欠如）のみ |
| 5 | 重み付き percentile が主張どおりか | **単調性は成立、一様重み == `torch.quantile` はテスト済み、しかし複製等価ではない。** 単調性は代数（`p_{i+1}-p_i = w_i·B_i + A_i·w_{i+1} >= 0`）と randomized 300 ケースの検査（違反 0）で確認。一様重み一致は `test_regression.py:153-173` で固定済み。非一様重みでの定義は未固定で、他 metric と意味がずれる（S1） |
| 6 | テストが実装に寄っていないか | **1 件あり（M2）。** 過学習テストが実測 0.4621 に対し閾値 0.5、かつ seed 制御が無効。それ以外は計画書「テスト観点」の正常系 15 項目・異常系 11 項目・エッジ 6 項目がすべてテストになっている。抜けているのは計画書に無く実装者が足した挙動（S6）。「mean は常に正」→「非負」の緩和は orchestrator が裁定済みで、下流（`valid_sample_mask` の `mean > 0`）との整合も確認済み |
| 7 | deepcopy 後の同一 weight 確認の削除 | **妥当。** 同じ source から `copy.deepcopy` を 2 回呼んだ直後に weight が食い違う経路がない。唯一の反例候補である lazy module（`nn.LazyLinear` 等）は forward まで parameter が実体化しないため、削除された「forward 前の weight 比較」では検出できない。確認を入れても死にコード |
| 8 | ドメイン非依存 | **守られている。** `src/ml/` に paste / volume / nozzle / machine / dispense / µL / pixel_per の語なし（grep 確認）。v1 の channel 数 24 / 32 / 48 / 96 / 160 も無し。`ImageEncoderConfig` は channel・stride に既定値を持たず、`group_norm_groups=8` だけ。head の `hidden_features=128` と log 分散範囲 (-14, 5) は doc §2 の v1 値だが計画書が明示的に既定と定めたもの（nit で言及）。`GaussianRegressionHead` は `conditioning: [B, K]` を受けるだけで `log(pixel_per_mm)` を知らない |

## 検証結果

- `make format`: pass（全 hook Passed、tree 無変更）
- `make type`: pass（pyright 0 errors, 0 warnings）
- `make test-no-hardware`: pass（3107 passed / 140 deselected / 112.17s）
- 成果物汚染（`</content>` 等）: なし（`src/ml` / `tests/ml` / `tests/helpers.py` を grep）
- 実機テスト（`make test` / `pytest -m hardware`）は不実行（ユーザー実施）
