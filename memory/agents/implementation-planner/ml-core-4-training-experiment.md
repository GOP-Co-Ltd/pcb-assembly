# コア ML 基盤 MR4: training core と experiment tracking

全体計画は `/home/gop/.claude/plans/mr185-codex-docs-image-based-dispense-ca-modular-sonnet.md` の「MR4」節。
MR1〜MR3 は main へ merge 済み。本 MR のブランチは `feature/2026-09-04/ml-core-4-training-experiment`。
参照実装は `origin/feature/2026-09-01/paste-volume-ml`（MR185）。**設計から作り直す**対象なので、
移植するのは下記「MR185 から取るもの／捨てるもの」に挙げたものだけ。

## 概要

`ml.experiment`（実験記録の抽象と MLflow adapter、provenance）と `ml.training`
（task / data の ABC、optimizer group トランザクション、checkpoint、乱数状態、Trainer）を実装し、
epoch ループ・early stopping・scheduler・checkpoint・deadline・signal をドメイン側から基盤へ持ち上げる。
`tests/ml/support.py` に合成回帰タスク（8×8 画像 → 正のスカラー）を置き、`pcbasm` を一切使わずに
`Trainer.run()` → checkpoint → resume → 評価まで end-to-end で検証する。

## MR185 から取るもの／捨てるもの

| MR185 | MR4 |
| --- | --- |
| `execute_optimizer_group` の「全 batch 成功時だけ commit、metric も commit 後だけ更新」契約 | **そのまま持ち上げる** |
| `(next_batch_index, group 先頭の RNG snapshot)` で group 単位に完全再試行 | **そのまま持ち上げる** |
| compiled callable を別変数に持ち、state 系は常に元 `nn.Module` を触る | **そのまま持ち上げる**（`_orig_mod.` 問題の根絶） |
| 最初の optimizer step 前に `latest` を 1 本置く | **そのまま持ち上げる** |
| checkpoint payload のキー集合完全一致検証 + readback 検証 | **持ち上げる**。ただし atomic 書き込みは `ml.artifact.atomic.atomic_write_stream` に統一（`atomic_torch_save` は作らない） |
| `random_state.py` の numpy state Tensor 化 | **持ち上げる**。`weights_only=True` が `np.ndarray` を読めないためであることをコメントで明記 |
| `OptimizerBoundary`（可変共有オブジェクト） | **廃止**。`OptimizerGroupResult.optimizer_state_dirty` に畳む |
| `committed: bool` + 例外 `FloatingPointError` | **廃止**。`OptimizerGroupResult.outcome` の Literal に畳む |
| `MetricReducer` Protocol | **廃止**。`TrainingTask.reduce()` に統合 |
| `ExperimentLogger` / `OperationLogger` の同型 Protocol 2 本 | **廃止**。ABC 1 本 |
| `_metric_queue` の死んだバッファリング、retry の tight loop、`verify_connection()` の手書き `/health` probe | **廃止**（`start()` が既に fail-fast） |
| signal flag の二重管理（outer / inner で `requested` が伝播しない） | **修正**。flag は 1 個 |
| MLflow の step 軸が epoch と global_step で混在 | **修正**。step 軸は `global_step` のみ |
| 中断 epoch でも full validation を走らせる | **修正**。中断 epoch は validation を実行しない |
| `best.pt` の `epoch` が best epoch + 1 | **修正**。epoch 加算と best 判定を別の純遷移に分ける |
| finalization で checkpoint を load → 書き換え → 再 save（`uncertainty_log_variance_offset` の後追い注入） | **廃止**。checkpoint は不変。log-variance offset 較正は Trainer の責務にしない |
| `logger.end()` が経路により 2 回呼ばれうる | **修正**。`end` は 1 メソッドに集約し内部 flag で二重呼び出しを防ぐ |
| `TrainingDeadlineExceeded` 送出 | **廃止**。deadline は設定された正常な停止条件なので `TrainingOutcome.stop_reason` で返す |

## 設計判断（先に決めたこと）

| 論点 | 決定 | 理由 |
| --- | --- | --- |
| `GaussianRegressionTask` の形 | 上位計画の「部分実装 ABC」ではなく、`GaussianBatch` 値オブジェクトを受ける**具象クラス** | 抽象を 1 段減らせる。ドメインは `TrainingData.materialize` で `GaussianBatch` を作るだけでよく、抽象 hook を実装する必要がない。ABC 実装が 3 つという上位計画の条件は満たす |
| AMP の gradient overflow | `outcome` に 5 つ目の Literal `"gradient_overflow"` を足し、group を rewind して再試行（連続 8 回で fatal） | GradScaler の overflow skip は正常動作。MR185 はこれを `FloatingPointError` にして run 全体を FAILED にしていた。4 値のままだと「step していないのに committed」と記録が食い違う。**確認事項 1** |
| 非有限 loss | `outcome="non_finite"` → Trainer が `emergency.pt` を残して失敗 | 発散は再試行しても直らない |
| `latest.pt` と `emergency.pt` の関係 | `latest.pt` は resume 点。`emergency.pt` は post-mortem 専用で resume 対象にしない（`resume_rejection` が role で拒否） | MR185 は失敗時の状態で latest を上書きしており、既知良点を壊していた |
| 例外時の復旧 | Trainer は「例外が group を貫いたら optimizer 状態は信用しない」を無条件に適用し、live 状態を `emergency.pt` へ落として再送出する。`latest.pt` は触らない | `optimizer_state_dirty` は戻り値でしか観測できないので、例外経路は常に保守側へ倒す |
| monitor の一元化 | `BestSelection`（monitor / mode / minimum_delta / best_value / best_epoch / patience_counter）が early stopping と scheduler の両方へ同じ値を供給する | MR185 は `_is_better` に metric 名と mode をハードコードし、scheduler が別 patience で同じ metric を二重監視していた |
| checkpoint のワイヤ | `torch.save` + `weights_only=True` load。`DocumentKind`（JSON/cattrs 前提）は使わず、`kind` / `schema_version` を payload に埋めてキー集合完全一致で検証する | Tensor を cattrs に通せない。エンベロープの考え方だけ踏襲する |
| `CompileOptions` | 既存の `ml.evaluation.compile_parity.CompileOptions` を再利用（新設しない） | 同じ型を 2 つ持たない |
| compile の seam | `TrainingTask.compile_forward(options)` を ABC の**具象 no-op 既定**として置き、compile を使う task が `@override` する。`model` プロパティは常に compile 前の `nn.Module` を返す | Trainer が batch 型を知らずに compile を有効化でき、state_dict に `_orig_mod.` が入る余地がなくなる |
| log-variance offset 較正 | Trainer は行わない。`GaussianPredictions.fit_log_variance_offset()` を呼ぶのは利用側 | checkpoint を不変に保つ。較正はドメインの評価工程 |
| `device` | `TrainerConfig` ではなく `Trainer.__init__` の keyword。fingerprint に含めない | 同じ config を CPU / GPU で resume できる |
| `run_tags` | `Trainer.run()` に持たせない。固定タグは `TaggedExperimentLogger` で被せる | デコレータの存在理由をここに集約する |
| provenance の生 diff | 永続化しない。`GitProvenance.as_tags()` は commit / branch / dirty / `diff_fingerprint` / untracked 件数だけを返す | 生 diff に credential が混ざりうる |

