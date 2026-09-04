"""学習進捗・best 選択・checkpoint 永続化の公開契約."""

from __future__ import annotations

from pathlib import Path

import pytest
import torch

from ml.training.checkpoint import (
    CHECKPOINT_KIND,
    CHECKPOINT_SCHEMA_VERSION,
    BestSelection,
    CheckpointRole,
    CheckpointStore,
    TrainingCheckpoint,
    TrainingProgress,
)
from ml.training.random_state import RandomState, seed_everything
from tests.ml.support import build_synthetic_model

# tiny config の model が持つ state_dict キー。checkpoint と ONNX export の公開契約。
# 内部属性をリネームすると、既存 checkpoint の resume と export の互換性が壊れる。
TINY_MODEL_STATE_DICT_KEYS = (
    "_encoder._padding_pixel",
    "_encoder._stages.0.0._convolution.weight",
    "_encoder._stages.0.0._entry.0.weight",
    "_encoder._stages.0.0._entry.1.bias",
    "_encoder._stages.0.0._entry.1.weight",
    "_encoder._stages.0.0._normalization.bias",
    "_encoder._stages.0.0._normalization.weight",
    "_encoder._stem.0.0.weight",
    "_encoder._stem.0.1.bias",
    "_encoder._stem.0.1.weight",
    "_head._log_variance.bias",
    "_head._log_variance.weight",
    "_head._mean.bias",
    "_head._mean.weight",
    "_head._trunk.0.bias",
    "_head._trunk.0.weight",
)

DATASET_FINGERPRINT = "sha256:dataset"
CONFIG_FINGERPRINT = "sha256:config"
RUN_ID = "run-1"


def _checkpoint(
    role: CheckpointRole = "latest", **overrides: object
) -> TrainingCheckpoint:
    seed_everything(0, deterministic=True)
    model = build_synthetic_model(seed=1)
    values: dict[str, object] = {
        "role": role,
        "created_unix_seconds": 1_757_000_000.0,
        "run_id": RUN_ID,
        "progress": TrainingProgress(epoch=1, global_step=4, epochs_completed=1),
        "selection": BestSelection(
            monitor="relative_error_score",
            mode="min",
            minimum_delta=1e-4,
            best_value=0.5,
            best_epoch=0,
        ),
        "validation_metrics": {"relative_error_score": 0.5},
        "dataset_fingerprint": DATASET_FINGERPRINT,
        "config_fingerprint": CONFIG_FINGERPRINT,
        "model_state": model.state_dict(),
        "optimizer_state": {"state": {}, "param_groups": []},
        "scheduler_state": {"best": 0.5},
        "gradient_scaler_state": {},
        "random_state": RandomState.capture(),
    }
    values.update(overrides)
    return TrainingCheckpoint(**values)  # pyright: ignore[reportArgumentType]


class TestModelStateDictContract:
    """``state_dict()`` のキーは checkpoint と export の公開契約."""

    def test_tiny_model_keys_are_pinned(self):
        model = build_synthetic_model(seed=1)

        keys = tuple(sorted(model.state_dict()))

        assert keys == TINY_MODEL_STATE_DICT_KEYS, (
            "state_dict のキーが変わりました。内部属性のリネームは既存 checkpoint の "
            "resume と ONNX export の互換性を壊します"
        )


