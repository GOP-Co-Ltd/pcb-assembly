# コア ML 基盤 MR3: モデル部品と評価

計画全体は `/home/gop/.claude/plans/mr185-codex-docs-image-based-dispense-ca-modular-sonnet.md`。
MR1 = `feature/2026-09-04/ml-core-1-package-artifact`、MR2 = `feature/2026-09-04/ml-core-2-data-pipeline`。
本 MR のブランチは `feature/2026-09-04/ml-core-3-model-evaluation`。

## 概要

「画像 + 有効画素 mask + スカラー条件 → 平均と log 分散」という回帰タスクの一般形を、
`nn.Module` サブクラス・純関数・`attrs.frozen` 値オブジェクトだけで組む。あわせて
Gaussian 回帰の loss / metric / slice 診断 / eager vs `torch.compile` 比較を実装する。
paste_volume 固有の単位（µL）・model family 名・次元名（machine / nozzle 等）は一切持ち込まない。

## 設計判断（先に決めたこと）

| 論点 | 決定 | 理由 |
| --- | --- | --- |
| encoder は部品か組み立て済みか | **config 駆動の `ImageEncoder` を提供**。channel 数・stem 段数・stage 数・stride はすべて `ImageEncoderConfig` のタプルで受ける | ドメインは config 値だけを決めればよい。MR3 単体で過学習テスト・parity テストが書ける |
| ファイル分割 | 全体計画どおり `blocks.py` / `heads.py` / `loss.py` / `inspection.py` の 4 本。encoder は `blocks.py`（block の組み立て）、合成 model は `heads.py` に置く | 承認済み計画のレイアウトを変えない |
| 学習可能 padding pixel | **encoder 側**（`ImageEncoder` が `nn.Parameter [1, C, 1, 1]` を持つ）。置換自体は `blocks.replace_invalid_pixels()` の純関数 | channel 数が encoder 入力と一致し、mask は `ml.data.batch.PaddedBatch` から画像と一緒に来る。head は画素を見ない |
| 平均の活性化 | **常に Softplus**（正の量の回帰に限定）。config 化しない | metric 側も `target > 0` / `mean > 0` を前提に相対誤差を出す。負値回帰の要求が出てから activation を config 化する |
| スカラー条件変数 | head が `conditioning: Tensor \| None`（`[B, K]`）を受け、pooled feature に連結する。`log(pixel_per_mm)` への変換はドメインの責務 | 物理量の意味を ml に持ち込まない |
| 誤差の呼称 | doc の「正規化誤差 e」を `relative_error_*`（`(mean - target) / target`）に統一 | MR185 は `normalized_error`（= 相対誤差）と `normalized`（= z-score）を別ファイルで同名運用しており取り違えの元。primary gate `abs(mean(e)) + std(e) <= 0.10` は相対誤差の側 |
| slice の metric 型 | `GaussianRegressionMetrics` を slice でも使い回す。専用 `SliceMetrics` を作らない | MR185 は 14 フィールドがほぼ同じ型を 2 つ持ち、片方だけ `float \| None` だった |
| graph break 計測 | **入れない**。`CompileOptions.fullgraph=True` で break をエラーにする | `torch._dynamo.utils.counters` は 3rd-party private API |
| 診断レポートの永続化 | MR3 では入れない（`save_document` / `load_document` は MR6 の evaluation.json で足す） | 利用者が現れてから足す（AGENTS.md 開発原則 2） |

## 公開インターフェース案

### `src/ml/model/blocks.py`（torch を module-level import してよい層）

```python
def build_group_norm_convolution(in_channels: int, out_channels: int, *, stride: int,
                                 groups: int, kernel_size: int = 3) -> nn.Sequential
    # Conv2d(bias=False) -> GroupNorm(eps=1e-5, affine=True) -> ReLU

class GroupNormResidualBlock(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, *, stride: int, groups: int) -> None
    @override
    def forward(self, inputs: Tensor) -> Tensor

def build_group_norm_residual_stage(in_channels: int, out_channels: int, *, block_count: int,
                                    first_stride: int, groups: int) -> nn.Sequential

def replace_invalid_pixels(images: Tensor, valid_pixel_mask: Tensor, fill: Tensor) -> Tensor
    # torch.where(mask, images, fill)。fill は [1, C, 1, 1]

@attrs.frozen
class ImageEncoderConfig:
    input_channels: int
    stem_channels: tuple[int, ...]
    stem_strides: tuple[int, ...]
    stage_channels: tuple[int, ...]
    stage_strides: tuple[int, ...]          # 各 stage の先頭 block の stride
    blocks_per_stage: tuple[int, ...]
    group_norm_groups: int = 8
    def validate(self) -> str | None
    @property
    def output_features(self) -> int        # stage_channels[-1]
    @property
    def total_stride(self) -> int           # stem と stage の stride の積

class ImageEncoder(nn.Module):
    def __init__(self, config: ImageEncoderConfig) -> None   # config 不正は ValueError
    @property
    def output_features(self) -> int
    @override
    def forward(self, images: Tensor, valid_pixel_mask: Tensor | None = None) -> Tensor
        # [B, C, H, W] (+ bool [B, 1, H, W]) -> [B, output_features]
        # padding pixel 置換 -> stem -> stage -> AdaptiveAvgPool2d((1,1)) -> flatten
```