## 公開インターフェース案

すべて `from __future__ import annotations`。attrs は `@attrs.frozen`（Tensor を持つものだけ `eq=False`）。
ABC は `abc.ABC` + `@abc.abstractmethod`、実装側は全メソッドに `@override`。

### `src/ml/experiment/logger.py`（依存フリー層。torch を import しない）

```python
type Scalar = bool | int | float | str
type RunStatus = Literal["FINISHED", "FAILED", "KILLED"]

class ExperimentLogger(abc.ABC):
    @property
    @abc.abstractmethod
    def run_id(self) -> str: ...
    @abc.abstractmethod
    def start(self, *, run_kind: str, run_name: str | None = None,
              tags: Mapping[str, str] | None = None) -> str: ...
    @abc.abstractmethod
    def log_params(self, params: Mapping[str, Scalar]) -> None: ...
    @abc.abstractmethod
    def log_metrics(self, metrics: Mapping[str, float], *, step: int) -> None: ...
    @abc.abstractmethod
    def log_artifact(self, path: Path, *, artifact_path: str | None = None) -> None: ...
    @abc.abstractmethod
    def set_tags(self, tags: Mapping[str, str]) -> None: ...
    @abc.abstractmethod
    def flush(self) -> None: ...
    @abc.abstractmethod
    def end(self, *, status: RunStatus = "FINISHED") -> None: ...

class TaggedExperimentLogger(ExperimentLogger):
    """固定タグを必ず付けて内側の logger へ委譲する."""
    def __init__(self, inner: ExperimentLogger, *, tags: Mapping[str, str]) -> None
    # 全メソッド @override。start() だけ tags を合成し、固定タグを呼び出し側より優先する
    # （provenance タグを呼び出し側が黙って上書きできないようにする）。他は素通し
```

### `src/ml/experiment/provenance.py`（依存フリー層）

```python
TRACKED_PACKAGE_NAMES: tuple[str, ...]   # torch, torchvision, hydra-core, hydra-optuna-sweeper,
                                         # optuna, mlflow, onnx, onnxruntime, onnxscript

@attrs.frozen
class GitProvenance:
    commit: str
    branch: str
    dirty: bool
    diff: str                       # 生 diff。永続化してはならない（as_tags には出ない）
    untracked_files: tuple[str, ...]
    untracked_content: str          # 同上

    @classmethod
    def capture(cls, repository: Path) -> tuple[GitProvenance | None, str | None]
    @property
    def diff_fingerprint(self) -> str          # "sha256:..."（diff + untracked_content）
    def as_tags(self) -> dict[str, str]        # git.commit / git.branch / git.dirty /
                                               # git.diff_fingerprint / git.untracked_file_count

@attrs.frozen
class DependencyVersions:
    versions: Mapping[str, str] = attrs.field(converter=<read-only mapping>)

    @classmethod
    def collect(cls) -> DependencyVersions     # python / platform / TRACKED_PACKAGE_NAMES /
                                               # cuda / cudnn（torch は関数内 import）
    def as_params(self) -> dict[str, str]      # "dependency.torch" 等へ平坦化

def sanitize_persisted_uri(uri: str) -> str    # credential と query / fragment を落とす
def sanitize_persisted_text(value: str) -> str # 自由文中の URI をすべて sanitize
```

`sanitize_persisted_value`（再帰版）は利用者が現れる MR5 まで作らない（AGENTS.md 開発原則 2）。

### `src/ml/experiment/mlflow.py`（`ml-train` 層。`import mlflow` は module-level）

```python
@attrs.frozen
class MLflowRunTarget:
    tracking_uri: str
    experiment_name: str
    run_name: str | None = None
    resume_run_id: str | None = None

    def validate(self) -> str | None
    @property
    def sanitized_tracking_uri(self) -> str    # provenance.sanitize_persisted_uri

class MLflowExperimentLogger(ExperimentLogger):
    def __init__(self, target: MLflowRunTarget) -> None   # validate() が理由を返したら ValueError
    # 全メソッド @override
    #   start: set_tracking_uri → set_experiment → start_run（resume_run_id があれば run_id 指定）
    #   log_metrics: mlflow.log_metrics(..., step=step, synchronous=True)。キュー・retry は持たない
    #   flush: mlflow.flush_async_logging()
    #   end: flush → mlflow.end_run(status=...)。end 後の呼び出しは RuntimeError
```

### `src/ml/training/task.py`（`ml-runtime` 層）

```python
@attrs.frozen(eq=False)
class StepResult[ObservationT]:
    loss: Tensor              # 0 次元・微分可能
    observation: ObservationT
    sample_count: int
    def validate(self) -> str | None

class TrainingTask[BatchT, ObservationT](abc.ABC):
    @property
    @abc.abstractmethod
    def model(self) -> nn.Module: ...
        # 契約: compile_forward の後も常に compile 前の module を返す。
        # state_dict / parameters / train / eval はすべてこの module に対して行う
    @abc.abstractmethod
    def training_step(self, batch: BatchT) -> StepResult[ObservationT]: ...
    @abc.abstractmethod
    def evaluation_step(self, batch: BatchT) -> ObservationT: ...
    @abc.abstractmethod
    def reduce(self, observations: Sequence[ObservationT]) -> Mapping[str, float]: ...
        # 集計できないときは空 Mapping を返す（例外を投げない）
    def compile_forward(self, options: CompileOptions) -> None:
        """forward 経路を torch.compile 済みへ差し替える。既定は何もしない."""

@attrs.frozen(eq=False)
class GaussianBatch:
    images: Tensor                       # [B, C, H, W]
    valid_pixel_mask: Tensor | None      # [B, 1, H, W] bool
    conditioning: Tensor | None          # [B, K]
    target: Tensor                       # [B, 1]
    sample_weight: Tensor                # [B, 1]
    def validate(self) -> str | None

@attrs.frozen(eq=False)
class GaussianObservation:
    mean: Tensor
    log_variance: Tensor
    target: Tensor
    sample_weight: Tensor

class GaussianRegressionTask(TrainingTask[GaussianBatch, GaussianObservation]):
    def __init__(self, model: GaussianImageRegressor) -> None
    @property
    @override
    def model(self) -> nn.Module
    @override
    def training_step(self, batch: GaussianBatch) -> StepResult[GaussianObservation]
        # weighted_gaussian_negative_log_likelihood を使う。observation は detach 済み
    @override
    def evaluation_step(self, batch: GaussianBatch) -> GaussianObservation
    @override
    def reduce(self, observations: Sequence[GaussianObservation]) -> Mapping[str, float]
        # 連結 → GaussianPredictions → GaussianRegressionMetrics.measure。
        # (None, reason) なら空 Mapping。成功時は attrs.asdict のキーをそのまま float で返す
    @override
    def compile_forward(self, options: CompileOptions) -> None
```

