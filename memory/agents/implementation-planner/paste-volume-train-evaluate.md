# paste_volume 学習・評価（Phase 2 + Phase 3）実装計画

## 概要

`ml.paste_volume` に model / task / entrypoint / packaged config / Optuna / MLflow を足し、
5 session の **session leave-one-session-out 5-fold** を実走させて評価 report を出すところ
まで。`ml` コアは既に必要な部品を持っているので、新規に書くのは**ドメイン固有の組み立てと
argv 所有層**が中心になる。

---

## スコープ

作る module:

```
ml.paste_volume.model      # v1 encoder + head の config 組み立てと fine-tune 範囲
ml.paste_volume.task       # PasteVolumeTask（TrainingTask 実装）。TrainingData は既存
ml.paste_volume.train      # argv を所有する training entrypoint（__main__ 付き）
ml.paste_volume.evaluate   # argv を所有する evaluation entrypoint（fold 集約を含む）
ml.paste_volume.search     # Optuna 探索 entrypoint
ml.paste_volume.conf/      # packaged config group（TOML）
ml.paste_volume.cli        # dataset validate / summarize のみ
```

### `ml.paste_volume.release` を作らない根拠（実測に基づく）

1. **release の中身は既に `ml.export.promotion` にある。** `src/ml/export/promotion.py` は実装
   済みで、`tests/ml/export/test_promotion.py` が緑。ドメイン層 `release` の仕事はそこへ渡す
   gate 値の束ね直しだけになる。
2. **gate の 3 本のうち 2 本がこの MR で測れない。** 仕様書 §6「promoted artifact が精度、
   coverage、**export parity**、**Pi p95 1 秒**の全 gate を通る」。export parity は
   Phase 4（ONNX export）、Pi p95 は実機 benchmark（ユーザーが実行）。gate の 2/4 が空の
   promotion API は、通ったことに意味が無い。
3. **promotion が切り替える対象（model package）が存在しない。** `ImmutablePackage.publish` /
   `ActivePointer.switch` が受け取るのは Phase 4 の export 成果物。この MR の成果物は
   `best.pt`（PyTorch checkpoint）までで、切り替える active model が無い。

同じ理由で `cli export / optimize / benchmark / infer` も作らない。

### 仕様書からの意図的な逸脱（要記録）

**`data.manifest`（composite manifest）を作らない。** 仕様書 §3 の
`data/paste_volume.toml` は「相互排他的な `manifest` と `roots`」を持つが、composite manifest を
作る `dataset merge` は Phase 1 で実装されていない（`grep -rn composite src/ml/` が 0 件）。
この MR は `roots` だけを持ち、`manifest` は `dataset merge` と同じ MR へ回す。TOML に
書けない key は strict converter が拒否するので、黙って無視される事故は起きない。

---

## 実測した事実（計画の前提）

### 実データ（5 session、`data/paste-volume-datasets/`、git 管理外）

| session | n | blank | measured min | max | mean |
| --- | ---: | ---: | ---: | ---: | ---: |
| ...08T144137 | 160 | 4 | 0.0435 | 0.3045 | 0.1740 |
| ...08T153829 | 164 | 3 | 0.0518 | 0.3625 | 0.2072 |
| ...09T120737 | 164 | 3 | 0.0431 | 0.3019 | 0.1725 |
| ...09T130756 | 164 | 3 | 0.0324 | 0.2270 | 0.1297 |
| ...09T145923 | 164 | 3 | 0.0357 | 0.2497 | 0.1427 |
| **合計** | **816** | **16** | **0.0324** | **0.3625** | **0.1652**（median 0.1601） |

### `LeaveOneGroupOutPlan.build` が session LOSO に使えること（コードを追って確認済み）

`src/ml/data/split.py:206-269`。`group_values = {session_fp: session_fp}`（5 件）を渡すと:

- `values = sorted(set(...))` → 5 種類。`len(values) < 2` を通過し `available=True`
- 各 value について `held_out` は 1 group、`remaining` は 4 group。`len(remaining) < 2` に
  掛からない（4 >= 2）ので `unavailable` は空
- `validation_count = min(max(1, round(4 * 0.15)), 3)` = `min(max(1, 1), 3)` = **1**
- → **5 fold、各 fold が held-out 1 / validation 1 / train 3**（Python で検算済み）

`fold_seed = _derived_seed(f"{seed}:{dimension}:{value}")` なので fold ごとに独立。

### cell group では LOSO にならないこと（設計の要）

`_cell_key` は `f"{x:.3f},{y:.3f},{w:.3f},{h:.3f}"`（`index.py:341-348`）で座標由来。5 session
とも同じ 47.5x20 銅板・同じ cell 配置なので **cell group は session をまたいで同一値**。
cell group 166 個 × 各 5 sample となり、どの split にも 5 session 全部が入る。

**さらに致命的な帰結**: `SplitManifest.validate` は「1 group が複数 split にまたがる」ことを
拒否する（`split.py:170-175`）。cell group のまま session LOSO の manifest を書くと、
既存 manifest の再読み込みが**必ず失敗する**。つまり cell group と session LOSO は併存
できず、`sample_groups()` の次元切り替えが必須。

### v1 encoder は `ml.model` の組み立てだけで作れること（parameter 数で検算済み）

仕様書 §2 の諸元を `ImageEncoderConfig` / `GaussianHeadConfig` へ写し、手計算すると:

