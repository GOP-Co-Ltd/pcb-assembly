# 点塗布・多視点への `src/ml/` 適応（実装ノート）

計画書: `memory/agents/implementation-planner/ml-point-dispense-model.md`

## 計画外の判断ログ

### 1. `ViewDropout` は計画書に無い。orchestrator の裁定で新設した

ユーザー決定 3（view dropout を入れる）と orchestrator 追加決定（batch ごとに V を
揃える）を受けて `src/ml/data/batch.py` に新設した。計画書にはシグネチャが無いので
実装前に orchestrator へ提案し、1 点修正（異常系を `ValueError` ではなく
`str | None` へ）を受けて確定した。

```python
@attrs.frozen
class ViewDropout:
    minimum_view_count: int = 1

    def validate(self) -> str | None: ...

    def view_indices_for(
        self,
        sample_ids: Sequence[str],
        *,
        available_view_count: int,
        global_seed: int,
        epoch: int,
    ) -> tuple[tuple[tuple[int, ...], ...] | None, str | None]: ...
```

- 戻り値は sample ごとの view 番号列。batch 内で全て同じ長さ・昇順・重複なし
- 決定論の材料は `(global_seed, epoch, sample_ids)`。`plan_pixel_budget_batches` が
    既に使っている「文字列 seed の `random.Random`」に合わせ、`image.py` の
    `_derived_seed` を跨いで import しない
- `available_view_count < minimum_view_count` は理由文字列（MR6 の裁定 S2 と同型）。
    1 view で収集した session に `minimum_view_count=3` を当てれば実データで起きる
- `minimum_view_count == available_view_count` で実質無効化。評価・推論は呼ばない

### 2. `!207` schema の確認結果

`PasteDatasetConfig.view_count` は session 単位の設定で、`PasteDatasetSample.views` /
`PasteDatasetBlank.views` は `tuple[DatasetCapturedView, ...]`。1 session 内では view 数が
揃い、composite manifest で session をまたぐときだけ混ざる。`_bucket_key` に
`view_count` を入れれば別 batch へ分かれるので、**batch 内 V 均一は成立する**。

副次的な利得: 計画の E4 は「均一サイズが bucket を 1 個へ潰し `_bucket_key` の
aspect / area 項が空洞化する」だったが、`view_count` を鍵に足したことで
**bucket 鍵そのものは production で意味を持つ**ようになった。

### 3. `MultiViewImageEncoder` の入力検査を ndim 2 本だけにした

計画書は「ndim != 5、channel 数不一致、mask の dtype と shape」を挙げつつ「既存
`ImageEncoder._reject_invalid_inputs` と重複する検査を足さない」とも書いており、
両立しない。`flatten(0, 1)` 後に既存 encoder が channel 数・mask dtype・mask shape を
すべて捕まえるので、multiview 側は `images.ndim != 5` と `valid_pixel_mask.ndim != 5`
だけにした（orchestrator 承認済み）。

理由: 同じ不整合に検出器を 2 つ置くと前段が後段を隠し、`ml.export` の変異実験で
survivor の最大要因になった構造をこちらから作ることになる。`blocks.py` へ
`input_channels` property を足す案は却下された。

理由文には `[B, V, C, H, W]` / `[B, V, 1, H, W]` を含めてある。

### 4. `_fill_batches` のコストは view 数の総和で数える

計画書は `len(candidate) * view_count * ceil(H) * ceil(W)`。bucket 鍵に `view_count` が
入るので candidate 内は必ず均一だが、`max()` や `candidate[0]` を書くと「起こり得ない
不揃い」への態度を決める必要が出る。`sum(item.view_count for item in candidate)` は
均一なら同じ値になり、一般にも padding 後の実画素数として正しいので、こちらを採った。

### 5. `image.py` の幾何処理をヘルパーへ切り出した

`PreprocessedSample.preprocess` と `PreprocessedMultiViewSample.preprocess` が
resize / rotate / mask 生成を共有するので、`_target_shape` / `_transformed_images` /
`_valid_pixel_mask` の 3 関数へ分けた。単視点側の観測可能な振る舞いは変えていない。

`PreprocessedMultiViewSample` は mask を 1 枚だけ作って全 view で共有するので、
view 間の mask は `torch.equal` ではなく**同一オブジェクト**になる。

### 6. `validate_augmentation` は先に `AugmentationRange.validate()` へ委譲する

`minimum_scale` が非有限だと `math.floor` が例外になる。設定 file 由来の
`AugmentationRange` が未検証で届くのは現実的な経路なので、先頭で委譲した。
計画書に無い分岐が 1 本増えている。

### 7. `predict` の入力検査（ユーザー決定 5）の範囲

`_mismatched_inputs` / `_mismatched_input` を `ml/export/runtime.py` へ足した。
検査は 4 つ。

1. manifest が宣言する入力名の集合と、渡された辞書のキー集合の一致
2. 要素型（`_ELEMENT_TYPE_DTYPES` は `{"FLOAT": "float32"}` のみ。未知の要素型は
    「推論経路が扱えません」を返す。`InputValues` の型注釈が float32 限定なので
    ここを広げる根拠が今は無い）
3. 軸数（`len(contract.dimensions)`）
4. 固定軸（`dimension.isdigit()` の軸だけ厳密一致。symbol 軸は見ない）

`ModelSize.measure` の `AttributeError`（E7 後段）は範囲外。**未修正のまま残っている**。

## 他 implementer への IF 変更通知

- `BatchShape` に `view_count: int = 1` を末尾へ追加した。既存の位置引数 3 個の
    呼び出しは影響を受けない
- `PaddedBatch.pad` の既定 `stride` が 32 から 8 になった
- `ImageConstraints` の既定が `(16, 512, 262_144, 8)`、`AugmentationRange` の既定
    scale が `0.5〜2.0` になった
- `GaussianHeadConfig` に `mean_bias_initial: float = 1.0` を末尾へ追加した。
    `GaussianRegressionTask.reduce` の戻り値 key に `MeanSaturationDiagnostic` の
    6 項目が常時加わる
- `DiagnosticReport` に `zero_target: ZeroTargetMetrics | None` と
    `mean_saturation: MeanSaturationDiagnostic` が**位置引数として**増えた。
    `DiagnosticReport(...)` を直接構築している箇所は追従が要る
- `ml.data.image` が `ml.model.blocks.ImageEncoderConfig` を import するようになった
    （`validate_for_encoder` のため）。どちらも RUNTIME 層で循環しない

## 既知の制約・残課題

### R-A. ReLU 化で平均 head が死ぬ問題（裁定済み・対応済み）

`GaussianRegressionTask.reduce` は `GaussianRegressionMetrics.measure` が `None` を
返すと空写像を返す。`valid_sample_mask` が `mean > 0` を要求するので、**batch 内の
全 sample の mean が 0 になると monitor が 1 つも出ず、学習 loop が
`ValueError: validation の集計に monitor 'relative_error_score' がありません`
で落ちる**。ReLU 化直後は `tests/ml/training/` の 26 件がこれで落ちていた。

実測（`tests/ml/support.build_synthetic_model`、batch 3 件）。6 seed 中 4 seed で
`mean == [0.0, 0.0, 0.0]`。`GaussianRegressionHead` 単体に `randn(64, 16)` の feature を
与えると 40 seed で全滅 0 回・平均飽和率 54% なので、全 sample が揃って飽和するのは
trunk の ReLU 出力が sample 間でほとんど変わらないため。**`self._mean.bias` の初期値が
`Uniform(-1/sqrt(128), 1/sqrt(128))` で約半数が負**になり、batch 全体が同じ負の前活性へ
落ちる。orchestrator 側の 300 seed 再現では bias が負 151/300、全 sample 厳密 0 が
145/300。

`spec-test-author` の追加実測で、**全 sample 飽和時は `_mean` の weight / bias への
勾配も 0 になり、Adam(lr=0.02) を 200 step 回しても厳密 0 のまま**であることが
確かめられた。平均出力は 1 unit しかないので、一度全域で死ぬと復帰しない。計画の
E5 は「飽和 sample の前活性への勾配が 0」までしか見ていなかった。