### `src/ml/training/data.py`（`ml-runtime` 層）

```python
class TrainingData[BatchT](abc.ABC):
    @property
    @abc.abstractmethod
    def dataset_fingerprint(self) -> str: ...
        # resume 互換性判定に使う。"sha256:..." 形式
    @abc.abstractmethod
    def plan_epoch(self, *, split: SplitName, epoch: int) -> tuple[tuple[str, ...], ...]: ...
        # 同じ split と epoch なら常に同じ計画を返すこと（resume の前提）
    @abc.abstractmethod
    def materialize(self, sample_ids: Sequence[str], *, split: SplitName,
                    epoch: int, training: bool) -> BatchT: ...
```

上位計画では `plan_epoch(self, split, epoch)` が位置引数だが、`materialize` と揃えて keyword-only にした
（`plan_epoch("train", 0)` の取り違えを型で防ぐ）。`dataset_fingerprint` は上位計画にないが、
Trainer がドメインを知らずに「dataset 不一致の resume 拒否」を実現するために必要。

### `src/ml/training/transaction.py`（`ml-runtime` 層）

```python
type OptimizerGroupOutcome = Literal[
    "committed",            # 全 batch 成功 + can_commit 真 + optimizer step 完了
    "incomplete_batches",   # 予定数の batch が来なかった（deadline / signal で打ち切られた）
    "vetoed",               # can_commit() が偽
    "gradient_overflow",    # AMP 有効時に GradScaler が step を skip した
    "non_finite",           # loss が非有限（AMP 無効時の非有限 gradient を含む）
]

@attrs.frozen
class OptimizerGroupResult:
    outcome: OptimizerGroupOutcome
    optimizer_state_dirty: bool     # optimizer / GradScaler の内部状態が変化したか。
                                    # committed と gradient_overflow で True
    processed_batch_count: int
    sample_count: int
    elapsed_seconds: float

    @property
    def committed(self) -> bool

def execute_optimizer_group[BatchT, ObservationT](
    batches: Iterable[BatchT],
    *,
    expected_batch_count: int,
    task: TrainingTask[BatchT, ObservationT],
    optimizer: Optimizer,
    gradient_scaler: torch.GradScaler,
    gradient_clip_norm: float,
    device_type: str,
    autocast_enabled: bool,
    can_commit: Callable[[], bool] | None = None,
) -> tuple[OptimizerGroupResult, tuple[ObservationT, ...]]
    # observations は committed のときだけ非空

def execute_evaluation_batches[BatchT, ObservationT](
    batches: Iterable[BatchT],
    *,
    task: TrainingTask[BatchT, ObservationT],
    deadline_monotonic: float | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> tuple[tuple[ObservationT, ...], str | None]
    # 途中で止めたら理由文字列を返す（例外を投げない）。呼び出し側は部分結果を捨てる
```

順序（`execute_optimizer_group` 内）: `zero_grad` → 各 batch を autocast 下で
`task.training_step` → `StepResult.validate()` 違反は `ValueError` → loss 非有限なら `non_finite` で打ち切り →
`scaler.scale(loss / expected_batch_count).backward()` → 予定数未達 or `can_commit()` 偽なら grad を捨てて
`incomplete_batches` / `vetoed` → `scaler.unscale_` → gradient 有限性判定（AMP 有効なら overflow、無効なら
`non_finite`）→ `clip_grad_norm_` → `scaler.step` → `scaler.update` → `committed`。

### `src/ml/training/checkpoint.py`（`ml-runtime` 層）

```python
CHECKPOINT_KIND = "ml-training-checkpoint"
CHECKPOINT_SCHEMA_VERSION = 1
type CheckpointRole = Literal["latest", "best", "final", "emergency"]

@attrs.frozen
class TrainingProgress:
    epoch: int = 0
    global_step: int = 0
    next_batch_index: int = 0
    epochs_completed: int = 0
    batch_plan: tuple[tuple[str, ...], ...] = ()

    def validate(self) -> str | None
    def with_batch_plan(self, plan: tuple[tuple[str, ...], ...]) -> TrainingProgress
    def with_committed_group(self, next_batch_index: int) -> TrainingProgress   # global_step += 1
    def with_completed_epoch(self) -> TrainingProgress   # epoch += 1, epochs_completed += 1,
                                                         # next_batch_index = 0, batch_plan = ()

@attrs.frozen
class BestSelection:
    monitor: str
    mode: Literal["min", "max"]
    minimum_delta: float
    best_value: float | None = None
    best_epoch: int | None = None
    patience_counter: int = 0

    def validate(self) -> str | None
    def consider(self, value: float, *, epoch: int) -> tuple[BestSelection, bool]
        # 戻り値は (更新後の自分, 改善したか)。純関数

@attrs.frozen(eq=False)
class TrainingCheckpoint:
    role: CheckpointRole
    created_unix_seconds: float
    run_id: str
    progress: TrainingProgress
    selection: BestSelection
    validation_metrics: Mapping[str, float]
    dataset_fingerprint: str
    config_fingerprint: str
    model_state: Mapping[str, Tensor]
    optimizer_state: Mapping[str, object]
    scheduler_state: Mapping[str, object]
    gradient_scaler_state: Mapping[str, object]
    random_state: RandomState

    def validate(self) -> str | None
        # model_state のキーに "_orig_mod." prefix があれば理由を返す
    def resume_rejection(self, *, dataset_fingerprint: str, config_fingerprint: str,
                         run_id: str, model_state_keys: Set[str]) -> str | None
        # role == "emergency" / fingerprint 不一致 / run_id 不一致 /
        # model_state のキー集合不一致 をすべてここで判定する
    def to_payload(self) -> dict[str, object]
    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> tuple[TrainingCheckpoint | None, str | None]
        # kind / schema_version / キー集合完全一致（不足と未知の両方）を検証

class CheckpointStore:
    def __init__(self, directory: Path) -> None
    @property
    def directory(self) -> Path
    def path_for(self, role: CheckpointRole) -> Path      # latest.pt / best.pt / final.pt / emergency.pt
    def exists(self, role: CheckpointRole) -> bool
    def save(self, checkpoint: TrainingCheckpoint) -> Path
        # ml.artifact.atomic.atomic_write_stream に torch.save を渡し、
        # validate_readback で weights_only load → from_payload → validate まで通す
    def load(self, role: CheckpointRole) -> tuple[TrainingCheckpoint | None, str | None]
    def load_path(self, path: Path) -> tuple[TrainingCheckpoint | None, str | None]
```