| 部位 | parameter |
| --- | ---: |
| stem1 Conv(6→24,3x3,bias なし)+GN | 1,344 |
| stem2 Conv(24→32)+GN | 6,976 |
| stage1 block1（projection あり） | 36,384 |
| stage1 block2 | 41,664 |
| stage2 block1（stride 2、projection あり） | 129,600 |
| stage2 block2 | 166,272 |
| learnable padding pixel `[1,6,1,1]` | 6 |
| head trunk `Linear(97,128)` | 12,544 |
| mean / logvar `Linear(128,1)` ×2 | 258 |
| **合計** | **395,048** |

**仕様書の 395,048 と一致する。** `total_stride = 2*2*1*2 = 8`、`output_features = 96`、
`conditioning_features=1` で trunk が `Linear(96+1,128)` になるのも仕様書どおり。

→ **新しい `nn.Module` は 1 個も要らない。** `ml.paste_volume.model` が書くのは config の
組み立て・実測検証・freeze 適用だけ。

### `_derived_seed` の役割ラベル（step 0 の対象）

`src/ml/data/image.py:165` が `_derived_seed(f"{global_seed}:{epoch}:{sample_id}")`。
比較対象は `batch.py:102` の `view-dropout`、`batch.py:263` の `batch-plan`、
`paste_volume/batch.py:_placement_seed` の `placement`。**augmentation だけラベルが無い。**

### その他

- `TrainingCheckpoint` は **model 構成を保存しない**（`checkpoint.py:252-266`）。`evaluate` は
  config から model を再構築してから `model_state` を load する。仕様書 §5 の
  `config.json` / `split.json` / `weights.pt` は `CheckpointStore`（`latest/best/final/emergency`
  の 4 role のみ）が書かないので、**entrypoint 側の責務**
- `ml/config/conf/trainer/edge.toml` は key を**ファイル root**へ置いている。単体で
  `TrainerConfig` へ構造化する用途だから成り立つ形で、複数 group を merge する
  `ml.paste_volume.conf` では衝突する。**ドメイン側の option file は必ず自分の group 名の
  table 配下へ書く**
- `RUNTIME_MODULES` / `DEPENDENCY_FREE_MODULES`（`tests/ml/test_architecture.py:75-110`）へ
  新 module を追記する必要がある

---

## 公開インターフェース案

### `ml.data.image`（step 0、既存の変更）

```python
# 材料へ役割ラベルを挟む。振る舞いは変わるが公開シグネチャは変わらない。
_AUGMENTATION_ROLE = "augmentation"
# parameters_for 内: _derived_seed(f"{global_seed}:{epoch}:{_AUGMENTATION_ROLE}:{sample_id}")
```

### `ml.paste_volume.index`（既存の変更）

```python
type SplitDimension = Literal["session", "cell"]
SPLIT_DIMENSIONS: tuple[SplitDimension, ...] = ("session", "cell")

class PasteVolumeSampleIndex:
    def sample_groups(self, *, dimension: SplitDimension) -> dict[str, str]: ...
    #   "session" -> {sample_id: session_fingerprint}
    #   "cell"    -> {sample_id: cell_key}（従来の挙動）
    def session_values(self) -> tuple[str, ...]: ...
    #   整列済みの session_fingerprint 一覧
    def resolve_session(self, selector: str) -> tuple[str | None, str | None]: ...
    #   label 完全一致か session_fingerprint の前頭一致で 1 件へ解決。
    #   0 件・複数件は理由文字列。CLI から人が打てる名前を受けるための層。
```

**`dimension` は keyword 必須で既定値を持たせない。** 既定を `"cell"` にすると呼び出し側が
黙って leak する split を選んでしまう。このリポジトリが `_derived_seed` で 4 回踏んだのと
同じ「ラベル無しの既定」の形。

### `ml.paste_volume.task`（既存の変更 + 追加）

```python
@attrs.frozen
class PasteVolumeTrainingConfig:
    split_dimension: SplitDimension = "session"
    held_out_session: str | None = None   # session 次元では必須。label か fp 前頭一致
    validation_ratio: float = 0.15        # session 次元でのみ使う
    ratios: SplitRatios = SplitRatios(0.70, 0.15, 0.15)  # cell 次元でのみ使う
    split_seed: int = 0
    max_batch_pixels: int = 8_388_608
    max_batch_size: int = 32
    def validate(self) -> str | None: ...
    #   session 次元で held_out_session が None、cell 次元で held_out_session が
    #   非 None の組み合わせをそれぞれ拒否する

class PasteVolumeTrainingData(TrainingData[PasteVolumeBatch]):
    @classmethod
    def build(
        cls,
        index: PasteVolumeSampleIndex,
        *,
        collator: PasteVolumeCollator,
        config: PasteVolumeTrainingConfig,
        split_manifest_path: Path | None = None,
    ) -> tuple[Self | None, str | None]: ...   # 既存シグネチャ据え置き
    @property
    def split_dimension(self) -> SplitDimension: ...
```

`_resolve_split` を次元で分岐させる。session 次元の新経路:

```python
def _leave_one_session_out_manifest(
    index: PasteVolumeSampleIndex,
    *,
    held_out: str,
    seed: int,
    validation_ratio: float,
) -> tuple[SplitManifest | None, str | None]: ...
```

- `groups = index.sample_groups(dimension="session")`
- `LeaveOneGroupOutPlan.build({v: v for v in index.session_values()}, dimension="session",
  seed=seed, validation_ratio=validation_ratio)`