**対応 1（orchestrator 裁定）: `GaussianHeadConfig.mean_bias_initial: float = 1.0`。**
`__init__` で `nn.init.constant_(mean_layer.bias, config.mean_bias_initial)` を掛ける。
定数をハードコードしないのは、望ましい初期平均が真値のスケール（この用途では
0.05〜0.2 µL）に依存するドメイン知識だから。`ImageEncoderConfig` が形の既定値を
持たないのと同じ理屈。`validate()` は有限かつ正であることを要求する。ReLU 出力 head に
非正の bias 初期値が望ましい場面が無いので、選択肢として残さない。

**対応 2（orchestrator 追加裁定）: `reduce` が飽和の診断を必ず返す。**
`src/ml/training/task.py` を編集範囲に加える許可を得たうえで実装した。採った形は
「`MeanSaturationDiagnostic` の 6 項目を、回帰 metric が出せるかどうかに関わらず
常に返す」。

- Trainer の「monitor が無ければ例外」は**変えていない**。評価できない run を
    続行できないのは正しく、緩めると promotion gate の意味に触る
- `valid_sample_mask` の契約も**変えていない**
- 失敗したときだけ現れる指標は run をまたいだ推移を追えないので、正常時も同じ
    key で 0.0 を出す。値が 0.0 から動いた瞬間に気付ける形にした
- `predictions.validate()` が理由を返すときだけ空写像を返す（従来の
    「集計できないときは空」という ABC 側の契約を残すため）
- `attrs.asdict` を float 写像へ落とす `_as_float_mapping` を私有関数として置いた

結果として `reduce` の key 集合は
`GaussianRegressionMetrics` の全項 + `MeanSaturationDiagnostic` の全項になった。
全 sample 飽和時は後者だけが残る。

### R-B. `tests/ml/export/test_benchmark.py:390`（`spec-test-author` へ移管済み）

`"推論に失敗しました" in error` を assert しているが、決定 5 の入力検査で入力名の
食い違いが ORT ではなく `ml` 側で弾かれるようになったので文面が変わった。
`spec-test-author` の `tests/ml/export/test_runtime.py` は逆に
`"推論に失敗しました" not in error` を要求しており、**新しい契約が正**。
orchestrator が `spec-test-author` へ更新を割り当てた。単に文字列を差し替えるのではなく
「`ml` が弾いた理由文」を期待する形にすること、という指示付き。

### R-C. spec 側の取り違え（`spec-test-author` へ移管済み）

`tests/ml/evaluation/test_regression.py::TestMeanSaturationDiagnostic::
test_an_empty_population_reports_a_zero_fraction[mean1-target1]`。
`mean=[0.0, 0.0]` / `target=[0.0, 0.0]` は「真値 0 の母集団が空」ではなく
「真値 0 が 2 件、2 件とも飽和」なので `saturated_zero_fraction == 1.0` が正しい
（blank を正しく 0 と当てた状態）。母集団を空にするなら `target` を正の値にする。

### R-D. 計画の R4（`stride` の二重管理）は残っている

`ImageConstraints.stride` は `validate()` 以外から読まれない。実際の padding を決めるのは
`PaddedBatch.pad(stride=)` と `plan_pixel_budget_batches(stride=)`。既定値を 8 へ
そろえただけで、配線の一本化は別 MR。

### R-E. `ModelSize.measure` の `None` 耐性（E7 前段）は未着手

ユーザー決定 5 で「範囲外、ノートに記録のみ」とされたので手を付けていない。

### R-F. encoder の形（`stride8_A`）は `ml` へ入れていない

計画書どおり、`ImageEncoderConfig` の具体値はドメイン設定として渡す。`ml` 側の追加は
`ImageConstraints.validate_for_encoder` だけ。

## 検証結果

`make ml-docker-check` は `.git` ロック競合を避けるため使わず、個別に実行した。

- `pre-commit`（変更した src 9 ファイル、docformatter を含む全 hook）: **pass**
- `pyright src/ml scripts/ml_smoke.py`: **pass**（0 errors / 0 warnings）
- `pyright src/ml tests/ml scripts/ml_smoke.py`: **6 errors**。すべて
    `tests/ml/model/test_multiview.py:363-364` の
    `Cannot access attribute "shape" for class "SparseTensor" / "list" / "dict"`。
    `spec-test-author` の新規 file 側の型注釈の問題で、`src/ml` 由来ではない
- `python scripts/ml_smoke.py`: **pass**（すべて通過）
- `pytest tests/ml -m "not hardware and not e2e"`:
    **1263 passed / 1 failed / 1 skipped**
    - 残る 1 件は
        `tests/ml/training/test_task.py::TestObservationTyping::
        test_step_result_carries_the_observation_type`。同じ file の
        `test_reduce_keys_match_the_regression_metric_fields` が
        `METRIC_FIELD_NAMES | SATURATION_FIELD_NAMES` を要求しているのに対し、
        こちらだけ `METRIC_FIELD_NAMES` のまま。`spec-test-author` 側の更新漏れ

### export の追加知見（`spec-test-author` 報告、`ml` 側は未対応）

**batch 軸は `images` と `conditioning` の両方へ dynamic 宣言しないと export が
失敗する**（`Received user-specified dim hint Dim.DYNAMIC, but tracing inferred a
static shape of 2`）。head が両者の batch size を突き合わせるため。計画の E2 は
mask 付き・conditioning 無しで検証していたので拾えていなかった。export ヘルパーを
書くときはこれを踏まえること。

### docformatter の書き換えを 2 箇所で踏んだ

いずれも「1 文を 2 行に折り返した説明段落」を docformatter が 1 行へ連結した。
`src/ml/export/runtime.py` の `predict` docstring と `src/ml/model/multiview.py` の
`MultiViewImageEncoder` docstring。日本語の化けは起きなかったが、連結痕
（`根拠が 無いため` のような不自然な空白）が残るので、**説明段落は 1 文 1 行**まで
詰めて書き直した。検出は
`grep -rnP '[\x{3000}-\x{9FFF}、。] [\x{3000}-\x{9FFF}]' src/ml/` が使える。

---

## 変異実験フェーズ

対応表 T1〜T38 + S1〜S5 を、`src/` の 8 file へ 1 件ずつ当てて実測した。
手順は同期・小バッチ（1 回の Bash 呼び出しが返る時点で全復元済み）、
各バッチの前後で pristine の sha256 照合と `git diff --name-only` の src 判定。
**`git add` / `commit` / `stash` / `checkout` は一度も使っていない。**
`git restore --worktree <file>` のみ。**index は触っていない。**

- 変異総数 **161 件**（round 1）、round 2 で survivor 34 件を `tests/ml` 全体へ拡張、
    補強後の round 3 で 31 件を再検証、最後に全 161 件を再走
- 最終: **161 / 161 killed（真の survivor 0 件）**
- `tests/ml` = **1296 passed / 1 skipped**（変異前 1264 / 1、+32 本）、pyright 0 errors

### module ごとの survivor

| module | 変異数 | 初回 survivor | 全 suite 拡張後 | 補強後 |
| --- | --- | --- | --- | --- |
| `data/image.py` | 43 | 4 | 4 | 0 |
| `data/batch.py` | 49 | 13 | 12 | 0 |
| `model/multiview.py` + `model/heads.py` | 31 | 6 | 6 | 0 |
| `evaluation/regression.py` + `slices.py` + `export/runtime.py` + `training/task.py` | 38 | 11 | 10 | 0 |
| **計** | **161** | **34** | **32** | **0** |

初回 survivor 率 21%（MR6 は 36%）。MR6 の対策（理由文まで見る・1 分岐 1 ケース）が
効いている一方、**残った穴の性質は MR6 と同型**だった。

### 落ちなかった変異と補強（32 件）

#### A. 後段の分岐が前段を隠す（MR6 原因 1、7 件）