class TestTrainingProgress:
    """進捗の遷移は純関数で、元のオブジェクトを変えない."""

    def test_default_progress_is_valid(self):
        assert TrainingProgress().validate() is None

    def test_negative_epoch_is_rejected(self):
        reason = TrainingProgress(epoch=-1).validate()

        assert reason is not None
        assert "epoch" in reason

    def test_with_batch_plan_returns_a_new_value(self):
        progress = TrainingProgress()
        plan = (("a", "b"), ("c",))

        updated = progress.with_batch_plan(plan)

        assert updated.batch_plan == plan
        assert progress.batch_plan == ()

    def test_committed_group_advances_the_global_step(self):
        progress = TrainingProgress(global_step=4, next_batch_index=2)

        updated = progress.with_committed_group(5)

        assert updated.global_step == 5
        assert updated.next_batch_index == 5
        assert progress.global_step == 4
        assert progress.next_batch_index == 2

    def test_completed_epoch_resets_the_batch_position(self):
        progress = TrainingProgress(
            epoch=1,
            global_step=9,
            next_batch_index=3,
            epochs_completed=1,
            batch_plan=(("a",),),
        )

        updated = progress.with_completed_epoch()

        assert updated.epoch == 2
        assert updated.epochs_completed == 2
        assert updated.next_batch_index == 0
        assert updated.batch_plan == ()
        assert updated.global_step == 9
        assert progress.epoch == 1


class TestBestSelection:
    """Monitor の改善判定は 1 か所に集約され、純関数で遷移する."""

    def test_valid_selection_has_no_reason(self):
        selection = BestSelection(monitor="loss", mode="min", minimum_delta=1e-4)

        assert selection.validate() is None

    def test_empty_monitor_is_rejected(self):
        reason = BestSelection(monitor="", mode="min", minimum_delta=1e-4).validate()

        assert reason is not None
        assert "monitor" in reason

    def test_negative_minimum_delta_is_rejected(self):
        reason = BestSelection(
            monitor="loss", mode="min", minimum_delta=-1.0
        ).validate()

        assert reason is not None
        assert "minimum_delta" in reason

    def test_first_value_is_always_an_improvement(self):
        selection = BestSelection(monitor="loss", mode="min", minimum_delta=1e-4)

        updated, improved = selection.consider(1.0, epoch=0)

        assert improved is True
        assert updated.best_value == 1.0
        assert updated.best_epoch == 0
        assert updated.patience_counter == 0
        assert selection.best_value is None

    def test_smaller_value_improves_in_min_mode(self):
        selection, _ = BestSelection(
            monitor="loss", mode="min", minimum_delta=1e-4
        ).consider(1.0, epoch=0)

        updated, improved = selection.consider(0.5, epoch=1)

        assert improved is True
        assert updated.best_value == 0.5
        assert updated.best_epoch == 1
        assert updated.patience_counter == 0

    def test_larger_value_improves_in_max_mode(self):
        selection, _ = BestSelection(
            monitor="coverage", mode="max", minimum_delta=1e-4
        ).consider(0.5, epoch=0)

        updated, improved = selection.consider(0.9, epoch=1)

        assert improved is True
        assert updated.best_value == 0.9
        assert updated.best_epoch == 1

    def test_improvement_below_minimum_delta_increases_patience(self):
        selection, _ = BestSelection(
            monitor="loss", mode="min", minimum_delta=0.1
        ).consider(1.0, epoch=0)

        updated, improved = selection.consider(0.95, epoch=1)

        assert improved is False
        assert updated.best_value == 1.0
        assert updated.best_epoch == 0
        assert updated.patience_counter == 1

    def test_patience_accumulates_across_non_improving_epochs(self):
        selection = BestSelection(monitor="loss", mode="min", minimum_delta=1e-4)
        selection, _ = selection.consider(1.0, epoch=0)
        selection, _ = selection.consider(2.0, epoch=1)
        selection, _ = selection.consider(3.0, epoch=2)

        assert selection.patience_counter == 2
        assert selection.best_epoch == 0


class TestTrainingCheckpointValidation:
    """Checkpoint は compile 由来の prefix を持ち込ませない."""

    def test_plain_checkpoint_is_valid(self):
        assert _checkpoint().validate() is None

    def test_compiled_prefix_in_model_state_is_rejected(self):
        model_state = {
            f"_orig_mod.{key}": value
            for key, value in build_synthetic_model(seed=1).state_dict().items()
        }

        reason = _checkpoint(model_state=model_state).validate()

        assert reason is not None
        assert "_orig_mod." in reason