デフォルト値は `group_norm_groups` だけに置く。v1 の (24, 32, 48) / (48, 96, 160) は
ドメイン側の config が持つ。

### `src/ml/model/heads.py`

```python
@attrs.frozen
class GaussianHeadConfig:
    input_features: int
    conditioning_features: int = 0
    hidden_features: int = 128
    log_variance_minimum: float = -14.0
    log_variance_maximum: float = 5.0
    def validate(self) -> str | None

class GaussianRegressionHead(nn.Module):
    def __init__(self, config: GaussianHeadConfig) -> None
    @override
    def forward(self, features: Tensor, conditioning: Tensor | None = None
                ) -> tuple[Tensor, Tensor]
        # features [B, F] (+ conditioning [B, K]) -> (mean [B, 1], log_variance [B, 1])
        # Linear(F + K, hidden) -> ReLU -> 独立した 2 本の Linear(hidden, 1)
        # mean = softplus(raw)、log_variance = clamp(raw, minimum, maximum)

class GaussianImageRegressor(nn.Module):
    def __init__(self, encoder: ImageEncoder, head: GaussianRegressionHead) -> None
        # head.input_features != encoder.output_features は ValueError
    @override
    def forward(self, images: Tensor, valid_pixel_mask: Tensor | None = None,
                conditioning: Tensor | None = None) -> tuple[Tensor, Tensor]
```

出力契約:

- `mean`: Softplus により常に正。上限は設けない。shape は target と同じ `[B, 1]`
- `log_variance`: `[log_variance_minimum, log_variance_maximum]` へ clamp。範囲外で勾配が
    切れるのは意図どおり（数値安定性のみが目的）
- 戻り値は attrs 値オブジェクトではなく素の `tuple[Tensor, Tensor]`。MR6 の ONNX export と
    `torch.compile` の扱いを単純に保つ
- `conditioning` は `conditioning_features == 0` のときだけ `None` を許す。両者の不一致、
    ndim / channel / dtype 不一致は `ValueError`（呼び出し側の不変条件違反）
- shape・dtype の検査は `forward` 内で行う。`.item()` を伴う値検査は `forward` に入れない
    （compile 境界の外で `ml.model.loss.validate_gaussian_inputs()` を使う）

### `src/ml/model/loss.py`

```python
def weighted_gaussian_negative_log_likelihood(mean: Tensor, log_variance: Tensor,
                                              target: Tensor, sample_weight: Tensor) -> Tensor
    # 0-dim、微分可能。sum(w * 0.5 * (exp(-l) * (y - mu)**2 + l)) / sum(w)
    # shape 不一致は ValueError

def validate_gaussian_inputs(mean: Tensor, log_variance: Tensor, target: Tensor,
                             sample_weight: Tensor) -> str | None
    # 全 tensor が有限 / weight が非負 / weight 合計が正。compile 境界の外で呼ぶ
```

### `src/ml/model/inspection.py`

```python
def count_parameters(model: nn.Module, *, trainable_only: bool = False) -> int

@attrs.frozen
class ModelSize:
    parameter_count: int
    trainable_parameter_count: int
    multiply_accumulate_count: int
    @property
    def giga_multiply_accumulate(self) -> float

def measure_model_size(model: nn.Module, example_inputs: Sequence[Tensor]) -> ModelSize
    # forward hook で Conv2d と Linear の multiply-accumulate を実測する。
    # batch 次元が 1 でない入力は ValueError（1 sample 当たりの数を返す契約のため）。
    # eval() + no_grad で 1 回 forward し、元の training mode へ戻す。
    # GroupNorm / ReLU / pooling の要素演算は数えない（GMAC の慣用に合わせる）
```