| 変異 | 生き残った理由 | 補強 |
| --- | --- | --- |
| `validate_augmentation` の `AugmentationRange.validate()` 委譲を削除 | 壊れた範囲を渡すテストが 1 本も無かった | `minimum_scale=nan` で範囲側の理由文を**完全一致**で固定 |
| `sample_layer_norm` の `count == 0` guard | 直後の `isfinite` guard が同じ `None` を返す | `test_rejects_samples_without_usable_statistics` に期待理由を parametrize（3 ケースとも別の理由） |
| `ZeroTargetMetrics` の `predictions.validate()` 委譲 | 長さ不一致のケースが無かった（消すと `&` の broadcast 例外） | 長さ不一致で `件数` を含む理由を返すテスト |
| `ZeroTargetMetrics` の `sample_count == 0` guard | 後段の `weight_sum <= 0` が同じ `None` を返す。既存 assert は `"真値 0" in reason` で**両方に一致していた** | 理由文を `== "真値 0 の sample がありません"` へ |
| `predict` の `expected_dtype is None` guard | 次の `value.dtype.name != expected_dtype` が `None` と比較して「dtype 不一致」を返す | manifest の `element_type` を `BOOL` にした package で `扱えません` を見る |
| `_ELEMENT_TYPE_DTYPES` へ `BOOL` を追加 | 写像の中身を観測する経路が無かった | 同上のテストで同時に潰れた |
| `_validate_multi_view_batch_inputs` の先頭 `ndim != 4` と loop 内 `ndim != 4`（**相互に隠し合う 2 件**） | 3D 先頭画像は両方が捕まえる | `images[0]` が 1 次元（先頭検査を消すと `shape[1]` が `IndexError`）と **2 件目**が 3D のケースを parametrize |

#### B. 3rd-party / 後続計算が最後の砦（MR6 原因 2、3 件）

| 変異 | 生き残った理由 | 補強 |
| --- | --- | --- |
| `plan_pixel_budget_batches` の `stride < 1` | `_ceil_to(v, 0)` が `ZeroDivisionError` を投げる（`ValueError` ではない） | `pytest.raises(ValueError, match="stride")` |
| `_validate_batch_common` の `stride < 1` | 同上 | `PaddedBatch.pad(..., stride=0)` |
| `plan_pixel_budget_batches` の `max_batch_pixels < 1 or max_batch_size < 1` | `pixels=0` は「1 sample が超えます」へ化け、`size=0` は無言で 1 件ずつに割れる | 理由文 `正の整数` を 2 ケースで固定 |

#### C. 観測点が機構に対して鈍い（新しい類型、6 件）

**MR6 の 4 類型に無かった形。**「テストの assert は正しいが、
その入力配置では機構の差が出力に現れない」。

| 変異 | 生き残った理由 | 補強 |
| --- | --- | --- |
| `.mean(dim=1)` → `.amax(dim=1)` | **T20（順序不変）と T21（同一 view 反復）を最大値も満たす**。この 2 本では平均を特定できない | 違う 2 view を 1 枚ずつ通した結果の**算術平均**と厳密に突き合わせる |
| `MultiViewGaussianRegressor.forward` の出力順を swap | shape と `mean >= 0` しか見ていなかった | `head(encoder(images, mask), cond)` との `torch.equal` |
| 同 forward が mask を捨てて `None` を渡す | 同上 | 同上。**ただし 1 度目の補強も survivor だった**（下記 D） |
| `_placement` の `if not training` → `if True` | 既存テストは学習時配置の**再現性**しか見ていない。中央固定でも再現性は成り立つ | 評価時 mask と学習時 mask が**一致しないこと**を見る |
| `_fill_batches` の cost から `_ceil_to` を落とす | 既存の予算テストは 64x64（stride 8 で切り上げ無し）だった | 53 px 源 + 予算 `2 * 56² - 1` で 1 batch 1 件になることを固定 |
| `alone` から `view_count` を落とす | 「1 sample が予算超過」のテストが V=1 だけだった | `BatchShape(..., view_count=5)` で予算超過を作る |

#### D. 既定値・種の材料に観測点が無い（5 件）

| 変異 | 補強 |
| --- | --- |
| `ViewDropout.minimum_view_count` 既定 1 → 2 | 既定値の直接 assert + 1 view session が通ることの確認 |
| `view_indices_for` の seed 材料から `sample_ids` を落とす | 同じ seed / epoch で `sample_ids` を変えると結果が変わることを固定 |
| `keys = sorted(buckets)` → `list(buckets)` | 既存の「入力順に依存しない」テストは**単一 bucket**だった。5 bucket 版を追加 |
| `BatchShape.view_count` 既定 1 → 2 | **補強せず。** `tests/ml/training/test_loop.py::TestTrainerFullRun` 2 本が全 suite で捕まえる |
| `valid_sample_mask` の `mean > 0` → `>= 0` | **補強せず。** `test_a_fully_saturated_evaluation_still_reports_why` が全 suite で捕まえる |

#### E. 厳密比較を緩める変異（4 件）

`mean == 0` / `target == 0` を `<= 0` へ緩める変異は、既存テストの入力に
**負の値が 1 つも無かった**ため全て生き残った。ReLU の出力は負にならないが
`GaussianPredictions` は他 model の予測も受け取るので、契約としては区別が要る。

- `ZeroTargetMetrics.exact_zero_fraction` の `mean == 0` → 負の平均を含むケースを追加
- `MeanSaturationDiagnostic` の `mean == 0` → 同上
- 同 `target == 0`（真値 0 母集団）→ 負の真値を含むケースを追加
- `selected` の `isfinite` 3 本と `sample_weight >= 0` → **1 条件 1 ケース**の
    parametrize（5 ケース）を追加

#### F. `int()` の再導入が T35 をすり抜けた（**最も重要、3 件**）

`MultiViewImageEncoder.forward` の `batch_size` / `view_count` へ `int()` を戻す
変異が **3 件とも生き残った。** T35（`test_keeps_the_batch_view_and_spatial_axes_symbolic`）
は `OnnxExportResult.export` 経由で ONNX の `dim_param` を見ているが、
**MR6 の裁定 1 で判明した通り `torch.onnx.export(..., dynamo=True)` は非 strict の
失敗を黙って strict へ落とすので、ONNX の次元は `int()` の有無で変わらない。**

`ml.model.blocks` / `ml.model.heads` には MR6 が
`TestExportedDynamicShapes`（`torch.export.export(..., strict=False)` を明示）を
置いてあるが、**`ml.model.multiview` には無かった**。同型の class を
`tests/ml/model/test_multiview.py` へ追加した（mask 有り / 無しの 2 本）。
B・V・空間軸を同時に変えるので `int()` 3 変異すべてが落ちる。

**申し送り: 新しい module に `int()` を書かない forward を足したら、
`TestExportedDynamicShapes` を必ず対で置く。** ONNX 側の検査は検出器にならない。

### 「落ちにくい 4 か所」の実測（spec-test-author の申し送り）

| 箇所 | 予測 | 実測 |
| --- | --- | --- |
| `validate_augmentation` の `math.floor` → `round` | 境界ケース（31 / 32）が唯一の検出器 | **予測どおり killed。** `math.ceil` へ変えても killed。境界 parametrize が効いている |
| `_bucket_key` の `aspect` 項の削除 | `test_separates_different_aspect_ratios` だけが守る | **killed。** ただし `area` 項・`view_count` 項の削除も個別に killed |
| `unflatten(0, (batch_size, view_count))` の B/V 入れ替え | B=V の example では通る | **killed（7 failed + 3 errors）。** T20 / T21 が B≠V で書かれているため。`mean(dim=0)` へのずらしも killed |
| `view_indices_for` の `sorted(...)` 削除 | `test_every_row_is_sorted_and_free_of_duplicates` だけが守る | **killed。** `generator.sample` → `generator.choices`（重複可）も killed |

**4 か所すべて予測どおり守られていた。** 一方で、申し送りに無い
`.mean(dim=1)` → `.amax(dim=1)` は生き残った（上記 C）。
「順序不変」と「同一 view 反復で一致」の 2 条件は平均を一意に決めない。

### 実測で覆った見立て

- **`ViewDropout` の決定論は `sample_ids` を落としても観測できなかった。**
    既存の 3 本（再現性・大域乱数非依存・epoch 差）はいずれも
    「同じ `sample_ids` で 2 回呼ぶ」か「epoch を変える」形で、材料の 1 つを
    抜いても全部緑のままだった