- `available=False` ならその理由を返す（sample 単位へ fallback しない）
- `held_out` に一致する fold を選び、`test = held_out_group_ids`、
  `validation = validation_group_ids`、`train = train_group_ids` として
  `SplitManifest(...)` を直接構築する
- 既存 manifest を読む経路は `manifest.validate(groups, ...)` を同じ次元の groups で呼ぶ

**cell 単位 split は残す。** 消さない理由は 2 つ。(a) 素朴ベースラインの再測定と model の
比較で「cell 単位なら出るが session LOSO では出ない」という差そのものが k を憶えている
証拠になり、これが今回の設計判断の根拠を実測で支える。(b) `experiment=cell_split` の 1 行で
切り替えられ、維持コストが分岐 1 本で済む。ただし**既定は `"session"`**。

### `ml.paste_volume.model`（新規）

```python
MODEL_FAMILY = "paste-volume-resnet-small-v1"
INPUT_CHANNELS = 6              # pre RGB 3 + post RGB 3
CONDITIONING_FEATURES = 1       # log(有効 pixel_per_mm)

@attrs.frozen
class PasteVolumeModelConfig:
    """v1 encoder の形。既定が仕様書 §2 の確定値そのもの."""
    stem_channels: tuple[int, ...] = (24, 32)
    stem_strides: tuple[int, ...] = (2, 2)
    stage_channels: tuple[int, ...] = (48, 96)
    stage_strides: tuple[int, ...] = (1, 2)
    blocks_per_stage: tuple[int, ...] = (2, 2)
    group_norm_groups: int = 8
    hidden_features: int = 128
    log_variance_minimum: float = -14.0
    log_variance_maximum: float = 5.0
    mean_bias_initial: float = 0.15      # 下の「決めた根拠」参照
    initial_weights: Path | None = None  # fine-tune の起点
    fine_tune: bool = False              # True で最終 stage / MLP / head / padding のみ更新

    def validate(self) -> str | None: ...
    def encoder_config(self) -> ImageEncoderConfig: ...
    def head_config(self) -> GaussianHeadConfig: ...
    def validate_for_constraints(self, constraints: ImageConstraints) -> str | None: ...
        # ImageEncoderConfig.validate_for_constraints へ委譲（total_stride 8 <= minimum_size 16）

def build_paste_volume_model(
    config: PasteVolumeModelConfig,
) -> tuple[MultiViewGaussianRegressor | None, str | None]: ...

def apply_fine_tune_freeze(model: MultiViewGaussianRegressor) -> tuple[str, ...]: ...
    """stem と residual stage 1 を requires_grad=False にし、凍結した parameter 名を返す.

    更新対象は residual stage 2（= 仕様書の「stage 3」相当。v1 は 2 stage 構成なので
    最終 stage）、pooling 後の Linear、2 個の head、learnable padding pixel。
    """

def measure_paste_volume_model(
    model: MultiViewGaussianRegressor, *, height: int, width: int, view_count: int
) -> ModelSize: ...
```

**`mean_bias_initial = 0.15` の根拠。** 実データ 816 sample の measured 平均 0.1652 /
median 0.1601、session ごとの平均は 0.1297〜0.2072。0.15 はどの session 平均からも 1.4 倍
以内で、仕様書が示す「この用途の真値スケール 0.05〜0.2 uL」の内側。**train split の統計から
動的に決めない**（fold ごとに値が変わると `TrainerConfig.fingerprint` と別に model config が
fold 依存になり、run 間の比較が読めなくなる）。実際の train split 平均は MLflow の param
として毎 run 記録し、0.15 と 3 倍以上乖離したら理由を報告する。

### `ml.paste_volume.task`（TrainingTask 側、新規クラス）

```python
class PasteVolumeTask(TrainingTask[PasteVolumeBatch, GaussianObservation]):
    """PasteVolumeBatch を GaussianRegressionTask の契約へ橋渡しする.

    GaussianRegressionTask は GaussianBatch（4 次元 images）を前提にしているので継承
    できない。委譲でも同じ理由で使えないため、weighted_gaussian_negative_log_likelihood
    を直接呼ぶ薄い実装にする（loss の式は ml.model.loss の 1 本を共有する）。
    """
    def __init__(self, model: MultiViewGaussianRegressor) -> None: ...
    @property
    def model(self) -> nn.Module: ...
    def training_step(self, batch: PasteVolumeBatch) -> StepResult[GaussianObservation]: ...
    def evaluation_step(self, batch: PasteVolumeBatch) -> GaussianObservation: ...
    def reduce(self, observations: Sequence[GaussianObservation]) -> Mapping[str, float]: ...
        # MeanSaturationDiagnostic は常に、GaussianRegressionMetrics は測れたときだけ。
        # 加えて ZeroTargetMetrics（blank 16 件の観測点）を必ず載せる
    def compile_forward(self, options: CompileOptions) -> None: ...
```

`GaussianObservation` に `sample_ids` は載せない（`ml` 側の型を変えない）。session slice は
evaluate 側が split manifest から引き直す。

### `ml.paste_volume.conf`（新規、packaged config group）

