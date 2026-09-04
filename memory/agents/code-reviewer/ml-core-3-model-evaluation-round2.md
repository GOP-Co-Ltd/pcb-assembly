# コア ML 基盤 MR3 レビュー（2 巡目：修正の妥当性確認）

1 巡目: `memory/agents/code-reviewer/ml-core-3-model-evaluation.md`（request-changes、M1 / M2 / S1-S9 / nit 8）
裁定: `memory/agents/orchestrator/ml-core-3-model-evaluation.md`
修正報告: `memory/agents/plan-implementer/ml-core-3-review-fixes.md`
対象: staged 差分 30 file / 4,954 行。

## verdict: approve

先行指摘 19 件（M1 / M2 / S1-S9 / nit 8）は**すべて解消**をコードで確認した。
新規の must-fix はない。以下は新規の should-fix 3 件と nit 4 件。

## 先行指摘の解消確認

| # | 判定 | 確認方法 |
| --- | --- | --- |
| M1 | 解消 | 後述 |
| M2 | 解消 | 後述 |
| S1 | 解消 | docstring `regression.py:266-273` に複製非等価を明記。`test_fixes_the_percentile_of_non_uniform_weights` / `test_does_not_match_replicating_samples_by_their_weight` で固定。位置 `[0, 0.6, 0.8, 1.0]` と median 0.4167 / p95 2.5 を手計算で照合済み |
| S2 | 解消 | 後述 |
| S3 | 解消 | `slices.py:222` の `if members`。randomized 3,000 ケース（重複値多め）で空 bucket **0 件**（1 巡目は 440 件）。sample の取りこぼしなし（各ケースで `sum(len(members)) == n`）。最初と最後の区間は min / max を必ず含むので落ちない |
| S4 | 解消 | 後述 |
| S5 | 解消 | `_UsableSamples` / `_usable_samples`（`regression.py:114-163`）へ集約。`gaussian_regression_metrics` と `fit_gaussian_log_variance_offset` の両方が経由 |
| S6 | 解消 | (a) `test_returns_no_bins_below_one_bin` parametrize [0, -1] (b) `test_reports_the_relative_difference_of_each_output` が 0.5 と「両側 0 → 0.0」を実比較経路で固定 (c) `_DriftingModel` / `_FirstCallParameterModel` が mismatched / missing を実比較経路から非空に (d) `test_rejects_non_finite_values` の parametrize に `sample_weight` 追加 |
| S7 | 解消 | `compile_parity.py:190`「compile 済み model の実行に失敗しました」 |
| S8 | 解消 | 後述 |
| S9 | 解消 | `test_summarizes_each_bin_with_weighted_statistics` が RMSE `sqrt(7/5)` / coverage `4/5` を手計算固定。時間計測テストは後述 |
| nit 8 件 | 解消 | `blocks.py:59, 90` とも `inplace=True`（`residual + shortcut` は一時 tensor なので autograd 安全）／`_require_output_tuple`／`span <= 0` 分岐削除／`CompileOptions.validate()` 追加・実呼び出し／`padding_pixel` docstring を「勾配の観測」に／slice reason に slice 名／head 既定値は裁定どおり据え置き／`state_dict()` キーは MR5 申し送り |

### M1（reliability bin の重み付き集計）

`slices.py:293-312` の `_reliability_bin` は 3 統計とも `sum(w·x)/sum(w)`。
overall / slice との同一性を randomized 1,499 ケース（無効 sample・weight 0 混在）で照合し、
**最大相対差 2.5e-16**（float 誤差のみ）。`test_uses_the_same_definitions_as_the_overall_and_slice_metrics`
が 3 統計すべてを overall と slice の両方に対して固定している。

bin の境界は `_bin_edges` の等件数のままで変わっていない。母集合が
「全 sample」→「`valid_sample_mask() & (weight > 0)`」に変わったのは M1 の要求どおり
（寄与 0 の sample が bin を占有する 1 巡目の実測不具合はこれで消える）。
`_bin_edges` は最後に必ず `total > edges[-1]` を append するので空 bin は作れず、
bin 内 weight 合計は必ず正。randomized 2,000 ケースで `sample_count == 0` と非有限統計は 0 件。

`sum(bin.sample_count) != overall.valid_sample_count`（weight 0 の有効 sample の分だけずれる）は
`ReliabilityBin` docstring に明記され `test_excludes_zero_weight_samples_from_the_bins` で固定済み。

### M2（過学習テスト）— 契約の弱体化には当たらない

修正者の根拠を独立に再現した。8 seed × 300 step の実測:

| 統計 | 範囲 |
| --- | --- |
| `last / first` | 0.081 – 0.538 |
| `best / first` | 0.0041 – 0.0286 |
| 末尾 30 step の平均 / first | 0.056 – 0.546 |
| 末尾 30 step の最小 / first | 0.0041 – 0.538 |