- **device 検査は GPU 無しで観測できる。** `_reject_mixed_placement` の
    device 2 分岐は「単一 device の CI では到達不能」と判定しかけたが、
    `torch.device("meta")` の tensor で両方とも決定的に発火する。
    MR6 の教訓「到達不能と判定する前に実測する」がまた当たった
- **`GaussianRegressionHead` の trunk ReLU は初期化時点で batch 全体が死ぬことがある。**
    `test_multiview.py` の既定 seed 13 では実際に死んでおり、
    head の出力が bias 固定（`mean == 1.0`、`log_variance == 0.0272`）になって
    **encoder の feature 差を一切映さない。** 最初に書いた合成テストが
    mask 削除の変異を捕まえられなかった原因はこれ。seed 3 へ変え、
    「mask を捨てた出力と一致しないこと」をテスト自身が先に確かめる形にした

### 次 MR の候補（本フェーズで見つかった分）

1. **`mean_bias_initial` は平均 head の bias だけを直す。**`GaussianHeadConfig`
    の trunk（`hidden_features` の Linear + ReLU）は torch 既定初期化のままで、
    40 seed 中 1 seed（seed 13）で batch 全体が死んだ。学習は進むので F1 のような
    恒久停止ではないが、初期の数 step が無駄になる。`MeanSaturationDiagnostic`
    では検出できない（平均 head は生きていて bias 値を出すため）
2. `_reject_mixed_placement` の理由文が device 違いと dtype 違いで同一。
    理由コード分離（計画 R6）の対象に加える
3. `_validate_multi_view_batch_inputs` の先頭 `ndim != 4` は
    「`images[0].shape[1]` を読む前の guard」としてのみ必要。
    検証を loop へ寄せてから `view_count` を決める形にすれば 1 分岐減る

---

## レビュー対応ラウンド（code-reviewer の request-changes）

### なぜ変異 sweep が M2 の 2 件を拾えなかったか

レビュアーは `ViewDropout.view_indices_for` に 2 変異を当て、いずれも 1296 passed の
まま生存させた。私の sweep は同じ関数へ 11 変異（BAT-VD1〜VD11）を当てて全て killed
と報告していたので、**この 2 件は私の変異カタログに存在しなかった**。

再現した 2 変異と、私が当てていた最も近い変異:

| レビュアーの変異 | 私の最も近い変異 | 何が違ったか |
| --- | --- | --- |
| `count = generator.randint(min, avail)` → `count = self.minimum_view_count` | BAT-VD10（`randint(1, avail)` へ下限を変える）、BAT-VD6（count を sample ごとへ移す） | 私は **randint の引数と呼び出し位置**を変えた。**呼び出しを消して定数にする**変異が無い |
| `generator.sample(...)` を内包表記の外へ hoist | BAT-VD4（`sorted` 削除）、BAT-VD9（`sample` → `choices`）、BAT-VD6（count を loop 内へ） | 私は **式の中身**を変えた。**式を loop の外へ出す**という構造変異が無い |

原因は 3 つある。

1. **カタログを「ソースの字面」から導いた。** `ast` の `If` 列挙と式の書き換えを
   起点にしたので、生成できる変異が「演算子を変える / 引数を変える / 定数を変える /
   分岐を潰す」に偏った。**「loop の内と外を入れ替える」「変動する量を定数にする」は
   字面の局所編集では出てこない。** MR6 の survivor 4 類型はどれも「当てた変異が
   生き残る理由」の分類で、**「そもそも当てていない変異」という失敗様式が
   抜けていた。**
2. **`ViewDropout` は計画書の T 表に無い（S1、実装と並行して確定した）。**
   T1〜T38 には「どう壊すと落ちるか」の列があり、そこに書かれた機構は仕様側から
   変異が与えられていた。`ViewDropout` だけは私が実装コードを読んで変異を作ったので、
   1 の偏りがそのまま出た。**仕様由来の変異が無い機構が最も危険。**
3. **同じ偏りがテスト側にもあった。** 私が補強した BAT-VD7（seed 材料）/ VD11（既定値）
   も「引数・定数」系で、`TestViewDropout` の観測点は最後まで「長さ・順序・再現性」の
   ままだった。**変異カタログとテストが同じ盲点を共有すると、sweep は 100% を報告する。**

**次の MR で採る対策:** ソース由来の sweep に加えて、**公開メソッドの docstring の
主張 1 文につき最低 1 変異**を作る（契約由来変異）。`view_indices_for` の docstring は
「Batch 内で共通の view 数を**選び**」「**sample ごとの** view 番号列を返す」と書いて
おり、この 2 文をそのまま否定すればレビュアーの 2 変異になる。**書いた契約文を機械的に
反転するだけで、字面変異が届かない構造変異が出る。**

### M1: 飽和診断を monitor 検査より前に log する

`src/ml/training/loop.py` の `log_metrics` を `raise` の前へ出した。`raise` は残す。

- 例外文も `_formatted_metrics` で `名前=値` を出すようにした（key 名だけでは
    原因が読めない）
- **`learning_rate` の意味が変わる。** 従来は `scheduler.step` の**後**の値、つまり
    次 epoch の LR を当該 epoch の行に載せていた。移動後はその epoch で実際に使った
    値になる。**orchestrator が許容を裁定**（ラベルと中身の食い違いが直る方が正しく、
    production の学習 run がまだ無いので比較対象も無い）
- `epoch_seconds` も best checkpoint の保存時間を含まなくなった。**deadline 判定は
    `deadline_monotonic` と `_deadline_passed()` が絶対時刻で行う**ので停止判定には
    影響しない（orchestrator 確認済み）。`epoch_seconds` は log 専用
- **裁定の条件として、記録値そのものを固定するテストを足した。**
    `test_the_logged_learning_rate_is_the_one_the_epoch_used`。monitor が改善しない
    task と `scheduler_patience=0` で 3 epoch 回し、記録が `[0.02, 0.02, 0.01]` に
    なること（scheduler の低下より 1 epoch 遅れて動くこと）を厳密に見る。
    件数しか見ていない既存テストでは機構が守られていなかった
- 観測点は `TestTrainerFullRun::test_a_fully_saturated_run_logs_why_before_it_fails`。
    全 parameter を 0 にした model（ReLU'(0)=0 が勾配を遮断するので学習しても
    平均は 0 のまま）で Trainer を回し、**logger に
    `validation/saturated_positive_fraction == 1.0` が届くこと**と
    `validation/relative_error_score` が届かないことを同時に見る
- 変異実測: log を元の位置へ戻す変異、例外文を `sorted(metrics)` へ戻す変異とも killed

### M2: `ViewDropout` の中核 2 挙動

範囲 assert の `test_the_kept_count_stays_between_the_minimum_and_what_is_available` を
削除し、2 本へ置き換えた。

- `test_the_kept_count_varies_between_batches`: 16 epoch で観測された view 数が
    `{2, 3, 4, 5}` に**厳密一致**する。`minimum_view_count` 固定も `available` 固定も落ちる
- `test_each_sample_gets_its_own_subset_of_views`: 同一 batch の 3 sample が
    **長さ 1 種類・部分集合 3 種類**を得る。hoist 変異は部分集合 1 種類になって落ちる

変異実測: M2-A / M2-A2 / M2-B の 3 件とも killed。

### should-fix の対応