```
src/ml/paste_volume/conf/
├── base.toml                          # 全 run 共通の骨格
├── data/paste_volume.toml             # [data] roots / constraints / augmentation / batch
├── model/resnet_small.toml            # [model] （既定と同値は書かない → ほぼ空 + コメント）
├── trainer/gpu.toml                   # [trainer] AMP + compile + 200 epoch
├── trainer/pi.toml                    # [trainer] CPU FP32 / accum 4 / deadline 3300
├── logger/mlflow.toml                 # [logger] file: tracking URI
├── experiment/base.toml               # session LOSO（正式評価）
├── experiment/cell_split.toml         # cell 単位 70/15/15（比較用）
├── experiment/fine_tune.toml          # [model] fine_tune=true + [trainer] lr 1e-4
└── hyperparameter_search/base_optuna.toml
```

**規約: option file の top-level key はその group 名 1 つだけ。** `ml/config/conf/trainer/
edge.toml` が root へ直書きしているのは単体構造化用の別用途。混ぜると merge が壊れる。

構造化の到達先:

```python
@attrs.frozen
class PasteVolumeDataConfig:
    roots: tuple[Path, ...]
    constraints: ImageConstraints = ImageConstraints()
    augmentation: AugmentationRange = AugmentationRange()
    view_dropout: ViewDropout = ViewDropout()
    global_seed: int = 0
    split_dimension: SplitDimension = "session"
    held_out_session: str | None = None
    validation_ratio: float = 0.15
    ratios: SplitRatios = SplitRatios(0.70, 0.15, 0.15)
    split_seed: int = 0
    max_batch_pixels: int = 8_388_608
    max_batch_size: int = 32
    def validate(self) -> str | None: ...
    def collator(self) -> PasteVolumeCollator: ...
    def training_config(self) -> PasteVolumeTrainingConfig: ...

@attrs.frozen
class ExperimentLoggerConfig:
    tracking_uri: str
    experiment_name: str
    def validate(self) -> str | None: ...
    def build(self) -> ExperimentLogger: ...   # MLflowExperimentLogger を返す

@attrs.frozen
class ResumeConfig:
    checkpoint: Path | None = None

@attrs.frozen
class PasteVolumeExperimentConfig:
    """train / search が argv から組み立てる root config."""
    data: PasteVolumeDataConfig
    trainer: TrainerConfig
    model: PasteVolumeModelConfig = PasteVolumeModelConfig()
    logger: ExperimentLoggerConfig | None = None
    resume: ResumeConfig = ResumeConfig()
    hyperparameter_search: SearchConfig | None = None
    run_kind: str = "base-train"
    run_directory: Path = Path("runs")
    def validate(self) -> str | None: ...

def compose_experiment(
    arguments: Sequence[str],
) -> tuple[PasteVolumeExperimentConfig | None, str | None]: ...
    """argv を PackagedConfiguration.locate() 配下で合成して構造化する.

    ConfigComposition.from_arguments(arguments, configuration_root=root,
    base_names=("base",)) → structure(PasteVolumeExperimentConfig,
    converter=make_strict_converter())。
    """
```

### `ml.paste_volume.train`（新規）

```python
def main(argv: Sequence[str] | None = None) -> int: ...
    """1 fold ぶんの学習を最後まで回す。0 は成功、1 は理由を stderr へ出して失敗."""

def run_training(
    config: PasteVolumeExperimentConfig,
    *,
    logger: ExperimentLogger,
    device: torch.device | None = None,
) -> tuple[TrainingOutcome | None, str | None]: ...
```

`run_training` がやること（順に）:

1. `PasteVolumeSampleIndex.from_roots(config.data.roots, constraints=...)`
2. `PasteVolumeTrainingData.build(...)`（split を決め、`run_directory/split.json` へ保存）
3. `build_paste_volume_model` → `initial_weights` があれば load → `fine_tune` なら freeze
4. `measure_paste_volume_model` で parameter 数 / GMAC を実測し、
   **1.5M parameter・1.5 GMAC の上限を超えたら学習を始めずに理由を返す**（仕様書 §2）
5. 解決済み config を `run_directory/config.json` へ書き、MLflow artifact へ上げる
6. `GitProvenance.capture` / `DependencyVersions.collect` を tag / param へ
7. `Trainer(...).run(resume_from=config.resume.checkpoint, run_kind=config.run_kind)`
8. 終了後、validation split で `fit_log_variance_offset` を求めて
   `run_directory/calibration.json` へ保存（mean は変えない）
9. `best.pt` の `model_state` を `run_directory/weights.pt` へ（model config 同梱）

**1 プロセス = 1 fold = 1 MLflow run。** 5 fold を 1 プロセスで回さないのは、`Trainer` /
`CheckpointStore` / `MLflowExperimentLogger` がいずれも「1 run」を単位に resume と
artifact を組んでいるため。5 fold は shell の for ループで起こす。

### `ml.paste_volume.evaluate`（新規）

```python
def main(argv: Sequence[str] | None = None) -> int: ...

@attrs.frozen
class FoldEvaluation:
    held_out_session: str
    split: SplitName
    sample_count: int
    metrics: GaussianRegressionMetrics
    zero_target: ZeroTargetMetrics | None
    mean_saturation: MeanSaturationDiagnostic
    log_variance_offset: float | None

@attrs.frozen
class CrossValidationReport:
    dataset_fingerprint: str
    model_family: str
    folds: tuple[FoldEvaluation, ...]
    def aggregate(self) -> Mapping[str, float]: ...   # fold 平均と標準偏差
    def save(self, path: Path, *, converter: Converter) -> None: ...

EVALUATION_REPORT_DOCUMENT = DocumentKind(
    kind="paste-volume-cross-validation-report", schema_version=1
)
```