class TestTrainingCheckpointPayload:
    """Payload はキー集合の完全一致で検証する."""

    def test_round_trip_preserves_every_field(self):
        checkpoint = _checkpoint()

        recovered, reason = TrainingCheckpoint.from_payload(checkpoint.to_payload())

        assert reason is None
        assert recovered is not None
        assert recovered.role == checkpoint.role
        assert recovered.run_id == checkpoint.run_id
        assert recovered.progress == checkpoint.progress
        assert recovered.selection == checkpoint.selection
        assert dict(recovered.validation_metrics) == dict(checkpoint.validation_metrics)
        assert recovered.dataset_fingerprint == checkpoint.dataset_fingerprint
        assert recovered.config_fingerprint == checkpoint.config_fingerprint
        assert sorted(recovered.model_state) == sorted(checkpoint.model_state)
        assert all(
            torch.equal(recovered.model_state[key], checkpoint.model_state[key])
            for key in checkpoint.model_state
        )

    def test_payload_carries_the_envelope(self):
        payload = _checkpoint().to_payload()

        assert payload["kind"] == CHECKPOINT_KIND
        assert payload["schema_version"] == CHECKPOINT_SCHEMA_VERSION

    def test_wrong_kind_is_rejected(self):
        payload = _checkpoint().to_payload()
        payload["kind"] = "something-else"

        recovered, reason = TrainingCheckpoint.from_payload(payload)

        assert recovered is None
        assert reason is not None
        assert CHECKPOINT_KIND in reason

    def test_wrong_schema_version_is_rejected(self):
        payload = _checkpoint().to_payload()
        payload["schema_version"] = CHECKPOINT_SCHEMA_VERSION + 1

        recovered, reason = TrainingCheckpoint.from_payload(payload)

        assert recovered is None
        assert reason is not None
        assert "schema_version" in reason

    def test_missing_key_is_rejected(self):
        payload = _checkpoint().to_payload()
        del payload["optimizer_state"]

        recovered, reason = TrainingCheckpoint.from_payload(payload)

        assert recovered is None
        assert reason is not None
        assert "optimizer_state" in reason

    def test_unknown_key_is_rejected(self):
        payload = _checkpoint().to_payload()
        payload["unexpected"] = 1

        recovered, reason = TrainingCheckpoint.from_payload(payload)

        assert recovered is None
        assert reason is not None
        assert "unexpected" in reason


class TestResumeRejection:
    """Resume してよいかを 1 か所で理由つきに判定する."""

    def test_matching_checkpoint_is_accepted(self):
        checkpoint = _checkpoint()

        reason = checkpoint.resume_rejection(
            dataset_fingerprint=DATASET_FINGERPRINT,
            config_fingerprint=CONFIG_FINGERPRINT,
            run_id=RUN_ID,
            model_state_keys=set(checkpoint.model_state),
        )

        assert reason is None

    def test_emergency_checkpoint_is_rejected(self):
        checkpoint = _checkpoint(role="emergency")

        reason = checkpoint.resume_rejection(
            dataset_fingerprint=DATASET_FINGERPRINT,
            config_fingerprint=CONFIG_FINGERPRINT,
            run_id=RUN_ID,
            model_state_keys=set(checkpoint.model_state),
        )

        assert reason is not None
        assert "emergency" in reason

    def test_dataset_fingerprint_mismatch_is_rejected(self):
        checkpoint = _checkpoint()

        reason = checkpoint.resume_rejection(
            dataset_fingerprint="sha256:other",
            config_fingerprint=CONFIG_FINGERPRINT,
            run_id=RUN_ID,
            model_state_keys=set(checkpoint.model_state),
        )

        assert reason is not None
        assert "dataset" in reason

    def test_config_fingerprint_mismatch_is_rejected(self):
        checkpoint = _checkpoint()

        reason = checkpoint.resume_rejection(
            dataset_fingerprint=DATASET_FINGERPRINT,
            config_fingerprint="sha256:other",
            run_id=RUN_ID,
            model_state_keys=set(checkpoint.model_state),
        )

        assert reason is not None
        assert "config" in reason

    def test_run_id_mismatch_is_rejected(self):
        checkpoint = _checkpoint()

        reason = checkpoint.resume_rejection(
            dataset_fingerprint=DATASET_FINGERPRINT,
            config_fingerprint=CONFIG_FINGERPRINT,
            run_id="another-run",
            model_state_keys=set(checkpoint.model_state),
        )

        assert reason is not None
        assert "run_id" in reason

    def test_model_state_key_mismatch_is_rejected(self):
        checkpoint = _checkpoint()

        reason = checkpoint.resume_rejection(
            dataset_fingerprint=DATASET_FINGERPRINT,
            config_fingerprint=CONFIG_FINGERPRINT,
            run_id=RUN_ID,
            model_state_keys={*checkpoint.model_state, "_head._extra.weight"},
        )

        assert reason is not None
        assert "_head._extra.weight" in reason