| # | 対応 |
| --- | --- |
| S1 | `docs/image-based-dispense-calibration.md`（Softplus → ReLU と理由）、`docs/image-based-dispense-calibration-ml-plan.md`（32/1024/stride 32 → 16/512/stride 8、scale 0.8-1.2 → 0.5-2.0、Softplus 2 箇所、許容 op 一覧、まとめの寸法）を実装へ同期。mdformat 通過 |
| S2 | `MeanSaturationDiagnostic.measure` が `predictions.validate()` を通し、破れていたら `ValueError`。**理由文字列にしなかった**のは、`DiagnosticReport.mean_saturation` と `reduce` の「常に返す」契約を optional へ崩さないため。長さ不一致は実データでは起きず呼び出し側の取り違えでしか起きないので、F4 の区分どおり例外側 |
| S3 | `_reject_invalid_inputs` に `valid_pixel_mask.shape[:2] != images.shape[:2]` を追加。`int()` を使わず SymInt のまま比較する（`blocks.py:314` と同じ理由）。`int()` を入れる変異も別に当てて killed を確認 |
| S4 | `validate_for_constraints` が先に `self.validate()` へ委譲。`stem_strides=(0,)` が合格していた穴を塞いだ |
| S5 | `ImageConstraints.validate_for_encoder` → `ImageEncoderConfig.validate_for_constraints` へ移設。テストも `tests/ml/model/test_blocks.py` へ移した |
| S6 | `validate_augmentation` に上限側の合成検証を追加。判定は「**最小の**源すら clip されるか」。それより大きい源が clip されるのはサイズ上限の正常動作なので見ない |

**S5 の前提について訂正。** レビュー記録は「既存の向き（model → data）のまま済む」と
書いているが、実測すると **`ml.model` → `ml.data` も本 MR 以前には存在しない**
（`grep -rn "^from ml\." src/ml/model/` は `ml.model.*` しか出ない）。つまりどちらの
向きも新設で、片方を消せばもう片方が立つ。

model → data を採ったのは 2 点。

1. 入力契約を知るべきなのは消費側（encoder）で、data 層が消費者を知る必要は無い
2. 上位層が `ml.data` を引く向きは既にある（`ml.training.data` → `ml.data.split`）

ただし **runtime import にはしていない。** `ml.model.blocks` が実行時に
`ml.data.image` を読むと `ml.model.*` 全体が torchvision を道連れにする。
`ImageConstraints` は型注釈にしか現れず参照するのは `minimum_size` だけなので、
`TYPE_CHECKING` の下へ置いた。層テストはどちらでも通る（両者 RUNTIME で torchvision は
許可）が、**`_loaded_dependencies` が測っているのは実行時の import 重量**なので、
そこを増やさない形を選んだ。

### nit の対応

- docformatter の連結痕 5 箇所（`test_image.py` / `test_heads.py` ×2 /
    `test_multiview.py` ×2）を解消。**説明段落は 1 文 1 行**にすれば再発しない
- `test_multiview.py` の `assert bool((mean >= 0).all())` は ReLU でも Softplus でも
    恒真なので削除
- `BatchShape.validate()` を追加し `plan_pixel_budget_batches` から呼ぶ。
    `view_count` だけでなく `height` / `width` も見るのは、3 つとも 0 以下だと
    padding 後の画素数が 0 になって pixel budget が効かなくなる**同一の**失敗だから

### 変異実測（レビュー対応分）

| 変異 | 結果 |
| --- | --- |
| M2-A（count を `minimum_view_count` 固定） | killed |
| M2-A2（count を `available_view_count` 固定） | killed |
| M2-B（`sample` を loop 外へ hoist） | killed |
| M1-order（log を monitor 検査の後ろへ戻す） | killed |
| M1-reason（例外文を key 名だけへ戻す） | killed |
| S2（`validate` 委譲の削除） | killed |
| S3（B/V 検査の削除） / S3-int（`int()` 混入） | killed |
| S4（`self.validate()` 委譲の削除） / S4-bound（境界 -1） | killed |
| S6（上限検査の無効化） / S6-bound（`>=`） / S6-floor（`ceil`） | killed |
| N6-call / N6-fields / N6-bound | killed |
| **計 16 件** | **16 killed / 0 survived** |

S4-bound と S6-floor は初回 survivor だった。どちらも**境界ケースが 1 つ足りない**
だけで、`minimum_size=31` vs `32`（`total_stride` 32）と、`floor` と `ceil` が割れる
512.5 px のケースを足して潰した。**第 1 ラウンドの「観測点が機構に対して鈍い」と
同じ形。**

既存カタログ 161 件も再走し、`validate_for_encoder` の移設で anchor が変わった 3 件を
追随させたうえで **161 件中 159 件が per-file で killed、残る 2 件（BAT-BS1 / RG-M2）は
全 suite で killed**（第 1 ラウンドと同じ）。

### 変異実験の手順（次の MR とドメイン層で使う）

#### 1. 復元元は index ではなく内容 snapshot

**`git restore --worktree` を変異実験の復元に使ってはいけない。** index は
「レビュー対象の固定」という別の目的で管理されており、作業中の変更を含まない。
修正を伴うラウンドでは worktree ≠ index になるので、`git restore` は**変異ではなく
自分の修正を巻き戻す**。

→ 対象 file の中身を scratchpad へ copy し、そこから書き戻す。
`git restore` / `git add` / `commit` / `stash` / `checkout` は driver から完全に除去する。
**最初から snapshot 方式でよい**（index に依存しないので、worktree の状態を問わない）。

前後の照合は 2 本立てにする。

- 対象 file の sha256 が snapshot と一致すること
- `git diff --name-only` の `src/` 集合が**想定どおりであること**（空であること、ではない。
    修正ラウンドでは空にならない）

#### 2. 契約由来変異をソース由来 sweep に足す

ソースの字面から作る変異は「演算子・引数・定数を変える / 分岐を潰す」に偏り、
**構造変異（loop の内外、変動する量の定数化）が原理的に出てこない。**

→ **書かれた主張 1 つにつき最低 1 変異**を作り、その主張を機械的に反転する。
`view_indices_for` の「Batch 内で共通の view 数を**選び**」「**sample ごとの**
view 番号列を返す」を反転すれば、レビュアーが見つけた 2 変異がそのまま出る。

**手順（レビュー 2 巡目の補強 4 点を反映した最終形）。**

1. **対象は「module + class + method」の docstring。** method だけに絞ると空振りする。
    `MultiViewImageEncoder.forward` の docstring は 1 文（`[B, V, C, H, W]` を
    `[B, output_features]` へ変換する）だけで、反転しても 1 巡目の実 survivor
    （`.mean` → `.amax`）は出ない。その主張は **module docstring** の「集約は平均とする」
    にある
2. **単位は文ではなく述語。** 1 文に主張が複数入る。「batch 内で共通の view 数を選び、
    sample ごとの view 番号列を返す」は「view 数を**選ぶ**（＝変わる）」「batch 内で
    **共通**」「sample **ごと**」の 3 主張で、それぞれ別の変異になる
3. **反転は一意でないので、他の主張をすべて真に保つ最小の反転を採る。** M2-A2
    （`count = available_view_count`）は 4 本落としたが、うち 2 本は狙った主張以外が
    壊れて落ちており、狙った主張を検査していない。狙いどおりの検出器を特定できない
4. **関数内変異では取れない類型を明示的に足す。** M1 は `reduce` が完全に正しく、
    **呼び出し側が結果を捨てていた**。契約由来変異でも取れない。カタログへ
    「呼び出し側での**文の順序入れ替え**」「**戻り値の破棄**」を足す。判定の目安は
    **「公開関数の戻り値が観測可能な出力へ到達することを、その関数のテストではなく
    呼び出し側のテストで固定する」**。`test_a_fully_saturated_run_logs_why_before_it_fails`
    （`reduce` ではなく Trainer 経路で見る）がその形
5. **生存したら「テスト不足」と「観測不能」を先に切り分ける。** 1 巡目の類型 C
    （観測点が機構に対して鈍い）はカタログ網羅性と直交して残る。N-1 は
    「観測点そのものが無い」（`batch.images` を誰も見ていない）側だった