argv:

```text
python -m ml.paste_volume.evaluate \
    checkpoint=/abs/best.pt data.roots=[...] split=validation
python -m ml.paste_volume.evaluate \
    folds=/abs/runs split=test allow_frozen_test=true output=/abs/report.json
```

`split=test` は `allow_frozen_test=true` を要求する（仕様書 §7）。`folds=` は
`<dir>/<held-out>/best.pt` を全部読んで `CrossValidationReport` を作る。
session ごとの slice は `CategoricalDimension(name="session", values=...)` を
`DiagnosticReport.build` へ渡す。

### `ml.paste_volume.search`（新規）

```python
def main(argv: Sequence[str] | None = None) -> int: ...

@attrs.frozen
class SearchConfig:
    storage_uri: str
    search_space: SearchSpace
    trial_count: int = 20
    direction: Direction = "minimize"
    results_path: Path | None = None
    def validate(self) -> str | None: ...
```

objective は `TrialAssignment.as_override_arguments()` を**元の argv の末尾へ足して
`compose_experiment` を再実行**し、`run_training` の
`outcome.best_monitor_value` を返す。これで探索 run と単発 run が同じ設定経路を通る。
`StudyIdentity.build(model_family=MODEL_FAMILY, dataset_fingerprint=index.dataset_fingerprint,
search_space_fingerprint=space.fingerprint)`。

### `ml.paste_volume.cli`（新規、dataset のみ）

```python
def main(argv: Sequence[str] | None = None) -> int: ...
# python -m ml.paste_volume.cli dataset validate <root...>
# python -m ml.paste_volume.cli dataset summarize <root...> [--json]

@attrs.frozen
class DatasetSummary:
    session_count: int
    sample_count: int
    blank_count: int
    rejection_count: int
    dataset_fingerprint: str
    smallest_source_size: int
    cell_group_count: int
    measured_volume_minimum_ul: float
    measured_volume_maximum_ul: float
    measured_volume_mean_ul: float
    sessions: tuple[SessionSummary, ...]   # label / fp / n / blank / mean / pixel_per_mm

def summarize_dataset(
    roots: Sequence[Path], *, constraints: ImageConstraints
) -> tuple[DatasetSummary | None, str | None]: ...
```

`summarize` の `measured_volume_mean_ul` が `mean_bias_initial` の妥当性を測る観測点を
兼ねる。`cli` は mlflow / optuna を import しない（`ml-runtime` だけで動く）。

---

## 実装ステップ

各ステップの検証は container 内で行う。個別実行:

```bash
docker compose -f docker/ml/compose.yaml -f docker/ml/compose.credentials.yaml \
  exec -T ml uv run pytest <path> -m "not hardware" -q
```

全体は `make ml-docker-check`。**`make test` / `make run` / `pytest -m hardware` は実行しない。**

### step 0（独立 commit、最優先）augmentation の材料へ役割ラベルを足す

- `src/ml/data/image.py`: `_derived_seed(f"{global_seed}:{epoch}:augmentation:{sample_id}")`
- `_derived_seed` の docstring を「4 用途とも役割ラベルを挟む」へ書き替える
  （現状は「augmentation がラベル無しで占有している」と書いてある）
- `src/ml/paste_volume/batch.py` の `_placement_seed` docstring と、
  `tests/ml/paste_volume/test_batch.py:511-518` の docstring を追随させる

**既存テストへの影響（実測して確認済みの範囲）:**

| テスト | 影響 |
| --- | --- |
| `test_image.py` の `parameters_for` 系 5 件 | 全て関係性の assert（再現性・入力差・範囲内・恒等）。**golden 値なし → 素通り** |
| `test_batch.py::test_does_not_derive_the_position_from_the_rotation` | `len(pairs) >= 28` の閾値。値は変わるので**再測定が必要**。下回ったら閾値ではなく epoch 数を増やす（閾値を下げると M1 の bug を再び見逃す） |
| `test_task.py` の augmentation 依存 2 件 | shape 系の assert のみ。素通りの見込み |
| checkpoint / resume 系 | checkpoint 未作成なので影響なし |

**この step で足すテスト（自己検査を対にする）:**

- 4 用途（augmentation / placement / view-dropout / batch-plan）の種が、同じ
  `(global_seed, epoch, sample_id)` に対して**互いに一致しない**こと
- **その検査が働くことの自己検査**: ラベルを取り除いた材料どうしなら実際に一致することを
  同じ比較関数で示す。「一致しない」型の assert なので、比較が壊れると常に緑になる

検証: `pytest tests/ml/data tests/ml/paste_volume -m "not hardware" -q`

### step 1 split の次元切り替え

`index.sample_groups(dimension=...)` / `session_values` / `resolve_session`、
`PasteVolumeTrainingConfig` の新 field、`_leave_one_session_out_manifest`。

検証: `pytest tests/ml/paste_volume/test_index.py tests/ml/paste_volume/test_task.py`。
加えて**実 session 5 本で fold 構成を実測**し、held-out 1 / val 1 / train 3 × 5 fold と、
どの fold でも train ∩ test の session が空であることを確認する。

### step 2 model 層

`ml.paste_volume.model`。`RUNTIME_MODULES` へ追記。