- 「最終値が不安定」は事実。`last/first` は seed で 6.6 倍ばらつく。閾値を最終値に置くのは不可能。
- **修正者が挙げなかった代替も試した。** cosine decay（300 step / lr 0.02）で `last/first` 最悪 **0.317**、
    lr を 0.005 に落として 300 step で最悪 **0.116**。どちらも 0.1 を割れない。移動平均・末尾 min も
    上表のとおり最悪 0.5 台で、**best より弱い主張しか書けない**。
- したがって「history 中の最良値 < 初期比 0.1」（余裕 3.5 倍）は、この計画書の観点
    「勾配が model 全体へ流れ、少数 sample を過学習できる」を検証する手段として妥当。
    「収束が安定していること」の検証は失われるが、それは MR4 の学習ループの観点であり
    MR3 の計画書に無い。契約の弱体化ではなく、契約の言い直しと判定する。
- `torch.manual_seed(41)` の死んだ seed も削除済み。
- 確信度: 高（8 seed の軌跡と代替案 2 種を実測）

### S2（`@attrs.frozen(eq=False)`）

`GaussianPredictions`（`regression.py:26`）/ `PaddedBatch`（`batch.py:36`）/
`PreprocessedSample`（`image.py:105`）の 3 型とも class 側 `eq=False` へ移動し field 側を削除。
リポジトリ全体を grep して、3 型の**等価比較・hash・set / dict キーとしての利用箇所は 0 件**。
`PreprocessedSample` は従来 `scale` だけで `__eq__` していた（Tensor 2 本が `eq=False`）ので
挙動が変わるが、利用箇所が無いため影響なし。

### S4（bucket ラベルの一意性）

`_boundary_labels`（`slices.py:228-242`）の while 終了保証は成立する。
`_bucket_edges` は `boundary > edges[-1]` でしか append しないので境界は狭義単調増加、
異なる float64 は `%.17g` で必ず異なる文字列になる（round-trip 保証）ため 17 桁までに必ず抜ける。
隣接 double 7 本を含む 4,000 ケースの fuzz で衝突 0 件・ハングなし（最長ラベル 22 文字）。
非有限値は `NumericDimension.validate()` が弾き、仮に NaN 単独でも `set` サイズ 1 で即終了する。

### S8（doc §3）

`docs/…-ml-plan.md:512-514` の変更は「正規化誤差 e」→「相対誤差 `e = (mean - target) / target`」
＋実装名 3 つの併記のみ。primary gate の式は §3 も §6（`:730` の `abs(mean(e)) + std(e) <= 0.10`、
`:755` の INT8 悪化 0.01）も未変更。gate は元から無次元の 10% 判定なので、
相対誤差という定義の明示は**意味を変えず曖昧さを消す方向**。

## should-fix

### N1. 極端な weight 比で `median` / `p95` が黙って `nan` になる

- `src/ml/evaluation/regression.py:284`（`positions = exclusive / (total - sorted_weight)`）
- ある sample の weight が float64 で合計に等しくなると `total - w_i` が 0 になり、
    `positions` に `inf` / `nan` が入って狭義単調増加が崩れる。`torch.searchsorted` の前提が壊れる。
- 実測: `values=[0.5, 3.0]` / `weight=[1.0, 1e-20]` で `_weighted_percentile -> nan`。
    公開 API 経由でも `gaussian_regression_metrics` が
    `median_absolute_relative_error = nan`、`p95_absolute_relative_error = nan`、
    **`reason = None`** を返す。無効値として弾かれず report に nan が載る。
- **これは今回の修正で入った欠陥ではない**（`positions` の式は 1 巡目から不変）。ただし
    1 巡目 nit「`positions` は狭義単調増加なので `span <= 0` は到達不能」という削除根拠は、
    この underflow 域では成り立たない。削除自体は妥当（`span` 到達前に既に nan なので
    あのガードでは救えない）が、前提が docstring の
    「重みが正の sample が 1 件以上あることを前提」ではカバーされていない。
- 実ドメインの weight は `1/(session 内 pad 数) × 1/(pad 内 view 数)` で比は高々 1e4 程度。
    現実的な散らばり 1e-4〜1 の randomized 20,000 ケースで異常 0 件。ただし `ml/` は
    ドメイン非依存で任意の weight を受ける契約なので、docstring に前提
    （weight の比が float64 精度内であること）を 1 行足すか、
    `gaussian_regression_metrics` の戻り値の有限性を確認するかを決めたい。
- 確信度: 高（挙動は実測）／中（対応要否は設計判断）

### N2. `_reliability_bin` の重み付き平均再実装（修正者の判断 1 への判定）

- `src/ml/evaluation/slices.py:298-304` と `src/ml/evaluation/regression.py:253-254`
- 「`regression._weighted_mean` の cross-module import は `reportPrivateUsage`」という
    修正者の理由は正しい（`pyproject.toml:126` で `warning`、リポジトリは 0 warning 運用、
    実際に別プロジェクトで再現確認した）。public 化を避けた判断も妥当。