6. **「検証が要求する条件が、防ぎたい失敗を本当に防ぐか」を別途確かめる。**
    契約由来変異は「**実装が契約どおりか**」しか見ない。**契約そのものが不十分な場合は
    原理的に検出できない。** `mean_bias_initial` の `validate()` は「正であること」を
    主張し、実装はそのとおりだったが、**「正である」は「初期化時に死なない」を
    保証していなかった**（`weight @ hidden` の広がりが小さい bias を飲み込む）。
    call-site 変異でも出ない。防ぎたい失敗を**数値で測って**、検証条件がその失敗率を
    0 にしているかを確かめること。4 巡のレビューでも出なかった型

    運用の細目 3 点。

    - **測る対象を絞る判定条件:** 「guard の docstring が述べる**目的**が、
        **述語の言い換えになっていない**なら測る」。`mean_bias_initial > 0` ＋ 目的
        「死んだ領域を防ぐ」は述語 ≠ 目的なので**測る**。`stride > 0` ＋ 目的
        「`ZeroDivisionError` 回避」は述語 ⇒ 目的なので**不要**
    - **ギャップが出たら、まず構造で保証する。** 閾値を `validate()` へ足すのは最後の
        手段。今回の `nn.init.zeros_(weight)` が最良の解き方で、測定由来のマジック
        ナンバーを検証条件へ入れずに済んだ。**失敗率は regime 依存
        （同じ bias で 1%／37%／79%）なので、閾値にすると必ず脆くなる**
    - **guard 単体でなく「guard + 文書化された推奨値」を対で見る。** 今回いちばん
        危なかったのは、**docs が guard の通す危険域（0.05）を名指しで推奨していた**
        こと。guard が通す値域のうち、文書が積極的に勧めている点を測る

7. **レビューで出した問いが数値の問いなら、文書の更新は回答にならない。測るか、測らせる。**
    1 巡目の N5 でこの点に触れながら「docs に明記した」という回答で閉じてしまったのが、
    今回の穴が 5 巡目まで残った直接の原因（レビュアーの自己分析）。
    **「〜すると危ない」と書いて閉じるのは、危なさの大きさを測っていない。**

判定の目安。

- **仕様（T 表）に「どう壊すと落ちるか」の列が無い機構が最も危険。** 実装コードだけを
    読んで変異を作ることになり、字面の偏りがそのまま出る
- **変異カタログとテストが同じ盲点を共有すると、sweep は 100% を報告する。**
    「全 killed」は「カタログが網羅的」を意味しない。**変異スコアはカタログに対する
    相対値でしかない**
- MR6 の survivor 4 類型は「**当てた**変異が生き残る理由」の分類で、
    「**そもそも当てていない**」という失敗様式を含んでいなかった

#### 3. 「対になる実装」はテストの観測点集合を突き合わせる

最初は「新設側に当てた変異を既存側にも当てる」と書いたが、**向きが片側だった**。
その規則では N3-1（MUT-P4 / P5）は出ない。**既存側にあって新設側に無い観測点**という
逆向きの欠落だったからである。

→ **対の実装どうしで「何を観測しているか」の表を作って突き合わせる。**
今回なら次の表を作った時点で穴が見える。

| 観測点 | 単視点 `PaddedBatch` | 多視点 `MultiViewPaddedBatch` |
| --- | --- | --- |
| mask の画素数 | あり | あり |
| 画像の配置位置と mask の一致 | **無かった**（MUT-P3） | **無かった**（MUT-P1） |
| 評価時の中央配置 | あり | **無かった**（MUT-P4） |
| 学習時が中央でないこと | あり | **無かった**（MUT-P5） |

実測でこの 4 件すべてが survivor だった。**「片方にあるからもう片方も大丈夫」は
成り立たない。** 表にして両方向の欠落を見ること。

#### 4. 共有ヘルパーへの変異は call-site へ inline してから当てる

**この一連の MR で見つけた変異実験の限界のうち、最も根が深い。**

`_placement` は `PaddedBatch.pad` と `MultiViewPaddedBatch.pad` の共有関数で、
**そこへ変異を当てると単視点側のテストが必ず殺す。多視点側に観測点が 1 本も無くても
sweep は killed を報告する。** 実際 1 巡目の BAT-PL1 / BAT-PL2 は killed だったのに、
call-site へ inline した MUT-P4 / P5 は survivor だった。

原因はカタログの中身ではなく**変異を当てる場所**。共有ヘルパーは変異の効果を
「呼び出し側のどれか 1 つ」で吸収してしまう。

→ **2 箇所以上から呼ばれる私有ヘルパーは、各呼び出し側へ inline してから当てる
（call-site ごとに 1 変異）。** 対象を絞れば数個で済む。

本 MR で実施した対象と結果（17 変異）。

| 共有ヘルパー | call-site | 結果 |
| --- | --- | --- |
| `_placement` | `PaddedBatch.pad` / `MultiViewPaddedBatch.pad` | 多視点側 2 件が **survivor** |
| `_ceil_to` | 単視点 pad / 多視点 pad / `_fill_batches` の `alone` | `alone` が **survivor** |
| `_validate_batch_common` | 単視点 / 多視点の `_validate_*_inputs` | 両方 killed |
| `_target_shape` | 単視点 / 多視点の `preprocess` | 両方 killed |
| `_valid_pixel_mask` | 同上 | 両方 killed |
| `_transformed_images` | 同上 | **両方とも survivor** |
| `_fraction` | `MeanSaturationDiagnostic` の 2 項 | 両方 killed |

**`_transformed_images` の 2 件は本 MR で最も分かりにくい穴だった。** 画像側の回転を
落としても、有効画素 mask は `_valid_pixel_mask` が別経路で作るので mask を見る
テストは全て通る。N-1（mask は見るが image を見ない）と**同じ形が前処理側にもあった**。
90 度回転は補間誤差が出ないので、`rot90` との厳密一致で固定した。

### 事故と再発防止

**変異 driver の `git restore --worktree` が、index に入っていない今回の src 修正
6 file をまるごと巻き戻した。** 第 1 ラウンドは worktree と index が一致していたので
問題にならなかったが、今回は「index = レビュー前、worktree = 修正後」なので
`git restore` の復元元が誤りだった。tests / docs は driver の対象外だったので無傷。

→ **driver の復元を scratchpad の内容 snapshot からの copy に変えた**（上記手順 1）。
`git restore` は driver から完全に除去した。`git add` / `commit` / `stash` /
`checkout` は今回も一度も使っていない。

**orchestrator の裁定:** `git restore --worktree` で復元する手順を指示したのは
orchestrator 側で、「index と worktree は一致している」という前提が修正を入れた時点で
崩れる、というのが指示の不備だった。以後の変異実験は最初から snapshot 方式とする。

### ドメイン MR への申し送り

**行動が要る 3 点は `docs/image-based-dispense-calibration-ml-plan.md` の
「ドメイン実装者への申し送り」へ書いた**（orchestrator 裁定。`memory/` は判断の経緯を
残す場所で、ドメイン実装者が読むのは docs 側）。以下は経緯の記録。

1. **`GaussianHeadConfig.mean_bias_initial` の既定 1.0 は中立値。** この用途の真値
   スケールは 0.05〜0.2 µL で 5〜20 倍あるので、**ドメイン側が必ず上書きする**こと。
   `ml` はドメインを知らないので既定は中立のままにする（裁定済み）
2. **encoder の形を決めるドメイン MR で、trunk ReLU の全滅を 1 度実測すること。**
   `mean_bias_initial` が守るのは平均 head の bias だけで、trunk（`hidden_features` の
   Linear + ReLU）は torch 既定初期化のまま。合成 encoder（`output_features=16`、
   `hidden_features=8`）では 40 seed 中 1 seed で batch 全体が死に、head 出力が bias
   固定になって encoder の feature 差を映さなかった。レビュアーの production encoder
   （stride8_A、out 96、hidden 128）100 seed 実測では 0/100 なので `ml` 側の対処は
   不要と裁定されたが、**encoder の形はドメイン設定なので `ml` にこの安全性を固定する
   テストは置けない。** 形を確定させる MR で実測すること
3. `_reject_mixed_placement` の理由文が device 違いと dtype 違いで同一。計画 R6
   （理由コードと文面の分離）の対象に加える

---

## レビュー 2 巡目の対応

### N-1（must-fix）: 画像の配置を観測するテストが無かった

**レビュアーが契約由来変異のルールを前向き検証として適用し、新しい survivor を見つけた。**
`MultiViewPaddedBatch.pad` の docstring「配置位置は sample 内の全 view で共有する」を
反転した MUT-P1（mask は正しい位置、image だけ常に左上へ置く）が全 suite で生存した。