検証:
- `ModelSize.measure` で parameter 数が **395,048** ちょうどであることを固定
- `total_stride == 8`、`validate_for_constraints(ImageConstraints())` が None
- 53px / 159px × 5 view の GMAC が仕様書の 0.16 / 1.31 と桁で整合
- **trunk ReLU の死を 100 seed で実測**（仕様書 §2 申し送り 2）。観測は「全 0 でない
  feature を与えたとき head 出力が sample 間で動くか」。0/100 でなければ
  `hidden_features` を再検討し、実測値を計画へ追記
- `apply_fine_tune_freeze` が凍結する parameter 名の集合を固定し、
  **freeze 後も `padding_pixel.requires_grad` が True** であることを見る

### step 3 task 層

`PasteVolumeTask`。`RUNTIME_MODULES` へ追記。

検証: `pytest tests/ml/paste_volume/test_task.py`。
- 合成 8 sample を 200 step で **過学習できる**（train NLL が単調に下がり MAE が
  初期の 1/10 未満）
- 可変 shape batch（bucket 違い 2 種）を続けて通せる
- `padding_pixel.grad` が非 0（padding が生じる batch で）
- eager と `torch.compile` の forward / loss / gradient parity（`CompileParityResult`）

### step 4 packaged config

`conf/` の TOML 一式と `compose_experiment`。

検証: `pytest tests/ml/paste_volume/test_conf.py`。
- 全 group × 全 option が構造化まで通る
- **option file の top-level key が group 名 1 つだけ**（`ml/config/conf/trainer/edge.toml`
  と形が違うことを明示的に固定する）
- attrs 既定値の二重定義が無い（`tests/ml/tuning/test_integration.py:_duplicated_defaults`
  と同じ手口を再利用）
- 未知 key 拒否、`experiment=base` の group 解決、`data.held_out_session=xxx` の override 解決、
  `resume.checkpoint` と `model.initial_weights` の意味が混ざらないこと

### step 5 entrypoint（train / evaluate / search / cli）

検証:
- 合成 3 session（各 6 cell）で `train` を argv から end-to-end 実行し、
  `run_directory` に `config.json` / `split.json` / `best.pt` / `weights.pt` が揃う
- **中断あり / なしで最終 weight と metric が一致する resume test**（SIGTERM 相当を
  step 境界で立て、`latest.pt` から再開）
- `evaluate` が `folds=` から `CrossValidationReport` を作り、`split=test` が
  `allow_frozen_test` 無しで拒否される
- `search` が **実 local sqlite storage** で 2 trial 積み、同じ storage へ別プロセスから
  合流できる（`HyperparameterSearch.collect` で読み戻す）
- `cli dataset validate` / `summarize` が単一 root・複数 root で同じ API を通る

### step 6 MLflow 統合（実 local file store）

`logger/mlflow.toml`、`ExperimentLoggerConfig.build`。

検証: `tests/ml/paste_volume/test_train_mlflow.py`。
`tracking_uri = f"file://{tmp_path}/mlruns"` で `train` を 2 epoch 回し、
`MlflowClient` で run / param / metric / artifact を読み戻す。**MLflow API はモックしない。**
`tests/ml/experiment/test_mlflow.py` は http server を立てているが、こちらは file store で
足りる（仕様書 Phase 3 の要件は「実 local MLflow」であって server 常駐ではない）。

### step 7 ベースライン再測定（session LOSO）

`tests/ml` には置かない。`scripts/` 配下に使い捨ての測定 script を置くか、
`cli dataset summarize` の出力から手で回す。測るのは:

- **大域輝度差の線形回帰**（2 session 時代と同じ特徴量: post 平均輝度 − pre 平均輝度）を
  **session LOSO 5-fold** で。fold ごとの R^2 / MAE と 5 fold 平均
- 参考: cell 単位 70/15/15 でも同じ特徴量を測る。**cell 単位のほうが良く出るはず**で、
  その差が「cell split では k を憶えられる」ことの実測証拠になる
- 定数予測（train 平均）のベースラインも同時に測る。LOSO では k が session 固有なので
  これが意外に強い可能性があり、model がこれを超えなければ意味が無い

### step 8 LOSO 5-fold の実走と report

```bash
for S in <5 session の label>; do
  python -m ml.paste_volume.train experiment=base trainer=gpu logger=mlflow \
      data.roots=["/abs/data/paste-volume-datasets"] data.held_out_session=$S \
      run_directory=/abs/runs/loso/$S
done
python -m ml.paste_volume.evaluate folds=/abs/runs/loso split=test \
      allow_frozen_test=true output=/abs/runs/loso/report.json
```

**成功条件（何をもって「できた」とするか）:**

1. **必須（機能）**: 5 fold すべてが `stop_reason` に `max_epochs` か `early_stopping` で
   終わり、`best.pt` / `weights.pt` / `report.json` が揃う。MLflow から 5 run の
   dataset fingerprint・split fingerprint・config・metric・checkpoint を辿れる
2. **必須（健全性）**: 各 fold の held-out session が train / validation に 1 sample も
   現れない。`MeanSaturationDiagnostic.saturated_positive_fraction` が最終 epoch で 0.05 未満
   （真値が正の sample の平均 head が死んでいない）
3. **必須（学習が起きている）**: 5 fold 平均の test MAE が**定数予測ベースラインを下回る**。
   下回らなければ「実装は動いたが学習していない」であり、この MR の目的を満たさない