- **ただし第 3 の選択肢が検証済みで存在する。** 私有 module に public 関数を置く形
    （`src/ml/evaluation/_aggregation.py` に `def weighted_mean(...)`）なら
    pyright は警告を出さない。`from pkg._aggregation import weighted_mean` は無警告、
    `from pkg._aggregation import _private_mean` だけが警告になることを実機の pyright で確認した。
    `ml/evaluation/__init__.py` は何も re-export していないので、パッケージの公開 API も増えない。
- 加えて現在の `torch.stack((...)) * weight).sum(dim=1).div(weight.sum())` は、
    数百要素の bin に対する最適化としては利得が無く、`_weighted_mean` を 3 回呼ぶ形より読みにくい。
- 緩和材料: `test_uses_the_same_definitions_as_the_overall_and_slice_metrics` が
    overall / slice との一致を固定しているので、S5 と違い**黙って drift はしない**。
    リポジトリに私有 module の前例が `__main__.py` しか無い点も考慮が要る。
- 判定: 判断としては許容範囲。ただし「他に手が無かった」ではないので、
    `_aggregation.py` 案の採否は orchestrator が決めるべき。
- 確信度: 高（pyright 挙動は実測）／中（採るべき案は設計判断）

### N3. `PaddedBatch` / `PreprocessedSample` の identity 比較がテストで固定されていない

- `src/ml/data/batch.py:36`、`src/ml/data/image.py:105`
- `GaussianPredictions` には `test_compares_by_identity`（`test_regression.py:151-157`）があるが、
    同じ裁定で同時修正した残り 2 型には無い（`tests/ml/data/` を grep して確認）。
- S6 で orchestrator が示した原則「裁定で受け入れた挙動は契約としてテストに落とす」が
    ここにも当たる。特に `PreprocessedSample` は `scale: float` を持つため、
    `field(eq=False)` の壊れた形へ戻しても `__eq__` は「scale だけ一致で真」になり、
    テストが無いと気付けない。
- 確信度: 高（テストの有無は grep で確認）

## nit

- `tests/ml/model/test_heads.py:227` の `assert best_loss < first_loss` は、
    300 step 中 1 step でも改善すれば通るのでほぼ恒真。S9 で潰した限界価値テストと同種。
    `best_error` の行があれば足りるので削るか、絶対量を伴う条件にする。
- `tests/ml/evaluation/test_compile_parity.py:266` のコメント「実測 86 倍」。
    実測すると単独 process では `setup/compiled` が 2,082〜2,367 倍、
    他テストで dynamo が温まった process では 26〜70 倍（setup 37〜46 ms / compiled 0.5〜1.8 ms）。
    数字が片方の条件しか表していない。なお flaky ではない
    （`compiled_seconds` が 40 ms へ跳ねる必要がある。3 回連続実行で 32 passed を確認）。
    `sum <= elapsed` の側は perf_counter の単調性から構造的に必ず成立する。
- `_DriftingModel` / `_FirstCallParameterModel` の期待値（相対差 0.5、`_extra` のみ missing）は
    「compile 側だけ warm-up で 2 回走る」という内部回数に依存する。
    `CompileParityResult` docstring が「compiled_seconds は 2 回目の pass」と明示しているので
    公開契約の範囲内であり、回数が変われば `approx(0.5)` が大きく外れて落ちる（黙って通らない）。
    許容だが、テスト側にもその依存を 1 行書いておくと読み手が助かる。
- `DiagnosticSlice.reason` が `"machine=beta: 有効な sample がありません: …"` となり、
    `dimension` / `value` の構造化フィールドと内容が重複する。MR6 で JSON に落とすときに冗長。

## 全体の再確認

- **ドメイン非依存**: `src/ml/evaluation/` `src/ml/model/` に
    paste / volume / nozzle / dispense / µL / pixel_per / machine / pcb / solder の語なし（grep）。
    v1 channel 数 24 / 32 / 48 / 96 / 160 も `blocks.py` / `heads.py` に無し。
- **成果物汚染**: `</content>` 等の混入なし。新規 9 file と `tests/helpers.py` の末尾を目視確認。
- **`tests/helpers.py`**: `skip_if_no_inductor` は既存の `_skip_unless_available` 機構に沿っており、
    `torch` を関数内 import して collection を守る形も既存 helper と同型。

## MR5 / MR6 への申し送り

- `sum(bin.sample_count) != overall.valid_sample_count`（weight 0 の有効 sample 分ずれる）。
    evaluation.json を読む側が件数の突き合わせをしないよう注意。
- N1 の nan は MR6 の evaluation.json にそのまま載りうる。書き出し側で有限性を見るか、
    N1 を regression 側で塞ぐかを MR6 着手時に決める。
- 1 巡目の `state_dict()` private キー問題は未対応のまま（orchestrator メモどおり）。

## 検証結果

- `make format`: pass（全 hook Passed、tree 無変更）
- `make type`: pass（pyright 0 errors, 0 warnings）
- `make test-no-hardware`: pass（3125 passed / 140 deselected / 116.68s）
- `tests/ml/evaluation/test_compile_parity.py` 単独 3 連続実行: 32 passed ×3（時間計測テストの flaky 確認）
- 実機テスト（`make test` / `pytest -m hardware`）は不実行（ユーザー実施）