`TestMultiViewPaddedBatchPad` の全テストが `valid_pixel_masks` しか見ておらず、
`batch.images` は shape しか見ていなかった。

**補強:** `test_places_the_image_where_the_mask_says_it_is`（評価 / 学習の 2 ケース）。
既知の定数で埋めた画像を置き、`images.ne(0).all(dim=2)` が `valid_pixel_masks` と
**厳密一致**することを見る。冒頭に「padding が在ること」の確認を置き、
全 true の退化した配置で主張が観測できなくなるのを防いだ。

**実測で 1 件広がった。同型の変異が単視点 `PaddedBatch.pad` でも生存した（MUT-P3）。**
レビュー記録は「単視点側には `test_valid_mask_marks_exactly_the_placed_pixels` がある」と
していたが、そのテストは mask の**画素数**を数えるだけで画像の位置を見ていない。
`test_centres_each_image_during_evaluation` も mask の 1 点しか見ていない。
単視点側にも同じテストを足した。

| 変異 | 補強前 | 補強後 |
| --- | --- | --- |
| MUT-P1（多視点。image だけ左上） | survived | **killed** |
| MUT-P2（多視点。view ごとに配置をずらす） | killed | killed |
| MUT-P3（単視点。image だけ左上） | **survived（新発見）** | **killed** |

### N-2 / N-3（should-fix）: docs

- **N-2.** `v1 encoder` の表を `stride8_A`（総 stride 8 / 出力 96 次元 / 395,048 params）へ
    更新した。総 stride 32 のままだと**同じ doc の新しいサイズ契約（最小 16 px）と矛盾し、
    本 MR が新設した `validate_for_constraints` が弾く**。「総 stride は `minimum_size`
    以下でなければならない」を理由つきで明記し、旧構成を採らない理由（53 px が 2×2、
    27 px が 1×1 へ潰れる）も残した。後続の `Linear(161, 128)` も `Linear(97, 128)` へ修正
- **N-3.** 両 doc へ多視点を反映した（orchestrator 裁定: 要件 doc の記述が古い）
    - 要件 doc `## 複数視点`: 「初期達成条件には含めない」を削り、平均集約・view dropout・
        推論 1〜5 view・「達成条件は 1 view でも成立させる」へ書き換え
    - 要件 doc の収集節 2 箇所から「初期実装では central view だけ」「view 0 だけを撮影」を
        削除し、view 数が session 単位の設定であることへ
    - 要件 doc `## モデル入出力` の図を共有 encoder + view 平均へ
    - ml-plan `### 入出力` を `[B, V, 6, H, W]` へ。`pixel_per_mm` が view 軸を持たない理由、
        batch 内 V 均一の理由、`ViewDropout` の決定論、推論 1〜5 view を追記
    - ml-plan `### batch生成` の bucket 鍵へ view 数、cost へ `sum(view_count)`、
        stride 32 → 8、collate の戻り値を 5 階へ、配置位置の view 共有を追記

### N-5（nit）

`test_each_sample_gets_its_own_subset_of_views` に `len(indices[0]) < 5` を足した。
全 view を残す回では部分集合が一致してしまい主張を観測できないので、その配置でないことを
テスト自身が先に確かめる形にした（N-1 の補強と同じ作法）。

### N-4（nit）: 失敗 epoch の log 重複 — 現状維持と判断した

monitor を欠いて raise する epoch は `with_completed_epoch()` にも
`_save(state, "latest")` にも到達しないので、同じ checkpoint から再開すると
**同じ `global_step` へ 2 回 log される**。M1 の移動前は起こらなかった。

**現状維持とする。** 決定的なのは理由 3。

1. **この重複が起きる run は続行できない run である。** monitor を欠いた epoch は
    例外で止まり、原因（平均飽和など）を直さない限り再開しても同じ epoch で同じ
    場所で止まる。**ただし 2 行が同一内容になるわけではない。**
    `epoch_seconds` と `train_samples_per_second` は wall clock なので必ず異なり、
    model / optimizer state が同じでも数値が一致するのは metric 側だけである
    （レビュー 3 巡目の指摘で訂正。当初「2 行は同じ内容」と書いたのは誤り）
2. **MLflow は同一 step の重複 metric を許す。** 記録側の破損にはならない
3. **塞ぐには機構が要る（これが決定的）。** 「失敗 epoch を log 済みとして checkpoint
    へ記録する」か「再開時に log 済み step を読み戻して skip する」かで、どちらも
    Trainer に新しい状態を足す。**M1 が解こうとした問題（原因が運用者に届かない）より、
    この重複の害の方がはるかに小さい。** AGENTS.md 原則 2（要求されていない機能を
    足さない）と、到達不能な防御を足さない基準に照らして足さない

**記録だけ残す。** 実運用で「同じ step が 2 行ある」ことが解釈の邪魔になったら、
`epoch_seconds` 系の時間 metric が重複行で食い違うことを前提に読むこと。

---

## レビュー 3 巡目の対応

### N3-1（must-fix）: 多視点側に配置位置の観測点が無かった

`MultiViewPaddedBatch.pad` の配置位置を観測するテストが 0 本で、
**MUT-P4（常に左上）と MUT-P5（`training` を無視して常に中央）が全 suite で生存**した。

2 巡目で足した `test_places_the_image_where_the_mask_says_it_is` は image と mask の
**相対**一致しか見ないので、両方を同じだけずらす変異は通る。単視点側にある 2 本
（`test_centres_each_image_during_evaluation` /
`test_training_placement_is_not_the_evaluation_centre`）の多視点版を足した。

- 評価時: 56 px canvas の 32 px 画像で `[12, 12]` が有効・`[11, 11]` が無効
- 学習時: 同じ seed の評価時 mask と**一致しないこと**

**実測: MUT-P4 / MUT-P5 とも killed。**

### 共有ヘルパーへの call-site 変異（補強 B の実施結果）

17 変異を当て、**3 件が survivor**だった（手順 4 の表を参照）。すべて補強し、
再走で 17/17 killed。

| survivor | 内容 | 補強 |
| --- | --- | --- |
| MUT-P4 / P5 | `_placement` の多視点 call-site | 上記 2 本 |
| CS-CT3 | `_fill_batches` の `alone` から `_ceil_to` を落とす | `max_batch_pixels=53²` で 53x53 の 1 sample が弾かれることを固定（padding 後 56² で判定する） |
| CS-TI1 / CS-TI2 | `_transformed_images` の回転を単視点 / 多視点の各 call-site で落とす | 90 度回転の出力が回転なし出力の `rot90` と**厳密一致**することを両方で固定 |

**CS-TI1 / CS-TI2 が本 MR で最も分かりにくい穴だった。** 画像側の回転を落としても
有効画素 mask は別経路（`_valid_pixel_mask`）で作られるので、mask を見るテストは全て通る。
N-1 と同型（mask は見るが image を見ない）が前処理側にもあった。

### docs（N3-2〜N3-6）

- **N3-2.** 要件 doc `## 目標` の「複数視点の画像を**将来**利用できるデータ形式にする」を
    「1 パッドを複数視点で撮影し、複数視点のまま体積を推定する」「1 view しか撮れない
    状況でも同じモデルで推定できる」の 2 項へ差し替え。読み手が最初に読む節なので
    ここが古いままだと以降が全て疑わしくなる
- **N3-3.** `### 推定精度` へ view 数の条件を追加。**既定 5 view で精度条件を満たすことを
    必須、同じモデルが 1 view でも動作することを併せて確認**。1 view の精度は下回ってよいが
    推論が成立しない状態は許容しない。複数視点節への相互参照も張った
- **N3-4.** `## データ収集` へ view 配置の定義を追加。**「中心 1 点 + 中心から 360/n 度ずつ
    回した n 方向、既定 n=4、距離 1 mm」→ 既定 view 数 5**（`!207` の収集方式）。
    「5 view」「1〜5 view」が doc 内で定義済みの語になった
- **N3-5.** ml-plan の「全 channel 数 24、32、48、96、160」から 160 を削除（削除した
    3 段目の値）
- **N3-6.** metadata schema 例の `views` を 1 件から 5 件（中心 + 4 方向、offset ±1 mm）へ