4. **目標**: 5 fold 平均の test MAE が **session LOSO の輝度差線形回帰ベースラインを下回る**。
   2 session・cell split 時代の R^2 0.59〜0.62 / MAE 0.039〜0.049 uL は**そのまま比較対象に
   しない**（split も session 数も違う）。step 7 で測り直した値を基準にする
5. **記録**: blank 比率 1.9%（16/832）と coverage の関係を最初の run で観測する
   （仕様書 §2 申し送り 3）。具体的には `ZeroTargetMetrics.exact_zero_fraction` と
   `GaussianRegressionMetrics.one_standard_deviation_coverage`、
   `mean_predicted_standard_deviation` を epoch ごとに記録し、**blank の
   `exact_zero_fraction` が 1 へ張り付くのと同時に正の真値側の coverage が落ちるか**を見る。
   落ちるなら blank の `sample_weight` を下げる調整余地があることを報告する（この MR では
   調整まではせず、観測と結論の記録に留める）

**目標 4 を満たせなかった場合も MR は成立させる。** 5 session・LOSO は 2 session・cell split
より本質的に難しく、精度が出ないこと自体が「k が session 固有定数である」という
データ側の性質の実測結果になる。その場合は report と考察を残し、次の打ち手（session 数を
増やす / 条件変数を足す / blank weight）を提案する。

### 並列化できる範囲（disjoint なファイル群）

step 0 と step 1 は逐次（step 1 が step 0 の commit の上に乗る）。**step 2 以降は
2 レーンへ分けられる。**

| レーン | 書き込む src | 書き込む tests |
| --- | --- | --- |
| A（model / task） | `src/ml/paste_volume/model.py`, `task.py` | `tests/ml/paste_volume/test_model.py`, `test_task.py` |
| B（config / entrypoint） | `src/ml/paste_volume/conf/**`, `train.py`, `evaluate.py`, `search.py`, `cli.py` | `tests/ml/paste_volume/test_conf.py`, `test_train.py`, `test_evaluate.py`, `test_search.py`, `test_cli.py` |

**共有して衝突するのは 2 ファイルだけ**: `tests/ml/test_architecture.py`（`RUNTIME_MODULES` へ
の追記）と `tests/ml/paste_volume/helpers.py`（合成 session の拡張）。両方とも**レーン A に
先に触らせ**、B は A の commit を取り込んでから始める。

`spec-test-author` × `plan-implementer` の同時起動は、**公開インターフェース案の
シグネチャがこの計画で確定している step 2・3・4 について可能**。step 5 以降の entrypoint は
`compose_experiment` の到達先 attrs が step 4 で確定してからにする。

---

## テスト観点

`tests/ml/` 配下のみ。`tests/helpers` を参照しない。`class TestXxx` に集約。
実 session は `skip_if_no_real_sessions` で opt-in、主体は合成 fixture。

### 正常系

- **model**: parameter 395,048 / total_stride 8 / GMAC 上限内 / freeze 対象の集合
- **task**: 数 sample の過学習、可変 shape batch、eager と compile の parity
- **split**: session 5 種で 5 fold、held-out 1 / val 1 / train 3、fold 間で held-out が重複しない
- **config**: 全 group × 全 option の構造化、group 解決と override 解決の振り分け
- **train**: 合成 dataset で end-to-end、成果物 4 種が揃う
- **resume**: 中断あり / なしで最終 weight と metric が一致
- **MLflow**: 実 file store へ書いて `MlflowClient` で読み戻す
- **Optuna**: 実 sqlite storage で trial を積み、別プロセスから合流して `collect` で読む
- **cli**: 単一 root / 複数 root で同じ summary API

### 異常系

- session 次元で `held_out_session` 未指定 → 理由を返す
- `held_out_session` が 0 件 / 複数件へ一致 → それぞれ別の理由
- cell group の manifest を session 次元で読み直す → `SplitManifest.validate` が
  「group が複数 split にまたがっています」で拒否する
- `split=test` を `allow_frozen_test` 無しで → 拒否
- `resume.checkpoint` の dataset / config fingerprint 不一致 → 拒否
- 未知 key を含む TOML / override → strict converter が拒否
- `LeaveOneGroupOutPlan.available=False`（session 1 本） → sample 単位へ fallback せず理由
- parameter 数 / GMAC が上限超過 → 学習を始めずに理由
- `initial_weights` の state_dict のキー集合が現在の model と不一致 → 拒否

### エッジケース

- **blank だけの validation split** → `GaussianRegressionMetrics.measure` が None を返し、
  Trainer が「monitor がありません」で止まる。**止まる前に
  `MeanSaturationDiagnostic` と `ZeroTargetMetrics` が記録されている**こと
  （`GaussianRegressionTask.reduce` の設計意図をドメイン側でも保つ）
- view 数が batch 内で不揃い → `_view_indices` が拒否（既存契約の回帰確認）
- `mean_bias_initial` を極端に大きく / 小さくしたときに model が組めること自体は変わらない
  （`GaussianHeadConfig.validate` は正の有限値のみ要求）
- fold 数 5 のうち 1 fold だけ session の sample 数が 160（他は 164）→ weight が
  `1/session_sample_count` なので session 間の総重みが揃うことを見る

### 「到達しない / 参照しない」型の assert には自己検査を対にする

このリポジトリで **5 回踏んだ**（`memory/agents/orchestrator/paste-volume-data-task.md`）。
この MR で該当するのは:

1. **step 0 の「4 用途の種が一致しない」** → ラベルを外した材料なら一致することを、
   同じ比較関数で示す
