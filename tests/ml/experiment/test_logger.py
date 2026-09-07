"""実験記録 ABC と固定タグデコレータの公開契約."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import override

import pytest

from ml.experiment.logger import ExperimentLogger, TaggedExperimentLogger
from tests.ml.support import RecordingExperimentLogger


class _IncompleteLogger(ExperimentLogger):
    """``run_id`` しか実装していない、契約違反の検証用 logger."""

    @property
    @override
    def run_id(self) -> str:
        return "incomplete"


class TestExperimentLogger:
    """``ExperimentLogger`` は実装漏れを instantiate 時に弾く."""

    def test_recording_logger_implements_every_abstract_method(self):
        logger = RecordingExperimentLogger()

        logger.start(run_kind="training")

        assert logger.run_id == "recorded-run"
        assert isinstance(logger, ExperimentLogger)

    def test_partial_implementation_cannot_be_instantiated(self):
        with pytest.raises(TypeError) as exception:
            _IncompleteLogger()  # pyright: ignore[reportAbstractUsage]

        assert "abstract" in str(exception.value).lower()

    def test_run_id_before_start_is_rejected(self):
        logger = RecordingExperimentLogger()

        with pytest.raises(RuntimeError) as exception:
            _ = logger.run_id

        assert "start" in str(exception.value)


class TestTaggedExperimentLogger:
    """固定タグを必ず付けたうえで内側の logger へ素通しする."""

    def test_start_adds_the_fixed_tags(self):
        inner = RecordingExperimentLogger()
        logger = TaggedExperimentLogger(inner, tags={"git.commit": "abc123"})

        run_id = logger.start(run_kind="training", run_name="run-1")

        assert run_id == inner.configured_run_id
        assert inner.tags["git.commit"] == "abc123"
        assert inner.run_kinds == ["training"]
        assert inner.run_names == ["run-1"]

    def test_fixed_tags_win_over_caller_tags(self):
        inner = RecordingExperimentLogger()
        logger = TaggedExperimentLogger(inner, tags={"git.commit": "fixed"})

        logger.start(run_kind="training", tags={"git.commit": "caller", "own": "kept"})

        assert inner.tags["git.commit"] == "fixed"
        assert inner.tags["own"] == "kept"

    def test_run_id_is_delegated(self):
        inner = RecordingExperimentLogger()
        logger = TaggedExperimentLogger(inner, tags={"git.commit": "abc123"})
        logger.start(run_kind="training")

        assert logger.run_id == inner.run_id

    def test_other_methods_pass_through_unchanged(self, tmp_path: Path):
        inner = RecordingExperimentLogger()
        logger = TaggedExperimentLogger(inner, tags={"git.commit": "abc123"})
        artifact = tmp_path / "artifact.txt"
        artifact.write_text("payload", encoding="utf-8")
        logger.start(run_kind="training")

        logger.log_params({"learning_rate": 0.001, "deterministic": True})
        logger.log_metrics({"validation/loss": 0.5}, step=7)
        logger.log_artifact(artifact, artifact_path="checkpoints")
        logger.set_tags({"stage": "finalization"})
        logger.flush()
        logger.end(status="FAILED")

        assert inner.params == {"learning_rate": 0.001, "deterministic": True}
        assert inner.metrics == [(7, {"validation/loss": 0.5})]
        assert inner.artifacts == [(artifact, "checkpoints")]
        assert inner.tags["stage"] == "finalization"
        assert inner.flush_call_count == 1
        assert inner.end_call_count == 1
        assert inner.status == "FAILED"

    def test_set_tags_does_not_drop_the_fixed_tags(self):
        inner = RecordingExperimentLogger()
        logger = TaggedExperimentLogger(inner, tags={"git.commit": "fixed"})
        logger.start(run_kind="training")

        logger.set_tags({"stage": "finalization"})

        assert inner.tags["git.commit"] == "fixed"


class TestLoggerMappingIsolation:
    """呼び出し側が渡した Mapping を後から変えても記録は動かない."""

    def test_start_tags_are_copied(self):
        inner = RecordingExperimentLogger()
        logger = TaggedExperimentLogger(inner, tags={"git.commit": "fixed"})
        caller_tags: dict[str, str] = {"stage": "before"}

        logger.start(run_kind="training", tags=caller_tags)
        caller_tags["stage"] = "after"

        assert inner.tags["stage"] == "before"

    def test_params_mapping_is_copied(self):
        inner = RecordingExperimentLogger()
        params: dict[str, float] = {"learning_rate": 0.001}
        mapping: Mapping[str, float] = params

        inner.log_params(mapping)
        params["learning_rate"] = 0.5

        assert inner.params["learning_rate"] == 0.001