### `src/ml/training/random_state.py`（`ml-runtime` 層）

```python
def seed_everything(seed: int, *, deterministic: bool) -> None

@attrs.frozen(eq=False)
class RandomState:
    python_state: tuple[object, ...]
    numpy_bit_generator: str
    numpy_keys: Tensor          # np.ndarray[uint32] は weights_only load が読めないので Tensor 化する
    numpy_position: int
    numpy_has_gaussian: bool
    numpy_cached_gaussian: float
    torch_cpu_state: Tensor
    torch_cuda_states: tuple[Tensor, ...]

    @classmethod
    def capture(cls) -> RandomState
    def restore(self) -> tuple[bool, str | None]   # CPU 専用ホストへ CUDA state を戻せない等は理由を返す
    def to_payload(self) -> dict[str, object]
    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> tuple[RandomState | None, str | None]
```

### `src/ml/training/loop.py`（`ml-runtime` 層）

```python
type StopReason = Literal["max_epochs", "max_steps", "early_stopping", "deadline", "signal"]

class NonFiniteLossError(RuntimeError): ...

class TerminationSignals(AbstractContextManager["TerminationSignals"]):
    """SIGINT / SIGTERM を flag へ落とし、group 境界で処理させる."""
    def __init__(self) -> None
    @property
    def requested(self) -> bool
    @override
    def __enter__(self) -> TerminationSignals
    @override
    def __exit__(self, *exc_info: object) -> None
    # main thread 以外では handler を張らず requested は常に False

@attrs.frozen
class TrainerConfig:
    max_epochs: int                                  # 既定なし
    monitor: str                                     # 既定なし。validation の reduce キー（prefix なし）
    mode: Literal["min", "max"] = "min"
    max_steps: int | None = None
    learning_rate: float = 1e-3
    weight_decay: float = 1e-4
    gradient_accumulation: int = 1
    gradient_clip_norm: float = 1.0
    early_stopping_patience: int = 15
    early_stopping_minimum_delta: float = 1e-4
    scheduler_factor: float = 0.5
    scheduler_patience: int = 5
    automatic_mixed_precision_enabled: bool = False
    compile_enabled: bool = False
    compile_options: CompileOptions = CompileOptions()
    deadline_seconds: float | None = None
    finalization_grace_seconds: float = 300.0
    checkpoint_interval_steps: int = 100
    checkpoint_interval_seconds: float = 600.0
    deterministic: bool = True
    seed: int = 0

    def validate(self) -> str | None
    @property
    def fingerprint(self) -> str          # ml.artifact.fingerprint.fingerprint_json(attrs.asdict(self))

@attrs.frozen
class TrainingOutcome:
    stop_reason: StopReason
    epochs_completed: int
    global_step: int
    best_monitor_value: float | None
    best_epoch: int | None
    best_checkpoint_path: Path | None
    final_checkpoint_path: Path | None
    last_validation_metrics: Mapping[str, float]
    elapsed_seconds: float

class Trainer[BatchT, ObservationT]:
    def __init__(self, task: TrainingTask[BatchT, ObservationT],
                 data: TrainingData[BatchT], *, config: TrainerConfig,
                 store: CheckpointStore, logger: ExperimentLogger,
                 device: torch.device | None = None) -> None
        # config.validate() が理由を返したら ValueError。device=None は cuda があれば cuda、なければ cpu
    def run(self, *, resume_from: Path | None = None,
            run_kind: str = "training", run_name: str | None = None) -> TrainingOutcome
```

`Trainer.run()` の制御順序（実装者はこの順序を変えないこと）:

1. `seed_everything(config.seed, deterministic=config.deterministic)`
2. `task.model.to(device)`、`AdamW`（`requires_grad` のみ）、`ReduceLROnPlateau(mode=config.mode, factor, patience)`、`GradScaler(device.type, enabled=amp)` を構築
3. `resume_from` があれば `store.load_path` → `resume_rejection(...)` が理由を返したら `ValueError` →
    model / optimizer / scheduler / scaler / `RandomState` / `progress` / `selection` を復元
4. `run_id = logger.start(run_kind=..., run_name=...)`。resume かつ `run_id != checkpoint.run_id` なら
    `_end_run("FAILED")` してから `ValueError`
