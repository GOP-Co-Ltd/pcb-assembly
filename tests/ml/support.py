"""``ml.training`` と ``ml.experiment`` のテストが共有する合成タスク一式.

装置ドメイン (``pcbasm``) を一切 import せず、``ml`` の公開インターフェースだけで
``Trainer.run()`` を end-to-end に回せる最小の task / data / logger を提供する。

規模は Raspberry Pi 5 の CI に収まるよう 8x8 画像・12 sample・tiny encoder に固定する。

``--doctest-modules`` で collect されるため、doctest として解釈される記法は書かない。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import override

import attrs
import torch
from torch import Tensor

from ml.artifact.fingerprint import fingerprint_json
from ml.data.batch import BatchShape, PaddedBatch, plan_pixel_budget_batches
from ml.data.split import SplitName
from ml.experiment.logger import ExperimentLogger, RunStatus, Scalar
from ml.model.blocks import ImageEncoder, ImageEncoderConfig
from ml.model.heads import (
    GaussianHeadConfig,
    GaussianImageRegressor,
    GaussianRegressionHead,
)
from ml.training.data import TrainingData
from ml.training.task import (
    GaussianBatch,
    GaussianObservation,
    GaussianRegressionTask,
    StepResult,
)

# 8x8 画像をそのまま扱えるよう padding stride を 8 にそろえる
BATCH_STRIDE = 8

# 1 batch 3 sample。gradient accumulation の境界テストで割り切れない組み合わせを作れる幅
MAX_BATCH_SIZE = 3

IMAGE_CHANNELS = 3

# 学習 step で global RNG を消費する augmentation ノイズの大きさ。
#
# これが無いと合成 task は global RNG を一切消費せず、RNG snapshot と
# ``RandomState.restore`` が壊れていても中断 / resume の一致テストが緑のままになる。
TRAINING_NOISE_SCALE = 0.05

ENCODER_CONFIG = ImageEncoderConfig(
    input_channels=IMAGE_CHANNELS,
    stem_channels=(8,),
    stem_strides=(2,),
    stage_channels=(8,),
    stage_strides=(1,),
    blocks_per_stage=(1,),
    group_norm_groups=4,
)

HEAD_CONFIG = GaussianHeadConfig(
    input_features=ENCODER_CONFIG.output_features,
    hidden_features=8,
)


@attrs.frozen
class SyntheticDatasetOptions:
    """合成 dataset の規模と乱数種."""

    sample_count: int = 12
    image_size: int = 8
    seed: int = 0


@attrs.frozen
class SyntheticTaskOptions:
    """合成 task の乱数種と、非有限 loss を注入する位置.

    ``non_finite_at_step`` は :meth:`SyntheticRegressionTask.training_step` の
    呼び出し回数 (0 起点) を指し、その呼び出しだけ NaN の loss を返す。
    """

    seed: int = 0
    non_finite_at_step: int | None = None


def build_synthetic_model(*, seed: int) -> GaussianImageRegressor:
    """テスト用の tiny な Gaussian 回帰 model を決定論的に組む."""

    torch.manual_seed(seed)
    encoder = ImageEncoder(ENCODER_CONFIG)
    head = GaussianRegressionHead(HEAD_CONFIG)
    return GaussianImageRegressor(encoder, head)


class SyntheticRegressionTask(GaussianRegressionTask):
    """合成回帰 task。指定した step だけ非有限 loss を返せる."""

    def __init__(
        self, model: GaussianImageRegressor, options: SyntheticTaskOptions
    ) -> None:
        super().__init__(model)
        self._options = options
        self._training_step_count = 0

    @property
    def training_step_count(self) -> int:
        """これまでに :meth:`training_step` が呼ばれた回数."""

        return self._training_step_count

    @override
    def training_step(self, batch: GaussianBatch) -> StepResult[GaussianObservation]:
        """Global RNG を消費するノイズを載せて学習 step を行う.

        指定 step でだけ NaN loss を返す。

        ノイズは ``torch.randn_like`` なので global RNG の状態が結果へ効く。

        RNG snapshot / rewind / ``RandomState.restore`` が壊れると、
        中断 / resume の一致テストが実際に落ちるようにするための仕掛け。
        """

        index = self._training_step_count
        self._training_step_count += 1
        noisy = attrs.evolve(
            batch,
            images=batch.images + TRAINING_NOISE_SCALE * torch.randn_like(batch.images),
        )
        result = super().training_step(noisy)
        if self._options.non_finite_at_step != index:
            return result
        # 微分可能なまま非有限にする。定数を掛けるので grad_fn は保たれる
        return attrs.evolve(result, loss=result.loss * float("nan"))


class SyntheticRegressionData(TrainingData[GaussianBatch]):
    """画像から決定論的に決まる正の target を返す合成 dataset.

    validation split には末尾の数 sample を割り当て、残りを train にする。

    test split は使わないので空にする。
    """

    def __init__(self, options: SyntheticDatasetOptions) -> None:
        self._options = options
        self._sample_ids = tuple(
            f"sample-{index:03d}" for index in range(options.sample_count)
        )
        validation_count = max(1, options.sample_count // 4)
        self._splits: dict[SplitName, tuple[str, ...]] = {
            "train": self._sample_ids[: options.sample_count - validation_count],
            "validation": self._sample_ids[options.sample_count - validation_count :],
            "test": (),
        }

    @property
    def options(self) -> SyntheticDatasetOptions:
        """この dataset を組み立てた設定."""

        return self._options

    def sample_ids_for(self, split: SplitName) -> tuple[str, ...]:
        """指定 split に属する sample ID を返す."""

        return self._splits[split]

    def target_for(self, sample_id: str) -> float:
        """1 sample の正の target を返す."""

        return 1.0 + float(self._image_for(sample_id).mean().item())

    @property
    @override
    def dataset_fingerprint(self) -> str:
        """規模と乱数種から決まる dataset fingerprint."""

        return fingerprint_json(attrs.asdict(self._options))

    @override
    def plan_epoch(
        self, *, split: SplitName, epoch: int
    ) -> tuple[tuple[str, ...], ...]:
        """1 epoch 分の batch 計画を決定論的に返す."""

        shapes = [
            BatchShape(
                sample_id=sample_id,
                height=self._options.image_size,
                width=self._options.image_size,
            )
            for sample_id in self._splits[split]
        ]
        if not shapes:
            return ()
        padded = _ceil_to(self._options.image_size, BATCH_STRIDE)
        return plan_pixel_budget_batches(
            shapes,
            max_batch_pixels=MAX_BATCH_SIZE * padded * padded,
            max_batch_size=MAX_BATCH_SIZE,
            stride=BATCH_STRIDE,
            seed=self._options.seed,
            epoch=epoch,
        )

    @override
    def materialize(
        self,
        sample_ids: Sequence[str],
        *,
        split: SplitName,
        epoch: int,
        training: bool,
        device: torch.device,
    ) -> GaussianBatch:
        """Sample ID の並びから、指定 device 上の 1 batch を組み立てる."""

        images = [self._image_for(sample_id).to(device) for sample_id in sample_ids]
        masks = [torch.ones_like(image[:1], dtype=torch.bool) for image in images]
        padded = PaddedBatch.pad(
            images,
            masks,
            placement_seeds=[
                _derived_seed(f"{sample_id}:{split}:{epoch}")
                for sample_id in sample_ids
            ],
            training=training,
            stride=BATCH_STRIDE,
        )
        target = torch.tensor(
            [[self.target_for(sample_id)] for sample_id in sample_ids],
            dtype=torch.float32,
            device=device,
        )
        return GaussianBatch(
            images=padded.images,
            valid_pixel_mask=padded.valid_pixel_masks,
            conditioning=None,
            target=target,
            sample_weight=torch.ones_like(target),
        )

    def _image_for(self, sample_id: str) -> Tensor:
        generator = torch.Generator().manual_seed(
            _derived_seed(f"{self._options.seed}:{sample_id}")
        )
        size = self._options.image_size
        return torch.rand(
            (IMAGE_CHANNELS, size, size), generator=generator, dtype=torch.float32
        )


class RecordingExperimentLogger(ExperimentLogger):
    """記録内容をそのまま観測できる in-memory の実験 logger.

    Trainer のテストが MLflow を起動せずに記録の中身を検証するために使う。

    ``ExperimentLogger`` は自前の ABC なので、これは 3rd-party のモックではない。
    """

    def __init__(self, *, run_id: str = "recorded-run") -> None:
        self.configured_run_id = run_id
        self.started = False
        self.start_call_count = 0
        self.run_kinds: list[str] = []
        self.run_names: list[str | None] = []
        self.params: dict[str, Scalar] = {}
        self.metrics: list[tuple[int, dict[str, float]]] = []
        self.tags: dict[str, str] = {}
        self.artifacts: list[tuple[Path, str | None]] = []
        self.flush_call_count = 0
        self.end_call_count = 0
        self.status: RunStatus | None = None

    @property
    @override
    def run_id(self) -> str:
        """開始済み run の ID."""

        if not self.started:
            raise RuntimeError("run を start していません")
        return self.configured_run_id

    @override
    def start(
        self,
        *,
        run_kind: str,
        run_name: str | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> str:
        """Run を開始したことを記録する."""

        self.started = True
        self.start_call_count += 1
        self.run_kinds.append(run_kind)
        self.run_names.append(run_name)
        self.tags.update(tags or {})
        return self.configured_run_id

    @override
    def log_params(self, params: Mapping[str, Scalar]) -> None:
        """記録した param を蓄積する."""

        self.params.update(params)

    @override
    def log_metrics(self, metrics: Mapping[str, float], *, step: int) -> None:
        """記録した metric を step 付きで蓄積する."""

        self.metrics.append((step, dict(metrics)))

    @override
    def log_artifact(self, path: Path, *, artifact_path: str | None = None) -> None:
        """記録した artifact の位置を蓄積する."""

        self.artifacts.append((path, artifact_path))

    @override
    def set_tags(self, tags: Mapping[str, str]) -> None:
        """記録した tag を蓄積する."""

        self.tags.update(tags)

    @override
    def flush(self) -> None:
        """Flush 回数を数える."""

        self.flush_call_count += 1

    @override
    def end(self, *, status: RunStatus = "FINISHED") -> None:
        """終了状態と終了回数を記録する."""

        self.end_call_count += 1
        self.status = status

    def metrics_for(self, key: str) -> list[tuple[int, float]]:
        """指定した metric キーについて ``(step, 値)`` の並びを返す."""

        return [(step, values[key]) for step, values in self.metrics if key in values]


def _derived_seed(material: str) -> int:
    return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:4], "big")


def _ceil_to(value: int, multiple: int) -> int:
    return ((value + multiple - 1) // multiple) * multiple
