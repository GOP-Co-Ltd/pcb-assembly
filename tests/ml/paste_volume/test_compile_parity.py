from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast, override

import pytest
import torch
from torch import Tensor, nn
from torch.nn import functional as F

from ml.paste_volume.compile_parity import (
    COMPILE_PARITY_KIND,
    COMPILE_PARITY_SCHEMA_VERSION,
    COMPILE_PARITY_TOLERANCE_PROFILE,
    REQUIRED_COMPILE_SHAPE_CASES,
    CompileParityBatch,
    CompileParityConfig,
    CompileShapeCase,
    compile_parity_report_from_dict,
    compile_parity_report_sha256,
    load_compile_parity_report,
    run_compile_parity,
    write_compile_parity_report,
)

_FINGERPRINTS = {
    "training_protocol_fingerprint": "sha256:" + "1" * 64,
    "dataset_fingerprint": "sha256:" + "2" * 64,
    "split_fingerprint": "sha256:" + "3" * 64,
}


class _TinyPasteVolumeModel(nn.Module):
    """Cheap real model that still gives every parameter a gradient."""

    def __init__(self) -> None:
        super().__init__()
        self._projection = nn.Conv2d(6, 2, kernel_size=1)
        self._mean_head = nn.Linear(3, 1)
        self._log_variance_head = nn.Linear(3, 1)

    @override
    def forward(
        self,
        image_6ch: Tensor,
        valid_pixel_mask: Tensor,
        pixel_per_mm: Tensor,
    ) -> tuple[Tensor, Tensor]:
        mask = valid_pixel_mask.to(dtype=image_6ch.dtype)
        projected = self._projection(image_6ch) * mask
        valid_count = mask.sum(dim=(2, 3)).clamp_min(1.0)
        pooled = projected.sum(dim=(2, 3)) / valid_count
        features = torch.cat((pooled, torch.log(pixel_per_mm)), dim=1)
        return (
            F.softplus(self._mean_head(features)),
            self._log_variance_head(features).clamp(-8.0, 4.0),
        )