5. `logger.log_params`（`TrainerConfig` の全フィールド + `config_fingerprint` + `dataset_fingerprint`）
6. `deadline_monotonic = start + deadline_seconds`、`finalization_deadline = deadline + finalization_grace_seconds`
7. deadline 未到達かつ `compile_enabled` なら `task.compile_forward(config.compile_options)`
8. resume でなければ、最初の optimizer step の前に `latest.pt` を 1 本保存する
9. epoch ループ（`progress.epoch < max_epochs`）
    - `plan = data.plan_epoch(split="train", epoch=progress.epoch)`。`progress.batch_plan` が非空
        （epoch 途中からの resume）なら一致を要求し、違えば `ValueError`。空なら `with_batch_plan(plan)`
    - `task.model.train()`。`progress.next_batch_index` から group 単位で回す
        - group 先頭で停止判定（signal → `"signal"` / deadline → `"deadline"` / `max_steps` → `"max_steps"`）。
            group の途中では判定しない
        - `RandomState.capture()` と group 先頭 index を控える
        - `execute_optimizer_group(..., can_commit=lambda: not signal and not deadline)`
        - `committed` → `progress = progress.with_committed_group(index)`、observations を保持
        - `incomplete_batches` / `vetoed` → RNG と index を group 先頭へ戻し、対応する stop_reason で break
        - `gradient_overflow` → RNG と index を group 先頭へ戻して同じ group を再試行。
            連続 `_MAXIMUM_CONSECUTIVE_GRADIENT_OVERFLOWS`（= 8、module 定数）を超えたら `non_finite` 扱い
        - `non_finite` → `emergency.pt` 保存 → failure タグ → `_end_run("FAILED")` → `NonFiniteLossError`
        - `checkpoint_interval_steps` / `checkpoint_interval_seconds` を満たしたら `latest.pt`
    - epoch が中断された（signal / deadline / max_steps）→ **validation を実行せず** `latest.pt` を保存して
        epoch ループを抜ける
    - epoch が完走した → `task.model.eval()` → `execute_evaluation_batches` で validation →
        理由が返ったら部分結果を捨てて中断扱い → `metrics = task.reduce(observations)` →
        `config.monitor` が `metrics` に無ければ `ValueError`
    - `scheduler.step(metrics[monitor])` → `selection, improved = selection.consider(value, epoch=progress.epoch)` →
        improved なら `best.pt`（この時点の `progress.epoch` が best epoch）→ `progress.with_completed_epoch()`
    - `logger.log_metrics({"train/...", "validation/...", "learning_rate", "epoch_seconds",
        "train_samples_per_second"}, step=progress.global_step)`（step 軸は常に `global_step`）
    - `latest.pt` を保存 → `patience_counter >= early_stopping_patience` なら `"early_stopping"` で break
10. finalization（`finalization_deadline` を超えたら打ち切って `latest.pt` のまま終了）:
    `best.pt` があれば `task.model.load_state_dict(best.model_state)` → `final.pt` を保存 →
    checkpoint を `logger.log_artifact` → `_end_run("FINISHED")`
11. `stop_reason` が `"signal"` なら `_end_run("KILLED")`。`"deadline"` は正常終了として `"FINISHED"`
12. 例外が貫いたら: live 状態から `emergency.pt`（`progress` は group 先頭へ巻き戻す。捕捉自体が失敗しても
    握り潰さずタグに残す）→ failure タグ → `_end_run("FAILED")` → 再送出。`latest.pt` は触らない
13. `_end_run(status)` は内部 flag で 1 度しか `logger.end` を呼ばない

### `tests/ml/support.py`（ドメインを一切 import しない。`tests.helpers` にも依存しない）

```python
@attrs.frozen
class SyntheticDatasetOptions:
    sample_count: int = 12
    image_size: int = 8
    seed: int = 0

@attrs.frozen
class SyntheticTaskOptions:
    seed: int = 0
    non_finite_at_step: int | None = None    # 指定 step の training_step が NaN loss を返す

def build_synthetic_model(*, seed: int) -> GaussianImageRegressor
    # ImageEncoderConfig(input_channels=3, stem_channels=(8,), stem_strides=(2,),
    #   stage_channels=(8,), stage_strides=(1,), blocks_per_stage=(1,), group_norm_groups=4)
    # GaussianHeadConfig(input_features=8, hidden_features=8)

class SyntheticRegressionTask(GaussianRegressionTask):
    def __init__(self, model: GaussianImageRegressor, options: SyntheticTaskOptions) -> None
    @override
    def training_step(self, batch: GaussianBatch) -> StepResult[GaussianObservation]

class SyntheticRegressionData(TrainingData[GaussianBatch]):
    def __init__(self, options: SyntheticDatasetOptions) -> None
    # target は画像から決定論的に決まる正のスカラー（例: 1.0 + 平均輝度）。
    # plan_epoch は ml.data.batch.plan_pixel_budget_batches、
    # materialize は PaddedBatch.pad → GaussianBatch
    @property
    @override
    def dataset_fingerprint(self) -> str
    @override
    def plan_epoch(self, *, split: SplitName, epoch: int) -> tuple[tuple[str, ...], ...]
    @override
    def materialize(self, sample_ids, *, split, epoch, training) -> GaussianBatch

class RecordingExperimentLogger(ExperimentLogger):
    """全メソッド @override の in-memory recorder（MR185 の NullExperimentLogger 置き換え）."""
    # params / metrics: list[tuple[int, dict[str, float]]] / tags / artifacts / status / end_call_count
    # を公開属性として観測できる
```

## 実装ステップ（依存順）

1. `src/ml/experiment/__init__.py` と `src/ml/training/__init__.py`（docstring のみ。`ml/data/__init__.py` の書式に揃える）、
    `tests/ml/experiment/__init__.py`、`tests/ml/training/__init__.py`
2. `ml/experiment/logger.py`（依存なし）
3. `ml/experiment/provenance.py`（依存なし）
4. `ml/experiment/mlflow.py`（2、3 に依存）
5. `ml/training/random_state.py`（依存なし）
6. `ml/training/task.py`（`ml.model.*` に依存）
7. `ml/training/data.py`（`ml.data.split` に依存）
8. `ml/training/transaction.py`（6 に依存）
9. `ml/training/checkpoint.py`（5、6 に依存）
10. `ml/training/loop.py`（2、6〜9 に依存）
11. `tests/ml/test_architecture.py` を更新:
    `DEPENDENCY_FREE_MODULES` に `ml.experiment.logger` / `ml.experiment.provenance`、
    `RUNTIME_MODULES` に `ml.training.checkpoint` / `ml.training.data` / `ml.training.loop` /
    `ml.training.random_state` / `ml.training.task` / `ml.training.transaction`
12. `make format && make type && make test-no-hardware`

## モジュールごとの担当分割（`spec-test-author` × `plan-implementer` の並列）

`spec-test-author` は `tests/ml/` のみ、`plan-implementer` は `src/ml/` のみを編集する。
同一ファイルの同時編集は発生しない。着手前に上記「公開インターフェース案」をシグネチャの正典として共有する。

| グループ | `plan-implementer`（`src/ml/`） | `spec-test-author`（`tests/ml/`） |
| --- | --- | --- |
| 0（先行） | 手順 1 の `__init__.py` 2 本 | `tests/ml/support.py`、`tests/ml/{experiment,training}/__init__.py` |
| A | `experiment/logger.py`、`experiment/provenance.py`、`experiment/mlflow.py` | `experiment/test_logger.py`、`test_provenance.py`、`test_mlflow.py` |
| B | `training/random_state.py`、`training/task.py`、`training/data.py` | `training/test_random_state.py`、`test_task.py`、`test_data.py` |
| C | `training/transaction.py`、`training/checkpoint.py` | `training/test_transaction.py`、`test_checkpoint.py` |
| D | `training/loop.py` | `training/test_loop.py`、`test_architecture.py` の更新 |

