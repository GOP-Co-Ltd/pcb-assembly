"""学習の進捗・best 選択と、その checkpoint 永続化.

checkpoint は書いたら書き換えない。

読み込む側は「役割・dataset・設定・run が一致するか」だけを見て、
一致しなければ理由文字列で拒否する。

Tensor を含むため JSON エンベロープは使えない。

``kind`` と ``schema_version`` を payload へ埋め、キー集合の完全一致で検証する
という考え方だけを :mod:`ml.artifact.document` から踏襲する。

書き込みは :func:`ml.artifact.atomic.atomic_write_stream` を通す。

読み戻し検証まで通ってから公開するので、中断しても既存の checkpoint は壊れない。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Set
from pathlib import Path
from typing import Literal, cast

import attrs
import torch
from torch import Tensor

from ml.artifact.atomic import atomic_write_stream
from ml.training.random_state import RandomState

CHECKPOINT_KIND = "ml-training-checkpoint"
CHECKPOINT_SCHEMA_VERSION = 1

type CheckpointRole = Literal["latest", "best", "final", "emergency"]

CHECKPOINT_ROLES: tuple[CheckpointRole, ...] = (
    "latest",
    "best",
    "final",
    "emergency",
)

# compile 済み wrapper の state_dict が付ける prefix。checkpoint へ入れてはならない。
_COMPILED_MODULE_PREFIX = "_orig_mod."

_PROGRESS_KEYS = frozenset(
    {"epoch", "global_step", "next_batch_index", "epochs_completed", "batch_plan"}
)
_SELECTION_KEYS = frozenset(
    {"monitor", "mode", "minimum_delta", "best_value", "best_epoch", "patience_counter"}
)
_CHECKPOINT_KEYS = frozenset(
    {
        "kind",
        "schema_version",
        "role",
        "created_unix_seconds",
        "run_id",
        "progress",
        "selection",
        "validation_metrics",
        "dataset_fingerprint",
        "config_fingerprint",
        "model_state",
        "optimizer_state",
        "scheduler_state",
        "gradient_scaler_state",
        "random_state",
    }
)


@attrs.frozen
class TrainingProgress:
    """どこまで学習したかを表す純粋な位置情報.

    ``batch_plan`` は現在の epoch の batch 並びで、epoch を完走すると空へ戻る。

    epoch 途中から再開するとき、同じ並びであることを確かめるために持ち回る。
    """

    epoch: int = 0
    global_step: int = 0
    next_batch_index: int = 0
    epochs_completed: int = 0
    batch_plan: tuple[tuple[str, ...], ...] = ()

    def validate(self) -> str | None:
        """位置情報の整合を検証する."""

        for name in ("epoch", "global_step", "next_batch_index", "epochs_completed"):
            value: int = getattr(self, name)
            if value < 0:
                return f"{name} は 0 以上が必要です: {value}"
        if self.next_batch_index > len(self.batch_plan):
            return (
                "next_batch_index が batch_plan の長さを超えています: "
                f"{self.next_batch_index} > {len(self.batch_plan)}"
            )
        return None

    def with_batch_plan(self, plan: tuple[tuple[str, ...], ...]) -> TrainingProgress:
        """現在の epoch の batch 並びを固定した位置情報を返す."""

        return attrs.evolve(self, batch_plan=plan)

    def with_committed_group(self, next_batch_index: int) -> TrainingProgress:
        """Group を 1 つ commit した位置情報を返す."""

        return attrs.evolve(
            self, next_batch_index=next_batch_index, global_step=self.global_step + 1
        )

    def with_completed_epoch(self) -> TrainingProgress:
        """Epoch を 1 つ完走した位置情報を返す."""

        return attrs.evolve(
            self,
            epoch=self.epoch + 1,
            epochs_completed=self.epochs_completed + 1,
            next_batch_index=0,
            batch_plan=(),
        )

    def to_payload(self) -> dict[str, object]:
        """``torch.save`` へ渡せる素の値だけの写像を返す."""

        return {
            "epoch": self.epoch,
            "global_step": self.global_step,
            "next_batch_index": self.next_batch_index,
            "epochs_completed": self.epochs_completed,
            "batch_plan": self.batch_plan,
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object]
    ) -> tuple[TrainingProgress | None, str | None]:
        """:meth:`to_payload` が作った写像から復元する."""

        if error := _key_set_error(payload, _PROGRESS_KEYS, "progress"):
            return None, error
        return (
            cls(
                epoch=int(cast(int, payload["epoch"])),
                global_step=int(cast(int, payload["global_step"])),
                next_batch_index=int(cast(int, payload["next_batch_index"])),
                epochs_completed=int(cast(int, payload["epochs_completed"])),
                batch_plan=tuple(
                    tuple(group)
                    for group in cast(
                        tuple[tuple[str, ...], ...], payload["batch_plan"]
                    )
                ),
            ),
            None,
        )


@attrs.frozen
class BestSelection:
    """監視する metric 1 本と、その最良値の記録.

    early stopping と scheduler の両方がこの 1 個から値を取る。

    同じ metric を別々の patience で二重監視しないため。
    """

    monitor: str
    mode: Literal["min", "max"]
    minimum_delta: float
    best_value: float | None = None
    best_epoch: int | None = None
    patience_counter: int = 0

    def validate(self) -> str | None:
        """監視設定の整合を検証する."""

        if not self.monitor:
            return "monitor は空にできません"
        if self.mode not in ("min", "max"):
            return f"mode は 'min' か 'max' が必要です: {self.mode!r}"
        if not math.isfinite(self.minimum_delta) or self.minimum_delta < 0:
            return f"minimum_delta は 0 以上の有限値が必要です: {self.minimum_delta}"
        if self.patience_counter < 0:
            return f"patience_counter は 0 以上が必要です: {self.patience_counter}"
        return None

    def consider(self, value: float, *, epoch: int) -> tuple[BestSelection, bool]:
        """新しい値を評価し、更新後の自分と改善したかを返す.

        元のオブジェクトは変えない純関数。
        """

        if self.best_value is None:
            improved = True
        elif self.mode == "min":
            improved = value < self.best_value - self.minimum_delta
        else:
            improved = value > self.best_value + self.minimum_delta
        if improved:
            return (
                attrs.evolve(
                    self, best_value=value, best_epoch=epoch, patience_counter=0
                ),
                True,
            )
        return attrs.evolve(self, patience_counter=self.patience_counter + 1), False

    def to_payload(self) -> dict[str, object]:
        """``torch.save`` へ渡せる素の値だけの写像を返す."""

        return {
            "monitor": self.monitor,
            "mode": self.mode,
            "minimum_delta": self.minimum_delta,
            "best_value": self.best_value,
            "best_epoch": self.best_epoch,
            "patience_counter": self.patience_counter,
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object]
    ) -> tuple[BestSelection | None, str | None]:
        """:meth:`to_payload` が作った写像から復元する."""

        if error := _key_set_error(payload, _SELECTION_KEYS, "best selection"):
            return None, error
        mode = payload["mode"]
        if mode not in ("min", "max"):
            return None, f"mode は 'min' か 'max' が必要です: {mode!r}"
        best_value = payload["best_value"]
        best_epoch = payload["best_epoch"]
        return (
            cls(
                monitor=str(payload["monitor"]),
                mode=cast(Literal["min", "max"], mode),
                minimum_delta=float(cast(float, payload["minimum_delta"])),
                best_value=None
                if best_value is None
                else float(cast(float, best_value)),
                best_epoch=None if best_epoch is None else int(cast(int, best_epoch)),
                patience_counter=int(cast(int, payload["patience_counter"])),
            ),
            None,
        )


@attrs.frozen(eq=False)
class TrainingCheckpoint:
    """1 時点の学習状態をまるごと写し取ったもの.

    Tensor は要素ごとの比較になり真偽値へ落ちないので、等価性は identity で決める。
    """

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

    def validate(self) -> str | None:
        """Checkpoint として保存してよい状態かを検証する."""

        if self.role not in CHECKPOINT_ROLES:
            return f"未知の checkpoint role です: {self.role!r}"
        if not self.run_id:
            return "run_id は空にできません"
        if error := self.progress.validate():
            return f"progress: {error}"
        if error := self.selection.validate():
            return f"selection: {error}"
        if compiled := sorted(
            name
            for name in self.model_state
            if name.startswith(_COMPILED_MODULE_PREFIX)
        ):
            return (
                "model_state に compile 済み wrapper の prefix が残っています"
                f"（{_COMPILED_MODULE_PREFIX}）: {compiled}"
            )
        return None

    def resume_rejection(
        self,
        *,
        dataset_fingerprint: str,
        config_fingerprint: str,
        model_state_keys: Set[str],
    ) -> str | None:
        """この checkpoint から再開してよいかを判定し、駄目なら理由を返す.

        ``run_id`` の照合はここでは行わない。

        実 run_id は logger の run を開始するまで決まらず、この判定は
        その前（model を書き換える前）に済ませる必要があるため。
        """

        if self.role == "emergency":
            return "emergency checkpoint は事後解析専用で、resume 対象にできません"
        if self.dataset_fingerprint != dataset_fingerprint:
            return (
                "dataset fingerprint が一致しません: "
                f"{self.dataset_fingerprint!r}（期待値 {dataset_fingerprint!r}）"
            )
        if self.config_fingerprint != config_fingerprint:
            return (
                "config fingerprint が一致しません: "
                f"{self.config_fingerprint!r}（期待値 {config_fingerprint!r}）"
            )
        saved_keys = set(self.model_state)
        current_keys = set(model_state_keys)
        missing = sorted(current_keys - saved_keys)
        unknown = sorted(saved_keys - current_keys)
        if missing or unknown:
            return (
                "model_state のキー集合が現在の model と一致しません"
                f"（checkpoint に不足: {missing}、checkpoint 側の余分: {unknown}）"
            )
        return None

    def to_payload(self) -> dict[str, object]:
        """``torch.save`` へ渡せる素の値だけの写像を返す."""

        return {
            "kind": CHECKPOINT_KIND,
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "role": self.role,
            "created_unix_seconds": self.created_unix_seconds,
            "run_id": self.run_id,
            "progress": self.progress.to_payload(),
            "selection": self.selection.to_payload(),
            "validation_metrics": {
                name: float(value) for name, value in self.validation_metrics.items()
            },
            "dataset_fingerprint": self.dataset_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "model_state": dict(self.model_state),
            "optimizer_state": dict(self.optimizer_state),
            "scheduler_state": dict(self.scheduler_state),
            "gradient_scaler_state": dict(self.gradient_scaler_state),
            "random_state": self.random_state.to_payload(),
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object]
    ) -> tuple[TrainingCheckpoint | None, str | None]:
        """:meth:`to_payload` が作った写像から復元する.

        エンベロープとキー集合の完全一致を検証し、違えば理由文字列を返す。
        """

        if error := _key_set_error(payload, _CHECKPOINT_KEYS, "checkpoint"):
            return None, error
        if payload["kind"] != CHECKPOINT_KIND:
            return None, (
                f"checkpoint の kind が一致しません: {payload['kind']!r}"
                f"（期待値 {CHECKPOINT_KIND!r}）"
            )
        if payload["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
            return None, (
                "checkpoint の schema_version が一致しません: "
                f"{payload['schema_version']!r}"
                f"（期待値 {CHECKPOINT_SCHEMA_VERSION}）"
            )
        role = payload["role"]
        if role not in CHECKPOINT_ROLES:
            return None, f"未知の checkpoint role です: {role!r}"
        for name in (
            "progress",
            "selection",
            "random_state",
            "validation_metrics",
            "model_state",
            "optimizer_state",
            "scheduler_state",
            "gradient_scaler_state",
        ):
            if not isinstance(payload[name], Mapping):
                return None, f"checkpoint の {name} は写像である必要があります"
        progress, error = TrainingProgress.from_payload(
            _as_mapping(payload["progress"])
        )
        if progress is None:
            return None, error
        selection, error = BestSelection.from_payload(_as_mapping(payload["selection"]))
        if selection is None:
            return None, error
        random_state, error = RandomState.from_payload(
            _as_mapping(payload["random_state"])
        )
        if random_state is None:
            return None, error
        return (
            cls(
                role=cast(CheckpointRole, role),
                created_unix_seconds=float(
                    cast(float, payload["created_unix_seconds"])
                ),
                run_id=str(payload["run_id"]),
                progress=progress,
                selection=selection,
                validation_metrics={
                    str(name): float(cast(float, value))
                    for name, value in _as_mapping(
                        payload["validation_metrics"]
                    ).items()
                },
                dataset_fingerprint=str(payload["dataset_fingerprint"]),
                config_fingerprint=str(payload["config_fingerprint"]),
                model_state=cast(
                    Mapping[str, Tensor], _as_mapping(payload["model_state"])
                ),
                optimizer_state=_as_mapping(payload["optimizer_state"]),
                scheduler_state=_as_mapping(payload["scheduler_state"]),
                gradient_scaler_state=_as_mapping(payload["gradient_scaler_state"]),
                random_state=random_state,
            ),
            None,
        )


class CheckpointStore:
    """役割ごとに 1 本ずつ checkpoint を置く directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        """Checkpoint を置く directory."""

        return self._directory

    def path_for(self, role: CheckpointRole) -> Path:
        """役割に対応する checkpoint のパス."""

        if role not in CHECKPOINT_ROLES:
            raise ValueError(f"未知の checkpoint role です: {role!r}")
        return self._directory / f"{role}.pt"

    def exists(self, role: CheckpointRole) -> bool:
        """役割に対応する checkpoint が置かれているか."""

        return self.path_for(role).is_file()

    def save(self, checkpoint: TrainingCheckpoint) -> Path:
        """Checkpoint を atomic に書き、読み戻し検証まで通してから公開する."""

        if error := checkpoint.validate():
            raise ValueError(error)
        path = self.path_for(checkpoint.role)
        payload = checkpoint.to_payload()
        atomic_write_stream(
            path,
            lambda stream: torch.save(payload, stream),
            validate_readback=_validate_readback,
        )
        return path

    def load(
        self, role: CheckpointRole
    ) -> tuple[TrainingCheckpoint | None, str | None]:
        """役割に対応する checkpoint を読む."""

        path = self.path_for(role)
        if not path.is_file():
            return None, f"checkpoint がありません: {path}"
        return self.load_path(path)

    def load_path(self, path: Path) -> tuple[TrainingCheckpoint | None, str | None]:
        """指定したパスの checkpoint を読む.

        読めない・形が違う場合は例外にせず理由文字列を返す。
        """

        payload, error = _load_payload(path)
        if payload is None:
            return None, error
        checkpoint, error = TrainingCheckpoint.from_payload(payload)
        if checkpoint is None:
            return None, error
        if error := checkpoint.validate():
            return None, error
        return checkpoint, None