### N-4 の理由づけを訂正した

当初「重複した 2 行は同じ内容」と書いたのは誤りで、**`epoch_seconds` と
`train_samples_per_second` は wall clock なので必ず異なる**。結論（現状維持）は
変えないが、決定的な理由は 3（塞ぐと Trainer に新しい状態が要る）であることを明示し、
「前提が崩れていないか確かめる」という誤解を招く条件文を削除した。

---

## merge 前ラウンド: `mean_bias_initial` の対処が不十分だった

### 問題

`validate()` が「正であること」しか要求しないので、bias が `weight @ hidden` の広がりに
対して小さいと**初期化時の全滅が再発する**。しかも docs の申し送りが勧めているのは
「真値スケール 0.05〜0.2 µL へ上書きせよ」で、**その指示に従うと死ぬ**。

実測（`input_features=128` / `hidden_features=128`、300 seed）。

| 特徴量 | bias=1.0 | bias=0.2 | bias=0.05 |
| --- | ---: | ---: | ---: |
| 全 0（trunk 出力が sample 間で同一） | 0% | 0% | 1% |
| 1 本を複製 + 微小ノイズ | 0% | **21%** | **45%** |
| randn（encoder 出力相当） | 0% | **55%** | **80%** |

orchestrator の実測（22% / 41%）は 2 行目の条件に対応する。

**既存テストは全 0 の特徴量で回していたので、この条件では死亡率 1% しか出ない。
検出器として弱かった。**

### 対処: 平均出力層の weight を 0 初期化する（orchestrator 裁定）

`nn.init.zeros_(mean_layer.weight)` を bias の初期化の隣へ置いた。初期の前活性が bias
そのものになるので、**正でありさえすれば必ず活性領域に入る**。修正後は上表の全条件・
全 bias で **0/300**。

勾配は流れる。初期の前活性が `bias > 0` なので ReLU の微分は 1 になり、weight へ
`grad_z * hidden` が伝わる（実測: `weight.grad` 非零 128/128、3 step 後の weight
非零 128/128）。`log_variance` 側は ReLU を通らないので触っていない。

### 代償: 学習前の平均は入力に依らず定数になる

**この変更の影響範囲は小さくない。既存テスト 6 本が落ちた。** すべて「初期化直後の
model で平均が入力・条件変数に応じて動く」ことを観測していたもので、その性質は
**実際に成り立たなくなった**（テストの都合ではなく仕様の変化）。

| テスト | 対応 |
| --- | --- |
| `test_mean_is_exactly_zero_when_the_pre_activation_is_negative` | 少し学習させた head で観測（`_head_trained_towards_zero`） |
| `test_a_saturated_sample_has_exactly_zero_gradient_to_its_features` | **真値 0 の blank を 1 件混ぜて回帰させた head**で観測（`_head_trained_to_partially_saturate`）。ReLU 化が狙った状態そのものなので、以前の「たまたま飽和する seed を探す」より faithful になった |
| `test_conditioning_changes_the_prediction` | log 分散側で観測 |
| `test_matches_the_encoder_and_head_applied_in_order` | 自己検証 guard を log 分散側へ |
| `test_the_conditioning_stays_one_value_per_sample` | 同上 |
| `test_parity.py::test_reports_a_mismatch_when_the_weights_differ` | 出力を特定せず「どれかが食い違う」を見る |

docs の申し送りにも「学習前の mean head は bias 一定を返すので、初期化直後の model で
出力が入力に応じて動くことを確かめる検査は log 分散側か学習後に行う」を書いた。

### テストと変異実測

`test_the_mean_head_is_never_born_dead` を **bias（1.0 / 0.05）× 特徴量（全 0 / randn）**
で parametrize し、32 seed で回す。`test_the_mean_head_still_learns_from_a_zero_weight`
で weight が 0 に固定されないことも固定した。

| 変異 | 結果 |
| --- | --- |
| HD-W1（`nn.init.zeros_(weight)` を削除＝torch 既定へ戻す） | **killed**（`[True-0.05]` seed 13 / `[False-0.05]` seed 0） |
| HD-W2（bias を 0 にして weight を残す＝対処が逆） | killed（6 本） |
| HD-W3（log 分散側まで 0 にする退行） | killed |

**bias=1.0 の 2 ケースは HD-W1 で落ちない。** 既定値だけで回していた以前のテストが
この穴を守れなかったことの裏付けになっている。

### 変異手順への追記（手順 6）

この穴は**契約由来変異でも call-site 変異でも出ない**類型だった。`validate()` の
docstring は「正であること」を主張し、実装はそのとおり。**主張自体が弱かった。**

→ 手順 6 として「**検証が要求する条件が、防ぎたい失敗を本当に防ぐかを別途確かめる**」を
足した。契約由来変異は「実装が契約どおりか」しか見ないので、契約が不十分な場合は
原理的に検出できない。防ぎたい失敗を**数値で測って**、検証条件がその失敗率を 0 に
しているかを確かめる。

---

## レビュー 5 巡目（approve 後の仕上げ）

### S5-1: 平均側の conditioning を観測するテストが 0 になっていた

weight 0 初期化への対応で `test_conditioning_changes_the_prediction` を log 分散側へ
移した副作用。**平均側の観測点が 1 つも無くなった。**

ml-plan は `log(pixel_per_mm)` を連結する狙いを「物理 scale を**平均**へ反映させる」と
書いているので、効かせたい先は平均。log 分散側だけでは契約を固定できない。

補強: log 分散側の観測を残したうえで、20 step 学習させて weight を 0 から動かしてから
**平均も conditioning に応じて動くこと**を見る。

| 変異 | 補強前 | 補強後 |
| --- | --- | --- |
| MUT-C1（`_joined_inputs` が conditioning を 0 で潰す） | killed | killed |
| MUT-C2（平均だけ conditioning 抜きの trunk 出力から作る） | **survived** | **killed** |

### S5-2 / S5-3: 死亡率の数値を実測へそろえた

**変異を実際に当てた状態**（`nn.init.zeros_(weight)` を削除）で 300 seed 測り直した。
`mean_bias_initial=0.05`。

| head 規模 (in/hidden) | 全 0 feature | ほぼ同一 feature | 標準正規 feature |
| --- | ---: | ---: | ---: |
| 32 / 16（テストの config） | 12% | 37% | **79%** |
| 128 / 128 | 1% | 42% | **80%** |

- docs の「標準正規の feature では 5%」は**誤り**（実測 80%）。**危険側の誤り**で、
    最悪の regime を最も安全に見せていた。表へ置き換えた
- docs の「全 0 では 1%」は **128/128 の値としては正しい**。config 依存であることが
    書かれていなかったのが原因なので、規模を明示した表にした
- テストの docstring の「9 割近くが死ぬ」は**否定済みの旧測定値**。実測へ直し、
    どの regime が検出を担っているかと、seed 数を減らすと検出器が空になることを書いた

**レビュー記録の「全 0 feature 側は HD-W1 で落ちない（3% なので seed 0〜31 に該当なし）」は
本 MR の code では成立しない。** 実測すると全 0 側も seed 13 で落ちる（12%、seed 0〜31 に
2 件）。おそらく weight を構築後に引き直して測ったため RNG の位置がずれた
（`nn.Linear.__init__` が消費する乱数列と、後から `kaiming_uniform_` を掛けた場合とで
seed ↔ 結果の対応が変わる。集計値はほぼ同じでも per-seed の一覧は一致しない）。
**変異の効果を測るときは、変異を実際に当てた code path で測ること。**

ただし **128/128 では全 0 側の死亡が seed 0〜31 に 0 件**なので、レビュアーの警告
「片方の regime だけに頼ると検出器が空になりうる」自体は正しい。両 regime を回す
現在の形を維持する。

### 別 MR へ送る（本ラウンドでは対応しない）

- **N4-1**: `torch.allclose` を使っている箇所で `rtol` を明示していない。既定 1e-5 が
    効いており、意図した許容が `atol` だけに見える
- **N4-2**: 既存テストの名前と中身が一致していない箇所がある（本 MR の変更に由来しない）