config から解析的に数える MR185 方式は採らない。config と実装が drift しても検出できないため、
実 module を走らせて数える。

### `src/ml/evaluation/regression.py`

```python
@attrs.frozen
class GaussianPredictions:
    mean: Tensor          # converter で detach -> reshape(-1) -> double -> cpu に正規化
    log_variance: Tensor
    target: Tensor
    sample_weight: Tensor
    def validate(self) -> str | None      # 長さ一致・空でない
    def with_log_variance_offset(self, offset: float) -> GaussianPredictions

@attrs.frozen
class GaussianRegressionMetrics:
    sample_count: int
    valid_sample_count: int
    invalid_sample_count: int
    weight_sum: float
    negative_log_likelihood: float
    mean_absolute_error: float
    root_mean_squared_error: float
    relative_error_mean: float
    relative_error_standard_deviation: float
    relative_error_score: float                 # abs(mean) + std。primary gate の値
    median_absolute_relative_error: float
    p95_absolute_relative_error: float
    one_standard_deviation_coverage: float
    mean_predicted_standard_deviation: float

def gaussian_regression_metrics(predictions: GaussianPredictions
                                ) -> tuple[GaussianRegressionMetrics | None, str | None]

def fit_gaussian_log_variance_offset(predictions: GaussianPredictions
                                     ) -> tuple[float | None, str | None]
    # NLL 最小の閉形式: log( sum(w * exp(-l) * (y - mu)**2) / sum(w) )
```

weight の扱い:

- NLL・MAE・RMSE・相対誤差の mean / std・coverage・予測 std 平均は
    すべて `sum(w * x) / sum(w)`。count 系は重みなし
- percentile（median / p95）も重み付きで計算する。sorted 昇順の累積重み `C_i` に対し
    位置を `p_i = (C_i - w_i) / (W - w_i)` と定義して線形補間する。重みが一様なら
    `torch.quantile(..., interpolation="linear")` と一致する（テストで固定する）
- 無効 sample（mean / log_variance / target / weight が非有限、`mean <= 0`、`target <= 0`、
    `weight < 0`）は集計から**除外して数える**。全体が無効、または有効分の weight 合計が 0 の
    ときだけ理由文字列を返す。MR185 のように 1 件でも無効なら例外、とはしない

### `src/ml/evaluation/slices.py`

```python
@attrs.frozen
class CategoricalDimension:
    name: str
    values: tuple[str, ...]              # prediction と同じ並び
    def validate(self, sample_count: int) -> str | None

@attrs.frozen
class NumericDimension:
    name: str
    values: tuple[float, ...]            # prediction と同じ並び
    bucket_count: int = 4                # percentile 分割数
    def validate(self, sample_count: int) -> str | None

type SliceDimension = CategoricalDimension | NumericDimension

@attrs.frozen
class DiagnosticSlice:
    dimension: str
    value: str                           # "machine-a" / "[0.12,0.34)"
    sample_count: int
    metrics: GaussianRegressionMetrics | None
    reason: str | None                   # metrics が None のときだけ非 None

@attrs.frozen
class ReliabilityBin:
    lower_standard_deviation: float
    upper_standard_deviation: float
    sample_count: int
    mean_predicted_standard_deviation: float
    observed_root_mean_squared_error: float
    one_standard_deviation_coverage: float

@attrs.frozen
class DiagnosticReport:
    overall: GaussianRegressionMetrics
    slices: tuple[DiagnosticSlice, ...]
    reliability_bins: tuple[ReliabilityBin, ...]

def build_diagnostic_report(predictions: GaussianPredictions, *,
                            dimensions: Sequence[SliceDimension],
                            reliability_bin_count: int = 5
                            ) -> tuple[DiagnosticReport | None, str | None]
```

次元定義の注入方法:

- 次元は**名前と、prediction と同じ並びの値列**として渡す。sample ID による突き合わせをしない
    ので、ml 側が sample の同一性を知る必要がない。長さ不一致は `ValueError`
- 数値次元は 0 / 25 / 50 / 75 / 100 percentile で bucket 化し、境界が縮退したら bucket を減らす。
    ラベルは `[lower,upper)`、最終 bucket だけ `[lower,upper]`