def _batch(
    shape_case: str,
    *,
    height: int,
    width: int,
    batch_size: int = 1,
) -> CompileParityBatch:
    generator = torch.Generator().manual_seed(height * 1009 + width + batch_size)
    image = torch.randn(batch_size, 6, height, width, generator=generator)
    mask = torch.ones(batch_size, 1, height, width, dtype=torch.bool)
    mask[:, :, :, max(1, width - width // 8) :] = False
    return CompileParityBatch(
        case_id=f"{shape_case}-{height}x{width}-b{batch_size}",
        shape_case=cast(CompileShapeCase, shape_case),
        image_6ch=image,
        valid_pixel_mask=mask,
        pixel_per_mm=torch.full((batch_size, 1), 24.0),
        target_volume_ul=torch.linspace(0.4, 1.2, batch_size).reshape(-1, 1),
        sample_weight=torch.linspace(1.0, 0.5, batch_size).reshape(-1, 1),
        sample_ids=tuple(f"{shape_case}-sample-{index}" for index in range(batch_size)),
    )


def _required_batches() -> tuple[CompileParityBatch, ...]:
    return (
        _batch("minimum", height=32, width=32),
        _batch("maximum-area", height=512, width=512),
        _batch("portrait", height=1024, width=256),
        _batch("landscape", height=256, width=1024),
        _batch("training-batch", height=48, width=96, batch_size=2),
    )


def _successful_report(tmp_path: Path):
    weights = tmp_path / "weights.pt"
    weights.write_bytes(b"exact release weights artifact")
    return run_compile_parity(
        _TinyPasteVolumeModel(),
        _required_batches(),
        weights_path=weights,
        config=CompileParityConfig(backend="eager", mode="default", device="cpu"),
        **_FINGERPRINTS,
    )


class TestCompileParityBatch:
    @pytest.mark.parametrize(
        ("shape_case", "height", "width"),
        [
            ("minimum", 31, 32),
            ("maximum-area", 1024, 256),
            ("portrait", 256, 1024),
            ("landscape", 1024, 256),
        ],
    )
    def test_fixed_release_shape_contract_is_enforced(self, shape_case, height, width):
        with pytest.raises(ValueError, match="requires HxW"):
            _batch(shape_case, height=height, width=width)

    def test_training_batch_preserves_variable_batch_and_shape(self):
        batch = _batch("training-batch", height=73, width=105, batch_size=3)

        assert batch.image_6ch.shape == (3, 6, 73, 105)
        assert batch.sample_ids == (
            "training-batch-sample-0",
            "training-batch-sample-1",
            "training-batch-sample-2",
        )


class TestRunCompileParity:
    def test_real_torch_compile_checks_outputs_loss_and_every_gradient(self, tmp_path):
        report = _successful_report(tmp_path)

        assert report.kind == COMPILE_PARITY_KIND
        assert report.schema_version == COMPILE_PARITY_SCHEMA_VERSION
        assert report.success
        assert tuple(result.shape_case for result in report.cases) == (
            REQUIRED_COMPILE_SHAPE_CASES
        )
        assert report.runtime.backend == "eager"
        assert report.runtime.device == "cpu"
        assert report.runtime.dtype == "float32"
        assert report.runtime.tolerance_profile == COMPILE_PARITY_TOLERANCE_PROFILE
        assert report.runtime.torch_version == torch.__version__
        assert report.provenance.weights_sha256 == (
            "sha256:" + hashlib.sha256(b"exact release weights artifact").hexdigest()
        )
        assert (
            report.provenance.training_protocol_fingerprint
            == (_FINGERPRINTS["training_protocol_fingerprint"])
        )
        for result in report.cases:
            assert result.compile_succeeded
            assert result.parity_passed
            assert result.checked_gradient_count == result.trainable_parameter_count
            assert result.checked_gradient_count == 6
            assert not result.missing_gradient_parameters
            assert not result.nonfinite_gradient_parameters
            assert not result.mismatched_gradient_parameters
            assert result.eager_step_seconds is not None
            assert result.compile_and_first_step_seconds is not None
            assert result.compiled_replay_step_seconds is not None
            assert result.eager_samples_per_second is not None
            assert result.compiled_samples_per_second is not None
            assert result.failure_reason is None
        assert compile_parity_report_sha256(report) == report.content_sha256

    def test_missing_or_duplicate_release_shape_is_rejected(self, tmp_path):
        weights = tmp_path / "weights.pt"
        weights.write_bytes(b"weights")
        missing = _required_batches()[:-1]
        duplicate = (*_required_batches(), _required_batches()[0])

        with pytest.raises(ValueError, match="missing compile parity shape"):
            run_compile_parity(
                _TinyPasteVolumeModel(),
                missing,
                weights_path=weights,
                config=CompileParityConfig(backend="eager", device="cpu"),
                **_FINGERPRINTS,
            )
        with pytest.raises(ValueError, match="duplicate compile parity case_id"):
            run_compile_parity(
                _TinyPasteVolumeModel(),
                duplicate,
                weights_path=weights,
                config=CompileParityConfig(backend="eager", device="cpu"),
                **_FINGERPRINTS,
            )

    def test_unavailable_backend_is_persistable_failure_evidence(self, tmp_path):
        weights = tmp_path / "weights.pt"
        weights.write_bytes(b"weights")

        report = run_compile_parity(
            _TinyPasteVolumeModel(),
            _required_batches(),
            weights_path=weights,
            config=CompileParityConfig(
                backend="pcbasm-backend-does-not-exist", device="cpu"
            ),
            **_FINGERPRINTS,
        )

        assert not report.success
        assert all(not result.compile_succeeded for result in report.cases)
        assert all(not result.parity_passed for result in report.cases)
        assert all(
            result.failure_reason is not None
            and "torch.compile setup failed" in result.failure_reason
            for result in report.cases
        )
        path = write_compile_parity_report(report, tmp_path / "compile-failure.json")
        assert load_compile_parity_report(path) == report


class TestCompileParityReportPersistence:
    def test_create_only_round_trip_preserves_verified_content(self, tmp_path):
        report = _successful_report(tmp_path)
        path = tmp_path / "compile-parity.json"

        assert write_compile_parity_report(report, path) == path
        assert load_compile_parity_report(path) == report
        original = path.read_bytes()

        with pytest.raises(FileExistsError, match="will not be replaced"):
            write_compile_parity_report(report, path)

        assert path.read_bytes() == original

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda raw: raw.update({"cases": []}), "cases must be non-empty"),
            (
                lambda raw: raw["provenance"].update(
                    {"training_protocol_fingerprint": ""}
                ),
                "non-empty string",
            ),
            (
                lambda raw: raw["cases"][1].update(
                    {
                        "shape_case": raw["cases"][0]["shape_case"],
                        "input_shape": raw["cases"][0]["input_shape"],
                    }
                ),
                "duplicate shape cases",
            ),
            (
                lambda raw: raw["cases"][0].update({"case_id": "tampered"}),
                "does not match report content",
            ),
        ],
    )
    def test_strict_decoder_rejects_empty_invalid_duplicate_or_tampered_content(
        self, tmp_path, mutate, message
    ):
        report = _successful_report(tmp_path)
        raw = json.loads(json.dumps(report.to_dict()))
        mutate(raw)

        with pytest.raises(ValueError) as error:
            compile_parity_report_from_dict(raw)

        assert message in str(error.value)

    def test_unknown_json_fields_are_rejected(self, tmp_path):
        report = _successful_report(tmp_path)
        raw = report.to_dict()
        raw["unexpected"] = True

        with pytest.raises(ValueError, match="unknown"):
            compile_parity_report_from_dict(raw)