- グループ 0 を最初に片付ける。`tests/ml/support.py` は B / C / D のテストすべてが import するので、
    src が未着手でも先に書き切る（シグネチャは本計画で確定済み）
- A は B / C / D と完全独立。並列開始してよい
- B → C → D は src 側に依存順があるが、**テストは 4 グループ同時に書ける**（spec first）
- `state_dict` キー契約のピン（下記）は `tests/ml/training/test_checkpoint.py` に置く。
    `tests/ml/model/` の既存ファイルには触らない

## テスト観点

配置は `tests/ml/experiment/test_{logger,mlflow,provenance}.py`、
`tests/ml/training/test_{task,data,transaction,checkpoint,random_state,loop}.py`。
すべて `class TestXxx` に集約。private（`_` prefix）は直接テストしない。
Pi CI の 30 分ジョブ / 180 秒 per-test timeout を守るため、画像は **8×8**、sample 12 件、
epoch は **2**、encoder は上記 tiny config に固定する。`Trainer` のテストは
`RecordingExperimentLogger` を使い、MLflow を起動しない。

### 正常系

- `ExperimentLogger`: `RecordingExperimentLogger` が ABC を満たす（`abc` の抽象メソッド未実装検出を含む）
- `TaggedExperimentLogger`: `start()` で固定タグが必ず入る / 同じキーを呼び出し側が渡しても固定タグが勝つ /
    `log_params` `log_metrics` `log_artifact` `set_tags` `flush` `end` が素通しで内側へ届く
- `GitProvenance.capture`: 実 git repo（`tmp_path` に `git init` + 1 commit）で commit / branch / dirty が取れる /
    ファイルを変更すると `dirty=True` と `diff_fingerprint` が変わる /
    `as_tags()` に生 diff と untracked 本文が **含まれない**
- `DependencyVersions.collect()`: `torch` の版が `importlib.metadata` と一致 / 未 install の名前は "not-installed"
- `sanitize_persisted_uri`: `postgresql://user:secret@host:5432/db?sslmode=require` →
    `postgresql://host:5432/db` / query と fragment が消える / scheme なしの文字列はそのまま
- `sanitize_persisted_text`: 自由文中の複数 URI をすべて sanitize する
- `MLflowExperimentLogger`（実 local server、session fixture）: run 作成 → params / metrics / tags / artifact 記録 →
    `end("FINISHED")` の一連を `MlflowClient` で読み戻して照合 / `resume_run_id` で同じ run へ追記できる
- `StepResult.validate()`: 0 次元 loss + 正の `sample_count` で `None`
- `GaussianRegressionTask`: `training_step` の loss が 0 次元・`requires_grad=True` /
    `reduce()` のキーが `GaussianRegressionMetrics` のフィールド名と一致 /
    `evaluation_step` が勾配を作らない
- `TrainingData`: 合成 data の `plan_epoch` が同じ epoch で 2 回呼んでも同一 / epoch を変えると並びが変わる /
    全 sample がちょうど 1 回現れる
- `execute_optimizer_group`: 全 batch 成功で `outcome="committed"`、`optimizer_state_dirty=True`、
    `processed_batch_count == expected_batch_count`、observations が batch 数ぶん返る / パラメータが実際に更新される
- `execute_evaluation_batches`: 全 batch 分の observation が返り、理由は `None` / 勾配が作られない
- `RandomState`: `capture()` → 乱数を消費 → `restore()` で `random` / `numpy` / `torch` の次の値が一致
- `TrainingProgress` / `BestSelection`: `with_committed_group` / `with_completed_epoch` /
    `consider` が期待どおりの新しい値を返し、元のオブジェクトを変えない /
    `mode="max"` で大きい値が改善と判定される / `minimum_delta` 未満の改善は非改善で `patience_counter` が増える
- `CheckpointStore`: `save` → `load` の往復で全フィールドが一致（Tensor は `torch.equal`）/
    `latest.pt` `best.pt` `final.pt` `emergency.pt` のファイル名 / 同じ role の上書き保存
- **Trainer 通し**: 2 epoch 走り切って `stop_reason="max_epochs"`、`best.pt` と `final.pt` が存在し、
    `final.pt` の `model_state` が `best.pt` と一致 / logger に epoch ごとの metrics が
    `step=global_step` で 1 回ずつ入り、`end` が **1 回だけ** `"FINISHED"` で呼ばれる
- **中断あり / なしの一致**: 同じ seed で (a) 通しで 2 epoch、(b) 1 epoch 目の途中で deadline に当てて停止し
    `latest.pt` から resume して 2 epoch、の最終 `model.state_dict()` の全 Tensor が
    `torch.allclose`（厳密一致を狙うが、許容は `atol=0` で開始し必要なら理由つきで緩める）で一致し、
    最終 validation metrics も一致する
- **`latest.pt` から未処理 batch を再開**: 1 epoch 目の途中で止めた `latest.pt` の
    `progress.next_batch_index` が 0 でも `len(batch_plan)` でもなく、resume 後に処理された
    train sample 数の合計が通し実行と一致する（同じ batch を二重に処理しない）
- **deadline 到達時に正常終了 checkpoint**: `deadline_seconds` を極小にすると
    `stop_reason="deadline"`、`latest.pt` が存在し `from_payload` で読める、
    `logger.end` は `"FINISHED"`、例外は送出されない
- **SIGTERM が optimizer step 境界で止まる**: 学習中に `os.kill(os.getpid(), signal.SIGTERM)` を送ると
    `stop_reason="signal"`、`logger.end("KILLED")`、`latest.pt` の `next_batch_index` が
    gradient accumulation 境界（`gradient_accumulation` の倍数）に載っている
- **compiled wrapper の `_orig_mod.` prefix を checkpoint に残さない**:
    `CompileOptions(backend="eager")` で `task.compile_forward()` した後に 1 epoch 回し、
    `latest.pt` の `model_state` のキー集合が compile しない場合と完全一致する
- **`state_dict` キー契約のピン**: tiny config の `GaussianImageRegressor.state_dict()` の
    ソート済みキー列を literal で固定する（`_encoder._padding_pixel` を含む）

### 異常系

- `MLflowRunTarget.validate()`: `tracking_uri` が空 / `experiment_name` が空 → 理由文字列
- `MLflowExperimentLogger`: `start()` 前の `run_id` / `log_params` → `RuntimeError` /
    `start()` 二重呼び出し → `RuntimeError` / `end()` 後の操作 → `RuntimeError` /
    存在しないファイルの `log_artifact` → `FileNotFoundError`