- カテゴリ次元は値の昇順。slice は (次元の指定順, 値) で安定ソートする
- reliability bin は予測 std の昇順に、同じ std を分断せずおよそ等件数へ分ける。最大
    `reliability_bin_count` 個で、範囲は重ならず単調増加する

### `src/ml/evaluation/compile_parity.py`

```python
@attrs.frozen
class CompileOptions:
    backend: str = "inductor"
    mode: str = "default"
    fullgraph: bool = False
    dynamic: bool | None = None

@attrs.frozen
class ParityTolerance:
    relative: float
    absolute: float
    def validate(self) -> str | None

@attrs.frozen
class CompileParityTolerances:
    output: ParityTolerance
    loss: ParityTolerance
    gradient: ParityTolerance
    def validate(self) -> str | None

FLOAT32_PARITY_TOLERANCES: CompileParityTolerances   # 呼び出し側が明示的に選ぶ既定値

@attrs.frozen
class TensorDifference:
    maximum_absolute_difference: float
    maximum_relative_difference: float
    within_tolerance: bool

@attrs.frozen
class CompileParityResult:
    outputs: tuple[TensorDifference, ...]
    loss: TensorDifference
    gradient: TensorDifference
    checked_gradient_count: int
    mismatched_gradient_parameters: tuple[str, ...]
    missing_gradient_parameters: tuple[str, ...]      # 片側だけ grad が None のもの
    non_finite_gradient_parameters: tuple[str, ...]
    eager_seconds: float
    compiled_seconds: float
    compile_setup_seconds: float
    @property
    def passed(self) -> bool

def compare_eager_and_compiled(
    model: nn.Module,
    inputs: Sequence[Tensor],
    *,
    loss: Callable[[tuple[Tensor, ...]], Tensor],
    tolerances: CompileParityTolerances,
    options: CompileOptions,
) -> tuple[CompileParityResult | None, str | None]
```

- 許容誤差は `CompileParityTolerances` 値オブジェクトで渡す。**引数にデフォルトを置かず**、
    module 定数 `FLOAT32_PARITY_TOLERANCES` を呼び出し側が明示的に渡す
- target と sample weight は `loss` クロージャが閉じ込める。ml は「model 出力タプル → 0-dim loss」
    しか知らない
- model は 2 つ deepcopy して同一 weight を確認したうえで、片方だけ `torch.compile` する。
    device 解決はしない（呼び出し側が model と入力を置いた device で走る）。CUDA のときだけ
    計測境界で `torch.cuda.synchronize()` する
- `torch.compile` 自体が失敗したら例外を投げず理由文字列を返す
- 「両方 grad が None」は不一致にしない（mask なし forward で padding pixel が使われない等、
    正常なケースがある）

### `tests/ml/test_architecture.py`

`RUNTIME_MODULES` に以下を追加する（アルファベット順を維持）。

```python
"ml.evaluation.compile_parity",
"ml.evaluation.regression",
"ml.evaluation.slices",
"ml.model.blocks",
"ml.model.heads",
"ml.model.inspection",
"ml.model.loss",
```

`DEPENDENCY_FREE_MODULES` は変更しない（いずれも torch を読む）。

## 実装ステップ

1. `src/ml/model/__init__.py` と `src/ml/evaluation/__init__.py`（docstring のみ、
    `ml/data/__init__.py` と同じ書式）、`tests/ml/model/__init__.py`、
    `tests/ml/evaluation/__init__.py`
2. `ml/model/blocks.py` — conv builder / residual block / stage builder /
    `replace_invalid_pixels` / `ImageEncoderConfig` / `ImageEncoder`
3. `ml/model/heads.py` — `GaussianHeadConfig` / `GaussianRegressionHead` /
    `GaussianImageRegressor`（2 に依存）
4. `ml/model/loss.py` — 2、3 に依存しない
5. `ml/model/inspection.py` — 依存なし（テストは 2、3 の model を使う）
6. `ml/evaluation/regression.py` — `GaussianPredictions` / metric / offset
7. `ml/evaluation/slices.py` — 6 に依存
8. `ml/evaluation/compile_parity.py` — 依存なし（テストは 3、4 を使う）
9. `tests/ml/test_architecture.py` の `RUNTIME_MODULES` 更新
10. `make format && make type && make test-no-hardware`

並列化: (2 → 3)、(4 → 8)、(6 → 7)、5 は独立。3 グループへ分けて並列実装できる。
spec-test-author も同じ 3 グループで分割できる。

## テスト観点