class TestCheckpointStore:
    """Checkpoint は role ごとに 1 本のファイルへ atomic に保存する."""

    def test_paths_follow_the_role_names(self, tmp_path: Path):
        store = CheckpointStore(tmp_path)

        assert store.directory == tmp_path
        assert store.path_for("latest").name == "latest.pt"
        assert store.path_for("best").name == "best.pt"
        assert store.path_for("final").name == "final.pt"
        assert store.path_for("emergency").name == "emergency.pt"

    def test_save_then_load_round_trips_the_tensors(self, tmp_path: Path):
        store = CheckpointStore(tmp_path)
        checkpoint = _checkpoint()

        path = store.save(checkpoint)
        loaded, reason = store.load("latest")

        assert path == store.path_for("latest")
        assert store.exists("latest") is True
        assert reason is None
        assert loaded is not None
        assert loaded.progress == checkpoint.progress
        assert all(
            torch.equal(loaded.model_state[key], checkpoint.model_state[key])
            for key in checkpoint.model_state
        )

    def test_saving_the_same_role_twice_overwrites(self, tmp_path: Path):
        store = CheckpointStore(tmp_path)
        store.save(_checkpoint())

        store.save(_checkpoint(progress=TrainingProgress(epoch=3, global_step=12)))
        loaded, reason = store.load("latest")

        assert reason is None
        assert loaded is not None
        assert loaded.progress.epoch == 3

    def test_load_path_reads_an_explicit_file(self, tmp_path: Path):
        store = CheckpointStore(tmp_path)
        path = store.save(_checkpoint(role="best"))

        loaded, reason = store.load_path(path)

        assert reason is None
        assert loaded is not None
        assert loaded.role == "best"

    def test_missing_file_is_reported(self, tmp_path: Path):
        store = CheckpointStore(tmp_path)

        loaded, reason = store.load("latest")

        assert loaded is None
        assert reason is not None
        assert store.exists("latest") is False

    def test_corrupted_file_is_reported(self, tmp_path: Path):
        store = CheckpointStore(tmp_path)
        store.path_for("latest").write_bytes(b"not a torch archive")

        loaded, reason = store.load("latest")

        assert loaded is None
        assert reason is not None

    def test_failed_readback_leaves_the_previous_file_intact(self, tmp_path: Path):
        store = CheckpointStore(tmp_path)
        good = _checkpoint()
        store.save(good)
        broken = _checkpoint(
            model_state={
                f"_orig_mod.{key}": value for key, value in good.model_state.items()
            }
        )

        with pytest.raises(ValueError):
            store.save(broken)

        loaded, reason = store.load("latest")
        assert reason is None
        assert loaded is not None
        assert sorted(loaded.model_state) == sorted(good.model_state)