def _validate_readback(path: Path) -> None:
    """公開前の一時ファイルを読み戻し、そのまま復元できることを確かめる."""

    payload, error = _load_payload(path)
    if payload is None:
        raise ValueError(error)
    checkpoint, error = TrainingCheckpoint.from_payload(payload)
    if checkpoint is None:
        raise ValueError(error)
    if error := checkpoint.validate():
        raise ValueError(error)


def _load_payload(path: Path) -> tuple[Mapping[str, object] | None, str | None]:
    try:
        payload = torch.load(path, map_location="cpu", weights_only=True)
    except Exception as error:  # noqa: BLE001 - torch の読み込み失敗は理由で返す
        return None, f"checkpoint を読めません（{path}）: {error}"
    if not isinstance(payload, dict):
        return None, f"checkpoint は写像である必要があります（{path}）"
    return cast(Mapping[str, object], payload), None


def _as_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"checkpoint payload の要素が写像ではありません: {value!r}")
    return cast(Mapping[str, object], value)


def _key_set_error(
    payload: Mapping[str, object], expected: frozenset[str], description: str
) -> str | None:
    keys = set(payload)
    if missing := sorted(expected - keys):
        return f"{description} payload に必要なキーがありません: {missing}"
    if unknown := sorted(keys - expected):
        return f"{description} payload に未知のキーがあります: {unknown}"
    return None


__all__ = [
    "CHECKPOINT_KIND",
    "CHECKPOINT_ROLES",
    "CHECKPOINT_SCHEMA_VERSION",
    "BestSelection",
    "CheckpointRole",
    "CheckpointStore",
    "TrainingCheckpoint",
    "TrainingProgress",
]