- `GitProvenance.capture`: git repo でないディレクトリ → `(None, 理由)`
- `StepResult.validate()`: loss が 0 次元でない / `sample_count <= 0` → 理由文字列。
    Trainer 経由で `ValueError` になる
- `GaussianBatch.validate()`: shape 不一致 / mask の dtype が bool でない → 理由文字列
- `TrainerConfig.validate()`: `max_epochs <= 0` / `gradient_accumulation <= 0` /
    `gradient_clip_norm <= 0` / `early_stopping_patience < 0` / `monitor` が空 /
    `finalization_grace_seconds < 0` → 理由文字列
- `Trainer.__init__`: `config.validate()` が理由を返す → `ValueError`
- **dataset 不一致の resume 拒否**: `dataset_fingerprint` が違う `TrainingData` で resume → `ValueError`
- **config 不一致の resume 拒否**: `TrainerConfig` を 1 フィールド変えて resume → `ValueError`
- **run_id 不一致の resume 拒否**: logger が別 run_id を返す → `ValueError`、かつ `end("FAILED")` が 1 回
- **`emergency.pt` からの resume 拒否**: `resume_rejection` が role の理由を返す
- **model_state キー集合不一致の resume 拒否**: checkpoint に無いキーを持つ model で resume → `ValueError`
    （`load_state_dict` の生の例外ではなく、理由つきで拒否されること）
- **batch_plan 不一致の resume 拒否**: epoch 途中の `latest.pt` を、別の `plan_epoch` を返す data で
    resume → `ValueError`
- **非有限 loss で緊急 checkpoint を残して失敗**: `SyntheticTaskOptions(non_finite_at_step=N)` で
    `NonFiniteLossError` が送出され、`emergency.pt` が存在して `from_payload` で読め、
    `latest.pt` は非有限化の前の状態のまま、`logger.end` が `"FAILED"` で 1 回だけ呼ばれ、
    failure タグが記録されている
- `execute_optimizer_group`: `expected_batch_count <= 0` / `gradient_clip_norm <= 0` → `ValueError` /
    batch が予定数に満たない → `outcome="incomplete_batches"`、パラメータが変化しない /
    `can_commit` が偽 → `outcome="vetoed"`、パラメータが変化しない /
    AMP 無効で非有限 gradient → `outcome="non_finite"`、パラメータが変化しない
- `execute_evaluation_batches`: `stop_requested` が真 → 部分 observations と理由 /
    `deadline_monotonic` 到達 → 同上
- `TrainingCheckpoint.from_payload`: `kind` 違い / `schema_version` 違い / キー不足 / 未知キー → 理由文字列
- `CheckpointStore.load`: ファイルなし → `(None, 理由)` / 壊れたファイル → `(None, 理由)`
- `TrainingCheckpoint.validate()`: `model_state` に `_orig_mod.` prefix のキー → 理由文字列
- `RandomState.restore()`: CUDA state を持つ payload を CPU 専用ホストで復元 → `(False, 理由)`
- `Trainer`: `task.reduce()` が `config.monitor` を含まない Mapping を返す → `ValueError`

### エッジケース

- `gradient_accumulation` が epoch の batch 数を割り切らない（最後の group が短い）
- batch が 1 個だけの epoch（`gradient_accumulation` より少ない）
- `max_steps` が 1 epoch 未満で先に到達 → `stop_reason="max_steps"`、次 epoch へ進まない
- `early_stopping_patience=0` で 1 epoch 目の後に即停止
- 全 epoch で改善しない（`best.pt` は 1 epoch 目のものだけ）
- validation の observation が 0 件 → `reduce()` が空 Mapping → `ValueError`（monitor 不在）
- deadline が最初の optimizer step より前に到達 → `best.pt` なし、`stop_reason="deadline"`、
    `TrainingOutcome.best_checkpoint_path is None`、例外なし
- `deadline_seconds=None` のとき deadline 判定が一切起きない
- `TerminationSignals` を非 main thread で使っても例外にならず `requested` が False のまま
- `CheckpointStore.save` の書き込み途中で中断しても、既存の `latest.pt` が壊れない
    （`atomic_write_stream` の readback validator が理由を投げるケースで検証）

### MLflow テストの隔離

- `tests/ml/experiment/test_mlflow.py` に閉じた **session scope fixture** で
    `[sys.executable, "-m", "mlflow", "server", "--host", "127.0.0.1", "--port", <空きポート>,
    "--backend-store-uri", f"sqlite:///{tmp}/mlflow.db"]` を 1 度だけ起動する
- `/health` を polling して起動を待つ（上限 60 秒）。立たなければ `pytest.skip`
- teardown は `terminate()` → `wait(timeout)` → `kill()`
- mlflow 未 install 環境で collection が止まらないよう、`tests/helpers.py` の
    `skip_if_no_inductor` と同じ capability probe 形式（`functools.cache` 付き）で skip 判定する

## `state_dict()` のキーが private 名を含む問題（MR4 で扱う）

**結論: MR4 で扱う。ただし正規化層は置かず、「契約として固定し、破ったら落ちる」形にする。**

- `ImageEncoder._padding_pixel` などの `_` prefix は MR3 のカプセル化規約どおりで、これ自体は正しい。
    問題は `state_dict()` のキーが内部属性名から機械生成されるため、内部リネームが
    checkpoint と（MR6 の）ONNX の互換性を黙って壊すこと
- **MR4 で扱う根拠**: checkpoint が初めて存在するのが MR4 であり、契約に実効性が生まれるのも MR4。
    MR5 へ送ると、すでに書かれた checkpoint に対して後から互換性の話を retrofit することになる
- **やること（3 点、いずれも既存コードの構造を変えない）**
    1. `tests/ml/training/test_checkpoint.py` に、tiny config の
        `GaussianImageRegressor.state_dict()` のソート済みキー列を literal でピンするテストを 1 本置く。
        リネームすると「checkpoint 互換性が壊れる」と明示するメッセージで落ちる
        （testing-strategy の「公開 API 契約ピン」例外に該当する）
    2. `TrainingCheckpoint.resume_rejection()` が、現在の model の `state_dict()` キー集合と
        checkpoint 側のキー集合を突き合わせ、不一致なら理由文字列で拒否する。
        `load_state_dict` の生の `RuntimeError` ではなく、MR4 の通常の `str | None` 経路に載せる
    3. `TrainingTask.model` の docstring に「この module の `state_dict()` キーは checkpoint と
        export の公開契約であり、内部属性のリネームは互換性を壊す」と明記する