2. **「held-out session が train / validation に現れない」** → 検査器が働くことを、
   わざと held-out を train へ混ぜた manifest で示す
3. **`apply_fine_tune_freeze` 後に「stem へ勾配が流れない」** → freeze しない model では
   同じ観測点で勾配が流れることを示す
4. **`cli` が mlflow / optuna を import しない** → `test_architecture.py` の既存機構
   （`TRAINING_ONLY_DEPENDENCIES`）へ載せる。自前の走査を新たに書かない
   （同じツリーに走査器を 2 つ置くと弱いほうが素通り経路になる）

いずれも**自己検査の起点が「その 1 段で届いてしまわないか」まで確かめる**（4 回目に
踏んだ型）。そして自己検査自体を変異で殺せることを測る。

---

## リスクとトレードオフ

| # | 内容 |
| --- | --- |
| R1 | **step 0 の閾値 28 が新しい seed で下回る可能性。** 下回ったら閾値を下げず epoch 数を増やして再測定する。閾値を下げると M1 の bug（材料の衝突）を再び見逃す |
| R2 | **train 3 session（約 490 sample）で 395k parameter は過学習しやすい。** early stopping patience 15 と augmentation で耐える設計だが、5 fold とも validation NLL が早期に反転する可能性がある。その場合は encoder を小さくせず（精度比較なしに構成を変えないのが仕様書 §2 の規約）、augmentation 幅と weight decay を Optuna で探す |
| R3 | **k が session 固有定数（0.65〜1.04、1.6 倍幅）なので、LOSO では原理的に当たらない成分がある。** 画像から k を推定できなければ、held-out session の系統誤差は消えない。目標 4 が達成できないシナリオの主因はこれ。成功条件 3（定数予測超え）を必須と目標に分けたのはこのため |
| R4 | **`torch.compile` の初回 compile が fold ごとに走る。** 可変 shape で再 compile が増えると 5 fold の実走時間が読めない。`trainer=gpu` は compile ON だが、first-step 時間と遭遇 shape 数を MLflow へ記録し、非現実的なら `trainer.compile_enabled=false` で 1 度測り直す（黙って eager へ落とさない） |
| R5 | **`mean_bias_initial` を config 固定にしたので、真値スケールが将来変わると死ぬ。** train split 平均を毎 run 記録して 3 倍乖離を報告する形で緩和する。動的決定にしなかったのは fold 間で model config が変わり比較が読めなくなるため |
| R6 | **`PasteVolumeTask` が `GaussianRegressionTask` を継承も委譲もしない。** loss の式は `ml.model.loss` の 1 本を共有するので二重定義にはならないが、`reduce` の集計は似た形が 2 箇所に出る。`GaussianRegressionTask.reduce` を切り出して共有する選択肢もあるが、`ml` コアの公開インターフェースを触ることになるので v1 では複製を許す |
| R7 | **cell 単位 split を残すコスト。** 分岐 1 本と TOML 1 枚。消す案もあったが、step 7 の比較実測が設計判断の根拠そのものなので残す |
| R8 | **5 fold を shell ループで回す。** 1 プロセス内で 5 fold を回す案は `Trainer` / `CheckpointStore` / MLflow がいずれも 1 run 単位なので採らない。代償として「実走を 1 コマンドで」ができない |

---

## ユーザーへの確認事項

1. **`ml.paste_volume.release` を作らない判断でよいか。** 根拠は上記 3 点（実体は
   `ml.export.promotion` に既存 / gate 4 本中 2 本が Phase 4・実機で測れない / 切り替える
   model package が存在しない）。**暫定案: 作らない。** Phase 4 の export MR へ回す。
2. **成功条件 4（輝度差ベースライン超え）が未達でも MR を成立させてよいか。**
   5 session・session LOSO は 2 session・cell split より本質的に難しく、k が session 固有
   定数である以上、画像から k を読めなければ系統誤差は消えない。**暫定案: 必須は成功条件
   1〜3（機能・健全性・定数予測超え）とし、4 は目標として report に達成 / 未達と考察を残す。**
3. **5 fold の実走を shell の for ループで起こす形でよいか。** `train` は 1 fold = 1 MLflow
   run に閉じる。**暫定案: for ループ。** 1 プロセス 5 fold にすると resume と MLflow run の
   単位が壊れる。

---

## 参照

- 仕様書: `docs/image-based-dispense-calibration-ml-plan.md` §2（344-524）§3（525-716）
  §4（717-765）§5（766-812）§7（958-1123）、Phase 2（1140-1147）Phase 3（1148-1156）
- 前 MR の裁定: `memory/agents/orchestrator/paste-volume-data-task.md`
  （とくに Recovery 節と「3 回踏んだ同じ型」）
- 実測と決定: `memory/agents/orchestrator/paste-volume-train-evaluate.md`
- LOSO の実装: `src/ml/data/split.py:184-269`
- v1 encoder の受け皿: `src/ml/model/blocks.py:130-260`（`ImageEncoderConfig`）、
  `src/ml/model/heads.py:36-95`（`GaussianHeadConfig`）、`src/ml/model/multiview.py`
- 役割ラベルの規則: `src/ml/data/image.py:475-488`（`_derived_seed` の docstring）
- 構造契約: `tests/ml/test_architecture.py:36-133`
- 規約: `AGENTS.md`、`CLAUDE.md`、skill `testing-strategy` / `refactor-conventions`