配置は `tests/ml/model/test_{blocks,heads,loss,inspection}.py`、
`tests/ml/evaluation/test_{regression,slices,compile_parity}.py`。すべて `class TestXxx` に集約し、
実 torch で検証する（モックなし）。Raspberry Pi 5 の CPU で回るよう、テスト用 config は
`input_channels=3`、`stem_channels=(8, 16)`、`stage_channels=(16, 32)`、
`blocks_per_stage=(1, 1)`、`group_norm_groups=4`、画像は 32x32〜96x64 に保つ。

### 正常系

- residual block: stride 2 と channel 変更で出力 shape が期待どおり。shape 不変時は projection なし
- **batch size 非依存**: 1 sample 単独の forward と、その sample を含む batch 2 の該当行が一致する
    （GroupNorm が batch 統計を使っていないことの契約）
- **train / eval 一致**: batch size 1 で `model.train()` と `model.eval()` の出力が一致する
- encoder 出力が入力 H/W に依らず `[B, output_features]`（32x32 と 96x64 の両方）
- mask が全 true のときと mask を渡さないときの出力が一致する
- head: mean が常に正 / log_variance が config の範囲に収まる（入力を 1e6 倍しても）
- conditioning: 値を変えると予測が変わる
- loss: 手計算した 2 sample の値と一致 / weight 合計で正規化されている / mean と log_variance の
    両方へ勾配が流れる / `l = log((y-mu)^2)` で最小になる
- **数 sample の過学習**: tiny model + 固定 4 sample を Adam で回すと NLL が下がり、
    MAE が初期比で大きく縮む（200 step 程度に抑える）
- **学習可能 padding pixel への勾配**: 一部が invalid な mask で backward すると
    padding pixel の grad が非ゼロになる。逆に padded 領域の画素値を書き換えても出力が変わらない
- inspection: 単一 Conv2d（3→8, k3, s1, 8x8 入力）の MAC が手計算値と一致 / Linear も同様 /
    freeze 後の `count_parameters(trainable_only=True)` が減る
- metrics: 手計算した 3 sample の各値と一致 / weight を変えると値が変わる /
    重み一様なら p95 が `torch.quantile` と一致
- offset: 真の log_variance から `-2.0` ずらした予測に対して fit した offset が `+2.0` に近い /
    fit した offset の NLL が offset ± 0.1 の NLL より小さい
- slices: 数値次元で各 sample がちょうど 1 つの bucket に入る / カテゴリ次元が値の昇順 /
    slice の sample_count 合計が全体と一致 / reliability bin が単調・非重複で、件数合計が
    有効 sample 数と一致 / 同じ std が複数 bin に分かれない
- compile parity: `backend="eager"` で forward / loss / gradient が完全一致し `passed` が真

### 異常系

- conv builder: channel が groups で割り切れない / stride が 0 以下 → `ValueError`
- `ImageEncoderConfig.validate()`: タプル長の不一致 / 0 以下の channel / groups で割り切れない
    channel → 理由文字列
- `GaussianHeadConfig.validate()`: `log_variance_minimum >= maximum` / hidden が 0 以下 → 理由文字列
- `GaussianImageRegressor.__init__`: head の `input_features` が encoder と食い違う → `ValueError`
- forward: 画像が 4 次元でない / mask の shape や dtype が違う / conditioning の K 不一致 →
    `ValueError`
- `validate_gaussian_inputs`: NaN / 負の weight / weight 合計 0 → 理由文字列
- loss: shape 不一致 → `ValueError`
- metrics: 全 sample が無効 → `(None, 理由)` / 一部無効 → 除外され `invalid_sample_count` に載る
- `fit_gaussian_log_variance_offset`: 誤差が全て 0 で offset が発散 → `(None, 理由)`
- slices: 次元の値列長が prediction と不一致 → `ValueError`
- compile parity: compile 自体が失敗 → `(None, 理由)`

### エッジケース

- sample 1 個だけの metric（重み付き percentile の `W - w_i == 0`）
- 有効 sample が 0 個の slice → `metrics=None` と理由を持つ `DiagnosticSlice`
- 数値次元の値が全て同じ → bucket が 1 個に縮退する
- log_variance が clamp 下限・上限に張り付いた入力での NLL が有限
- 画像の最小サイズ（`total_stride` と同じ H/W）で forward が通る
- mask が全 false の入力（画素が全て padding pixel に置換される）で forward が例外にならない

### `torch.compile` の扱い