- **採らなかった案**: キー正規化 / renaming map を置く案。リネームを不可視にして移行表という新しい保守面を
    増やすうえ、現時点で移行すべき既存 checkpoint が 1 つもない（AGENTS.md 開発原則 2）
- **採らなかった案**: `_padding_pixel` を `padding_pixel` へ public 化する案。
    MR3 のカプセル化規約に反し、構造的には何も解決しない（次のリネームで同じ問題が起きる）
- MR5 / MR6 に追加の仕組みは不要。MR6 の ONNX export は同じピンテストの対象を広げるだけでよい

## 想定リスク・トレードオフ

- **`outcome` の Literal を 4 → 5 に増やすこと。** 承認済み計画は
    `committed / incomplete_batches / vetoed / non_finite` の 4 値。`gradient_overflow` を足さないと、
    AMP 有効時に GradScaler が step を skip したケースを「committed」と記録するか、
    MR185 のように run 全体を FAILED にするかの二択になる。前者は global_step が実際の更新回数と
    食い違い、後者は AMP の正常動作を障害として扱う。**確認事項 1**
- **`GaussianRegressionTask` を具象にしたこと。** 上位計画の「部分実装」案は、ドメインが
    `inputs_of(batch)` のような抽象 hook を実装する形になる。具象案はドメインが `GaussianBatch` を
    materialize するだけで済み、抽象が 1 段減る。ただしドメイン batch が追加の情報を運びたい場合は
    `TrainingData` 側で保持することになる。**確認事項 3**
- **`mlflow.flush_async_logging()` の存在確認。** `log_metrics` を `synchronous=True` にするので
    `flush()` は主に artifact / param の async 経路向け。mlflow 3.15 で API 名が違う場合は
    実装者が確認して合わせる（`_metric_queue` 相当の自前キューは絶対に作らない）
- **attrs の PEP 695 ジェネリクス。** `@attrs.frozen class StepResult[ObservationT]` が
    slots + generic の組み合わせで pyright / 実行時ともに通ることを、実装の最初に 1 本の
    テストで確認する。通らない場合は `Generic[ObservationT]` 継承へ落とす
- **`torch.load(weights_only=True)` が読める型。** `random.getstate()` の戻り（`tuple[int, tuple[int,...], float|None]`）が
    そのまま往復することは MR185 で実績があるが、`RandomState.to_payload` を書いた直後に往復テストで確認する。
    numpy の key 配列は `np.ndarray` のままでは読めないので Tensor 化が必須
- **中断あり / なしの weight 一致テストの厳密さ。** CPU / `deterministic=True` なら bit 一致するはずだが、
    `torch.use_deterministic_algorithms(True)` でも一部の op は非決定になりうる。まず `atol=0` で書き、
    落ちたら「なぜ緩めるか」をコメントに残して最小限だけ緩める。閾値を実測値へ張り付けない
    （MR3 のレビュー指摘の再発防止）
- **MLflow server の起動コスト。** session fixture でも初回テストが 10〜30 秒を負担する。
    CI の per-test timeout は 180 秒なので収まる想定だが、不安定なら
    `sqlite:///` の file store 1 本に落とす。**確認事項 2**
- **`ml.training.task` が `ml.evaluation.compile_parity` を import すること。** `CompileOptions` の
    再利用のため。層としては training → evaluation の向きになるが、両方 `ml-runtime` 層で循環もない。
    `CompileOptions` を `ml/training/` へ移す案は MR3 の公開インターフェースを変えるので採らない
- **`docformatter` が日本語の複数文段落を壊す。** 説明部は 1 文ごとに空行で区切る
    （`~/.claude/projects/-home-gop-pcb-assembly/memory/docformatter-japanese-docstrings.md`）。summary を小文字の識別子で始めない
- **`tests/ml/support.py` が `--doctest-modules` で collection される。** doctest として解釈される
    `>>>` を docstring に書かない

## 確認事項

1. **`OptimizerGroupResult.outcome` に 5 つ目の `"gradient_overflow"` を足してよいか。**
    暫定案は追加する（AMP の GradScaler skip を group の rewind + 再試行で扱い、連続 8 回で fatal）。
    承認済み計画の 4 値のままにするなら、AMP 有効時の step skip を記録上どう扱うか（committed 扱いにするか、
    MR185 どおり run を FAILED にするか）を決める必要がある
2. **MLflow の実 local server テストを CI で常時走らせるか。** 暫定案は capability probe 付きで常時実行。
    起動が 10〜30 秒かかり、CI の per-test timeout は 180 秒。不安定なら server を立てず
    `sqlite:///` の file store に落とす（MLflow の実表面であることは変わらず、モックにはならない）
3. **`GaussianRegressionTask` を「部分実装 ABC」ではなく `GaussianBatch` を受ける具象クラスにしてよいか。**
    暫定案は具象（抽象が 1 段減る）。上位計画の文言どおり部分実装 ABC にするなら、
    ドメインが実装する抽象 hook のシグネチャをここで確定させる必要がある

## 参照

- 全体計画: `/home/gop/.claude/plans/mr185-codex-docs-image-based-dispense-ca-modular-sonnet.md`（「MR4」節）
- 参照実装: `git show origin/feature/2026-09-01/paste-volume-ml:src/ml/training/loop.py` /
    `checkpoint.py` / `random_state.py` / `experiment.py` / `cli/provenance.py` /
    `paste_volume/training.py`（`train_model()` は 581-1552 行）
- 既存コード: `src/ml/artifact/atomic.py`（`atomic_write_stream`）、
    `src/ml/artifact/fingerprint.py`（`fingerprint_json`）、`src/ml/data/split.py`（`SplitName`）、
    `src/ml/data/batch.py`（`plan_pixel_budget_batches` / `PaddedBatch.pad`）、
    `src/ml/model/heads.py`（`GaussianImageRegressor`）、`src/ml/model/loss.py`、
    `src/ml/evaluation/regression.py`（`GaussianPredictions` / `GaussianRegressionMetrics`）、
    `src/ml/evaluation/compile_parity.py`（`CompileOptions`）、`tests/ml/test_architecture.py`
- 規約: `memory/feedback_validation_method.md`、`memory/feedback_no_try_catch.md`、
    `memory/feedback_test_class.md`、`memory/feedback_no_private_test.md`、
    `~/.claude/projects/-home-gop-pcb-assembly/memory/docformatter-japanese-docstrings.md`、`~/.claude/projects/-home-gop-pcb-assembly/memory/ml-core-foundation-mrs.md`
- Skill: `refactor-conventions`、`testing-strategy`、`agent-team-startup`