- 常時実行するテストは `CompileOptions(backend="eager")` を使う。比較機構そのものの検証が目的で、
    C++ compiler や Inductor の可用性に依存させない
- Inductor を使う 1 本は `tests/helpers.py` に足す capability probe（1 度だけ小さな関数を
    compile して可否を判定し `functools.cache` で保持）で skip 制御する。
    `skip_if_no_mdns` と同じ「実物があるときだけ実物で検証する」構造
- 実機 Pi 5 での Inductor parity と初回 compile 時間の実測はユーザー実施（MR6 で
    `@mark_hardware` へ寄せる）

## 想定リスク・トレードオフ

- **Inductor の CI コスト。** Pi の pytest job は 30 分 timeout。Inductor は初回 compile に
    数十秒〜数分かかることがある。上記のとおり既定を eager にし、Inductor は probe 付き 1 本に
    限定する。それでも超過するなら Inductor テストを `@mark_hardware` へ移す
- **重み付き percentile の定義。** 定義が一意でないため「重み一様なら `torch.quantile` と一致」を
    契約としてテストで固定する。採らなかった案は「percentile だけ重み無し」で、他の metric と
    重みの扱いが食い違うため退けた
- **Softplus 固定。** 負値を取り得る回帰には使えない。`mean_activation` を config 化する案は、
    metric 側も相対誤差・coverage で正の target を前提にしているため、片方だけ汎用化しても
    意味がないと判断した
- **encoder に freeze の seam を作らない。** doc の Pi fine-tuning は「stage 3 + MLP + head +
    padding pixel だけ更新」だが、その API は利用者が現れる MR4 で足す。MR3 では
    `count_parameters(trainable_only=True)` だけを用意する
- **`GaussianImageRegressor` を ml に置くこと。** encoder + head の合成は 30 行程度で、
    ドメイン側に書かせる案もある。MR3 単体で過学習・parity・inspection を検証でき、MR6 の
    ONNX export wrapper が単一 module を前提にできる点を優先した
- **`ml.data.split` の `validate_split_ratios` が module 関数のまま残っている**（`SplitRatios.validate()`
    と二重）。別 agent が整理中。MR3 は最初から `validate()` メソッドだけを持つ形で書く
- **docstring の折り返し。** `docformatter --wrap-descriptions=72` が日本語の複数文段落を壊す。
    説明部は 1 文ごとに空行で区切る（MR2 で commit が中断した既知の摩擦）

## 確認事項

1. **誤差の呼称変更。** doc §3 の「正規化誤差 e」を `relative_error_mean` /
    `relative_error_standard_deviation` / `relative_error_score` に改名する。primary gate
    `abs(mean(e)) + std(e) <= 0.10` の意味は変えない。暫定案はこの改名で進め、MR3 の
    ドキュメント段階で doc 側の表記も揃える。doc の語をそのまま残す方がよければ指示がほしい
2. **`GaussianImageRegressor` を ml 側に置くか。** 暫定案は ml 側（上記トレードオフ参照）。
    「合成はドメインの責務」とする方針なら、MR3 のテストは tests 側で組んだ合成 model を使う形へ変える
3. **compile parity の既定 backend。** 暫定案は「常時テストは eager、Inductor は capability probe
    付きで 1 本」。CI で常に Inductor を通したい場合は時間計測を先に取る必要がある

## 参照

- 全体計画: `/home/gop/.claude/plans/mr185-codex-docs-image-based-dispense-ca-modular-sonnet.md`
- 仕様: `docs/image-based-dispense-calibration-ml-plan.md` §2（モデル）、§3（loss と metric）、
    §6（評価順序）
- MR1 / MR2 の判断記録: `memory/agents/orchestrator/ml-core-1-package-artifact.md`、
    `memory/agents/orchestrator/ml-core-2-data-pipeline.md`
- 既存の構造契約: `tests/ml/test_architecture.py`
- 規約: `AGENTS.md`、`memory/feedback_validation_method.md`、`memory/feedback_no_try_catch.md`、
    `memory/feedback_no_private_test.md`、`memory/feedback_test_class.md`、
    skill `testing-strategy`、skill `refactor-conventions`
- 参考実装（玉石混交、良い部分だけ）: `origin/feature/2026-09-01/paste-volume-ml` の
    `src/ml/model/residual.py`、`src/ml/evaluation/regression.py`、
    `src/ml/evaluation/compile_parity.py`、`src/ml/paste_volume/{model,metrics,reporting}.py`
