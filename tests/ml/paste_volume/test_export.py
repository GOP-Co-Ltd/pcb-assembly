from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import attrs
import numpy as np
import onnx
import onnxruntime as ort
import pytest

from ml.paste_volume.artifact import ArtifactLineage
from ml.paste_volume.benchmark import (
    BenchmarkSample,
    benchmark_model_candidate,
    save_model_benchmark_result,
)
from ml.paste_volume.data import (
    build_sample_index,
    create_session_split,
    resolve_dataset_inputs,
    save_split_manifest,
)
from ml.paste_volume.formal_artifact import publish_formal_artifact
from ml.paste_volume.onnx import (
    export_onnx_model_from_formal_weights,
    optimize_model,
    validate_onnx_parity,
)
from ml.paste_volume.package import (
    verify_model_package,
)
from ml.paste_volume.promotion import (
    PasteVolumeReleasePipeline,
    TrainingCoverage,
    activate_model_package,
    load_formal_cross_validation_evidence,
    package_model_candidate,
    package_model_candidate_from_dataset,
    promote_and_activate_model_package,
    promote_and_activate_model_package_from_dataset,
    promote_model_package,
    promote_model_package_from_dataset,
)
from ml.paste_volume.release import (
    build_model_candidate_validation,
    evaluate_frozen_test_candidate,
    evaluate_frozen_test_candidate_from_dataset,
    evaluate_validation_candidate,
    evaluate_validation_candidate_from_dataset,
    finalize_selected_model_candidate,
    save_candidate_evaluation,
    save_candidate_predictions,
    save_finalized_model_candidate,
    save_model_candidate_validation,
    save_selected_model_candidate,
    select_model_candidate,
)
from ml.training.experiment import MLflowExperimentLogger
from tests.ml.paste_volume.support_data import write_synthetic_session
from tests.ml.paste_volume.support_runtime import (
    canonical_preprocess_schema,
    formal_test_weights,
    make_test_lineage,
    passing_benchmark,
    passing_candidate,
    passing_compile_parity,
    passing_cross_validation_evidence,
    passing_cross_validation_reports,
    passing_export_parity,
    passing_finalized,
    passing_predictions,
    runtime_artifacts,
    write_test_weights,
)


class TestOnnxExport:
    def test_real_onnx_runtime_supports_dynamic_minimum_and_maximum_shapes(
        self, runtime_artifacts
    ):
        parity = validate_onnx_parity(
            runtime_artifacts.directory / "export" / "weights.pt",
            runtime_artifacts.model_path,
            shapes=((32, 32), (1024, 256)),
        )
        model = onnx.load(str(runtime_artifacts.model_path))
        session = ort.InferenceSession(
            str(runtime_artifacts.model_path), providers=["CPUExecutionProvider"]
        )
        result = session.run(
            None,
            {
                "image_6ch": np.zeros((1, 6, 64, 96), dtype=np.float32),
                "valid_pixel_mask": np.ones((1, 1, 64, 96), dtype=np.bool_),
                "pixel_per_mm": np.asarray([[20.0]], dtype=np.float32),
            },
        )

        onnx.checker.check_model(model, full_check=True)
        assert parity.passed
        assert [np.asarray(item).shape for item in result] == [(1, 1), (1, 1)]
        assert all(node.domain in ("", "ai.onnx") for node in model.graph.node)

    def test_formal_export_parity_covers_every_validation_sample_and_boundary_shape(
        self, runtime_artifacts
    ):
        report = runtime_artifacts.export_parity

        assert (
            report.sample_ids
            == runtime_artifacts.finalized.selection.candidate.evaluation.evaluated_sample_ids
        )
        assert [
            (item.case_name, item.image_height, item.image_width)
            for item in report.shape_results
        ] == [
            ("minimum", 32, 32),
            ("maximum-area", 512, 512),
            ("portrait", 1024, 256),
            ("landscape", 256, 1024),
        ]
        assert report.success
        assert all(item.passed for item in report.shape_results)

    def test_checkpoint_or_unknown_weight_schema_is_not_an_export_input(self, tmp_path):
        import torch

        checkpoint = tmp_path / "best.ckpt"
        torch.save({"role": "best", "model_state": {}}, checkpoint)

        from ml.paste_volume.onnx import export_onnx_model

        with pytest.raises(ValueError) as error:
            export_onnx_model(checkpoint, tmp_path / "model.onnx")

        assert "artifact" in str(error.value)

    def test_non_best_checkpoint_role_cannot_be_hidden_by_weights(self, tmp_path):
        import torch

        lineage = make_test_lineage()
        weights = write_test_weights(tmp_path / "weights.pt", lineage)
        payload = torch.load(weights, map_location="cpu", weights_only=True)
        payload["source_checkpoint_role"] = "latest"
        torch.save(payload, weights)
        from ml.paste_volume.onnx import export_onnx_model

        with pytest.raises(ValueError) as error:
            export_onnx_model(weights, tmp_path / "model.onnx")

        assert "best checkpoint" in str(error.value)


class TestStaticInt8Optimization:
    def test_calibration_uses_only_persisted_train_assignment(self, tmp_path):
        dataset = tmp_path / "dataset"
        for index in range(5):
            write_synthetic_session(
                dataset,
                f"session-{index}",
                pad_count=1,
                width=32 + index * 2,
                height=32,
            )
        composite = resolve_dataset_inputs(roots=(dataset,))
        samples = build_sample_index(composite)
        split = create_session_split(
            samples,
            composite.composite_fingerprint,
            seed=31,
        )
        split_path = tmp_path / "split.json"
        save_split_manifest(split, split_path)
        lineage = ArtifactLineage(
            source_run_id="quant-run",
            source_checkpoint_sha256="sha256:" + "3" * 64,
            dataset_fingerprint=composite.composite_fingerprint,
            split_fingerprint=split.split_fingerprint,
            training_protocol_fingerprint="sha256:" + "4" * 64,
        )
        weights = write_test_weights(tmp_path / "weights.pt", lineage)
        from ml.paste_volume.onnx import export_onnx_model

        exported = export_onnx_model(
            weights,
            tmp_path / "fp32.onnx",
            verification_shapes=((32, 32), (40, 32)),
        )
        result = optimize_model(
            exported.model_path,
            tmp_path / "optimized",
            calibration_data=(dataset,),
            split_manifest=split_path,
            calibration_sample_limit=3,
            seed=9,
        )

        assert set(result.calibration_sample_ids) <= set(split.train_sample_ids)
        assert len(set(result.calibration_session_ids)) == len(
            result.calibration_session_ids
        )
        assert result.optimized_fp32_path.is_file()
        assert result.int8_path.is_file()
        int8 = onnx.load(str(result.int8_path))
        assert any(node.op_type == "QuantizeLinear" for node in int8.graph.node)
        session = ort.InferenceSession(
            str(result.int8_path), providers=["CPUExecutionProvider"]
        )
        output = session.run(
            None,
            {
                "image_6ch": np.zeros((1, 6, 32, 40), dtype=np.float32),
                "valid_pixel_mask": np.ones((1, 1, 32, 40), dtype=np.bool_),
                "pixel_per_mm": np.asarray([[20.0]], dtype=np.float32),
            },
        )
        assert all(np.isfinite(np.asarray(value)).all() for value in output)

        with pytest.raises(ValueError) as error:
            evaluate_validation_candidate(
                result.int8_path,
                model_format="onnx-fp32",
                predictions=passing_predictions(),
                lineage=lineage,
                fp32_reference_artifact_sha256=(exported.model_artifact_sha256),
                evaluation_dataset_fingerprint=lineage.dataset_fingerprint,
                evaluation_split_fingerprint=lineage.split_fingerprint,
                training_sample_ids=("training-sample-0",),
            )

        assert "model_format" in str(error.value)

    def test_split_from_another_dataset_is_rejected(self, tmp_path, runtime_artifacts):
        dataset = tmp_path / "dataset"
        for index in range(3):
            write_synthetic_session(dataset, f"session-{index}", pad_count=1)
        composite = resolve_dataset_inputs(roots=(dataset,))
        samples = build_sample_index(composite)
        split = create_session_split(samples, composite.composite_fingerprint)
        split_path = tmp_path / "split.json"
        save_split_manifest(split, split_path)

        with pytest.raises(ValueError) as error:
            optimize_model(
                runtime_artifacts.fp32_reference_path,
                tmp_path / "optimized",
                calibration_data=(dataset,),
                split_manifest=split_path,
            )

        assert "training dataset fingerprint" in str(error.value)


class TestCandidateSelectionAndPromotion:
    def test_formal_cross_validation_loader_verifies_finished_summary_run(
        self, runtime_artifacts, tmp_path, monkeypatch
    ):
        report_path = tmp_path / "cross-validation-machine.json"
        shutil.copyfile(
            runtime_artifacts.cross_validation_reports[0].report_path,
            report_path,
        )
        monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
        tracking_uri = (tmp_path / "mlruns").resolve().as_uri()
        logger = MLflowExperimentLogger(
            tracking_uri=tracking_uri,
            experiment_name="cross-validation-promotion-test",
        )
        logger.start(run_kind="cross-validation-summary")
        publish_formal_artifact(
            report_path,
            tracking_uri=tracking_uri,
            run_kind="cross-validation-summary",
            logger=logger,
        )

        evidence = load_formal_cross_validation_evidence(
            report_path,
            tracking_uri=tracking_uri,
        )

        assert evidence.report.dimension == "machine"
        assert evidence.summary_attestation.status == "FINISHED"

    def test_promotion_rejects_raw_cross_validation_reports(
        self, runtime_artifacts, tmp_path
    ):
        destination = tmp_path / "raw-cross-validation-package"

        with pytest.raises(ValueError, match="typed"):
            promote_model_package_from_dataset(
                runtime_artifacts.finalized,
                destination,
                dataset_paths=(runtime_artifacts.dataset_path,),
                split_manifest=runtime_artifacts.split_path,
                cross_validation_evidence=cast(
                    Any, runtime_artifacts.cross_validation_reports
                ),
                model_name="test",
                model_version="raw-cross-validation",
            )

        assert not destination.exists()

    @pytest.mark.parametrize(
        ("field", "message"),
        (("run_id", "run ID"), ("tracking_uri_sha256", "tracking URI")),
    )
    def test_promotion_rejects_inconsistent_formal_summary_identity(
        self, runtime_artifacts, tmp_path, field, message
    ):
        evidence = list(runtime_artifacts.cross_validation_evidence)
        first = evidence[0].summary_attestation
        replacement = first.run_id if field == "run_id" else "sha256:" + "b" * 64
        changed_attestation = replace(
            evidence[1].summary_attestation,
            **{field: replacement},
        )
        evidence[1] = attrs.evolve(evidence[1], summary_attestation=changed_attestation)
        destination = tmp_path / f"inconsistent-{field}"

        with pytest.raises(ValueError, match=message):
            promote_model_package_from_dataset(
                runtime_artifacts.finalized,
                destination,
                dataset_paths=(runtime_artifacts.dataset_path,),
                split_manifest=runtime_artifacts.split_path,
                cross_validation_evidence=tuple(evidence),
                model_name="test",
                model_version="inconsistent-cross-validation",
            )

        assert not destination.exists()

    @pytest.mark.parametrize(
        "padded",
        (
            {
                "padding-25-right": 0.11,
                "padding-50-bottom": 0.11,
            },
            {
                "padding-10-top": 0.11,
                "padding-25-right": 0.11,
                "padding-50-bottom": 0.11,
                "padding-75-left": 0.11,
            },
        ),
    )
    def test_padding_invariance_requires_the_exact_pipeline_conditions(
        self, runtime_artifacts, padded
    ):
        predictions = list(passing_predictions())
        predictions[0] = attrs.evolve(predictions[0], padded_mean_volume_ul=padded)

        with pytest.raises(ValueError) as error:
            evaluate_validation_candidate(
                runtime_artifacts.model_path,
                model_format="onnx-fp32",
                predictions=predictions,
                lineage=runtime_artifacts.lineage,
                fp32_reference_artifact_sha256=hashlib.sha256(
                    runtime_artifacts.fp32_reference_path.read_bytes()
                ).hexdigest(),
                evaluation_dataset_fingerprint=(
                    runtime_artifacts.lineage.dataset_fingerprint
                ),
                evaluation_split_fingerprint=(
                    runtime_artifacts.lineage.split_fingerprint
                ),
                training_sample_ids=("training-sample-0",),
            )

        assert "padding condition集合" in str(error.value)

    def test_release_reports_are_create_only(self, tmp_path, runtime_artifacts):
        finalized = passing_finalized(
            runtime_artifacts.model_path, runtime_artifacts.lineage
        )
        candidate = finalized.selection.candidate

        def assert_create_only(
            name: str, save: Callable[[Path, Any], Path], value: Any
        ) -> None:
            output = tmp_path / f"{name}.json"
            save(output, value)
            original = output.read_bytes()

            with pytest.raises(FileExistsError):
                save(output, value)

            assert output.read_bytes() == original

        assert_create_only(
            "predictions", save_candidate_predictions, passing_predictions()
        )
        assert_create_only(
            "evaluation", save_candidate_evaluation, candidate.evaluation
        )
        assert_create_only(
            "benchmark", save_model_benchmark_result, candidate.benchmark
        )
        assert_create_only("candidate", save_model_candidate_validation, candidate)
        assert_create_only(
            "selection", save_selected_model_candidate, finalized.selection
        )
        assert_create_only("finalized", save_finalized_model_candidate, finalized)

    def test_report_output_cannot_alias_an_input_report(
        self, tmp_path, runtime_artifacts
    ):
        candidate = passing_candidate(
            runtime_artifacts.model_path, runtime_artifacts.lineage
        )
        input_report = tmp_path / "evaluation.json"
        save_candidate_evaluation(input_report, candidate.evaluation)
        original = input_report.read_bytes()

        with pytest.raises(FileExistsError):
            save_model_candidate_validation(input_report, candidate)

        assert input_report.read_bytes() == original

    @pytest.mark.parametrize("copy_candidate", (False, True))
    def test_candidate_cannot_be_its_own_fp32_reference(
        self, tmp_path, runtime_artifacts, copy_candidate
    ):
        reference = runtime_artifacts.model_path
        if copy_candidate:
            reference = tmp_path / "same-artifact.onnx"
            shutil.copyfile(runtime_artifacts.model_path, reference)

        with pytest.raises(ValueError) as error:
            evaluate_validation_candidate_from_dataset(
                runtime_artifacts.model_path,
                reference,
                model_format="onnx-fp32",
                dataset_paths=(),
                split_manifest=tmp_path / "unused-split.json",
            )

        assert "candidate" in str(error.value)

    def test_candidate_is_bound_to_the_original_fp32_export(
        self, tmp_path, runtime_artifacts
    ):
        wrong_reference = tmp_path / "wrong-reference.onnx"
        shutil.copyfile(runtime_artifacts.fp32_reference_path, wrong_reference)
        model = onnx.load(str(wrong_reference), load_external_data=False)
        property_value = model.metadata_props.add()
        property_value.key = "test.wrong-reference"
        property_value.value = "different-hash"
        onnx.save(model, str(wrong_reference), save_as_external_data=False)

        with pytest.raises(ValueError) as error:
            evaluate_validation_candidate_from_dataset(
                runtime_artifacts.model_path,
                wrong_reference,
                model_format="onnx-fp32",
                dataset_paths=(),
                split_manifest=tmp_path / "unused-split.json",
            )

        assert "元のFP32 export" in str(error.value)

    def test_fp32_graph_cannot_be_declared_as_int8(self, runtime_artifacts):
        with pytest.raises(ValueError) as error:
            evaluate_validation_candidate(
                runtime_artifacts.model_path,
                model_format="onnx-int8-qdq",
                predictions=passing_predictions(),
                lineage=runtime_artifacts.lineage,
                fp32_reference_artifact_sha256=(
                    hashlib.sha256(
                        runtime_artifacts.fp32_reference_path.read_bytes()
                    ).hexdigest()
                ),
                evaluation_dataset_fingerprint=(
                    runtime_artifacts.lineage.dataset_fingerprint
                ),
                evaluation_split_fingerprint=(
                    runtime_artifacts.lineage.split_fingerprint
                ),
                training_sample_ids=("training-sample-0",),
            )

        assert "Q/DQ graph" in str(error.value)

    def test_external_onnx_data_is_rejected_before_loading(
        self, tmp_path, runtime_artifacts
    ):
        external_model = tmp_path / "external.onnx"
        external_weights = tmp_path / "external-weights.bin"
        model = onnx.load(str(runtime_artifacts.model_path), load_external_data=False)
        onnx.save_model(
            model,
            str(external_model),
            save_as_external_data=True,
            all_tensors_to_one_file=True,
            location=external_weights.name,
            size_threshold=0,
        )
        serialized = onnx.load(str(external_model), load_external_data=False)
        assert any(
            initializer.external_data for initializer in serialized.graph.initializer
        )
        external_weights.unlink()

        with pytest.raises(ValueError, match="external data"):
            evaluate_validation_candidate(
                external_model,
                model_format="onnx-fp32",
                predictions=passing_predictions(),
                lineage=runtime_artifacts.lineage,
                fp32_reference_artifact_sha256=hashlib.sha256(
                    runtime_artifacts.fp32_reference_path.read_bytes()
                ).hexdigest(),
                evaluation_dataset_fingerprint=(
                    runtime_artifacts.lineage.dataset_fingerprint
                ),
                evaluation_split_fingerprint=(
                    runtime_artifacts.lineage.split_fingerprint
                ),
                training_sample_ids=("training-sample-0",),
            )

    def test_benchmark_iteration_contract_is_fixed(self, runtime_artifacts):
        image = np.zeros((32, 32, 3), dtype=np.uint8)
        samples = tuple(
            BenchmarkSample(
                sample_id=f"benchmark-{category}",
                category=category,
                pre_rgb=image,
                post_rgb=image.copy(),
                pixel_per_mm=20.0,
            )
            for category in ("small", "medium", "large", "portrait", "landscape")
        )

        with pytest.raises(ValueError) as error:
            benchmark_model_candidate(
                runtime_artifacts.model_path,
                model_format="onnx-fp32",
                preprocess_schema=canonical_preprocess_schema(),
                lineage=runtime_artifacts.lineage,
                evaluation_dataset_fingerprint=(
                    runtime_artifacts.lineage.dataset_fingerprint
                ),
                evaluation_split_fingerprint=(
                    runtime_artifacts.lineage.split_fingerprint
                ),
                samples=samples,
                power_condition="official 27W supply",
                cooling_condition="active cooler",
                warmup_iterations=9,
                measured_iterations=100,
            )

        assert "warm-upは10回" in str(error.value)

    def test_candidate_binding_rejects_nonstandard_benchmark_iterations(
        self, runtime_artifacts
    ):
        candidate = passing_candidate(
            runtime_artifacts.model_path, runtime_artifacts.lineage
        )

        with pytest.raises(ValueError) as error:
            build_model_candidate_validation(
                candidate.evaluation,
                attrs.evolve(candidate.benchmark, measured_iterations=99),
                candidate.compile_parity,
                candidate.export_parity,
                runtime_artifacts.model_path,
            )

        assert "測定は100回" in str(error.value)

    def test_candidate_binding_rejects_diagnostic_export_without_training_attestation(
        self, tmp_path, runtime_artifacts
    ):
        model_path = tmp_path / "diagnostic-only.onnx"
        shutil.copyfile(runtime_artifacts.model_path, model_path)
        model = onnx.load(str(model_path), load_external_data=False)
        metadata = next(
            item
            for item in model.metadata_props
            if item.key == "pcbasm.paste_volume.export"
        )
        payload = json.loads(metadata.value)
        payload["training_weights_attestation"] = None
        metadata.value = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        onnx.save(model, str(model_path), save_as_external_data=False)
        evaluation = evaluate_validation_candidate(
            model_path,
            model_format="onnx-fp32",
            predictions=passing_predictions(),
            lineage=runtime_artifacts.lineage,
            fp32_reference_artifact_sha256=hashlib.sha256(
                runtime_artifacts.fp32_reference_path.read_bytes()
            ).hexdigest(),
            evaluation_dataset_fingerprint=(
                runtime_artifacts.lineage.dataset_fingerprint
            ),
            evaluation_split_fingerprint=runtime_artifacts.lineage.split_fingerprint,
            training_sample_ids=("training-sample-0", "training-sample-1"),
        )

        with pytest.raises(ValueError, match="formal training weights attestation"):
            build_model_candidate_validation(
                evaluation,
                passing_benchmark(evaluation, model_path),
                passing_compile_parity(model_path, runtime_artifacts.lineage),
                passing_export_parity(evaluation, model_path),
                model_path,
            )

    @pytest.mark.parametrize(
        ("runtime_changes", "message"),
        (
            ({"backend": "eager"}, "inductor/default"),
            ({"mode": "reduce-overhead"}, "inductor/default"),
            ({"graph_break_count": 1}, "gateを通過"),
        ),
    )
    def test_candidate_binding_requires_release_compile_backend_without_graph_breaks(
        self, runtime_artifacts, runtime_changes, message
    ):
        candidate = passing_candidate(
            runtime_artifacts.model_path, runtime_artifacts.lineage
        )
        report = replace(
            candidate.compile_parity,
            runtime=replace(candidate.compile_parity.runtime, **runtime_changes),
            success=runtime_changes.get("graph_break_count", 0) == 0,
        )
        payload = report.to_dict()
        del payload["content_sha256"]
        report = replace(
            report,
            content_sha256="sha256:"
            + hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
        )

        with pytest.raises(ValueError) as error:
            build_model_candidate_validation(
                candidate.evaluation,
                candidate.benchmark,
                report,
                candidate.export_parity,
                runtime_artifacts.model_path,
            )

        assert message in str(error.value)

    def test_frozen_test_requires_at_least_one_three_sample_window(
        self, runtime_artifacts
    ):
        selection = select_model_candidate(
            (
                passing_candidate(
                    runtime_artifacts.model_path, runtime_artifacts.lineage
                ),
            )
        )

        frozen = evaluate_frozen_test_candidate(
            selection,
            predictions=passing_predictions(2, sample_prefix="too-short-frozen-test"),
        )

        assert not frozen.gate_passed
        assert "three_sample_acceptance" in frozen.gate_failures

    def test_five_percent_tie_prefers_the_simpler_candidate(
        self, tmp_path, runtime_artifacts
    ):
        variant = tmp_path / "variant.onnx"
        model = onnx.load(str(runtime_artifacts.model_path))
        property_value = model.metadata_props.add()
        property_value.key = "test.variant"
        property_value.value = "variant"
        onnx.save(model, str(variant))
        evaluation = evaluate_validation_candidate(
            variant,
            model_format="onnx-fp32",
            predictions=passing_predictions(),
            lineage=runtime_artifacts.lineage,
            fp32_reference_artifact_sha256=(
                hashlib.sha256(
                    runtime_artifacts.fp32_reference_path.read_bytes()
                ).hexdigest()
            ),
            evaluation_dataset_fingerprint=(
                runtime_artifacts.lineage.dataset_fingerprint
            ),
            evaluation_split_fingerprint=(runtime_artifacts.lineage.split_fingerprint),
            training_sample_ids=("training-sample-0", "training-sample-1"),
        )
        slower_simple = passing_candidate(
            runtime_artifacts.model_path, runtime_artifacts.lineage
        )
        slower_simple = attrs.evolve(
            slower_simple,
            benchmark=attrs.evolve(
                slower_simple.benchmark,
                p95_latency_ms=104.0,
                category_results=(
                    *slower_simple.benchmark.category_results[:-1],
                    attrs.evolve(
                        slower_simple.benchmark.category_results[-1],
                        p95_latency_ms=104.0,
                    ),
                ),
            ),
            dependency_complexity_rank=0,
        )
        variant_benchmark = passing_benchmark(evaluation, variant)
        variant_benchmark = attrs.evolve(
            variant_benchmark,
            p95_latency_ms=100.0,
            category_results=(
                *variant_benchmark.category_results[:-1],
                attrs.evolve(
                    variant_benchmark.category_results[-1], p95_latency_ms=100.0
                ),
            ),
        )
        faster_complex = build_model_candidate_validation(
            evaluation,
            variant_benchmark,
            slower_simple.compile_parity,
            slower_simple.export_parity,
            variant,
            dependency_complexity_rank=1,
        )

        selection = select_model_candidate((faster_complex, slower_simple))

        assert selection.candidate.candidate_id == slower_simple.candidate_id

    @pytest.mark.parametrize(
        ("field", "different_value"),
        (
            ("source_run_id", "different-run"),
            ("source_checkpoint_sha256", "sha256:" + "7" * 64),
            ("source_checkpoint_role", "different-role"),
            ("training_dataset_fingerprint", "sha256:different-training-dataset"),
            ("training_split_fingerprint", "sha256:different-training-split"),
            ("parent_run_id", "different-parent-run"),
            ("parent_checkpoint_id", "sha256:" + "8" * 64),
            ("dataset_fingerprint", "sha256:different-validation-dataset"),
            ("split_fingerprint", "sha256:different-validation-split"),
            ("fp32_reference_artifact_sha256", "0" * 64),
            ("evaluated_sample_ids", ("different-validation-sample",)),
            ("training_sample_ids", ("different-training-sample",)),
        ),
    )
    def test_selection_requires_shared_lineage_and_evaluation_assignment(
        self, runtime_artifacts, field, different_value
    ):
        candidate = passing_candidate(
            runtime_artifacts.model_path, runtime_artifacts.lineage
        )
        different_evaluation = attrs.evolve(
            candidate.evaluation,
            candidate_id="onnx-fp32:" + "f" * 16,
            **{field: different_value},
        )
        different = attrs.evolve(
            candidate,
            evaluation=different_evaluation,
            benchmark=attrs.evolve(
                candidate.benchmark,
                candidate_id=different_evaluation.candidate_id,
            ),
        )

        with pytest.raises(ValueError) as error:
            select_model_candidate((candidate, different))

        assert field in str(error.value)

    def test_raw_package_and_promotion_reject_hand_entered_coverage(
        self, runtime_artifacts, tmp_path
    ):
        coverage = TrainingCoverage(1.0, 100.0, 32, 1024, 32, 1024)

        with pytest.raises(ValueError) as candidate_error:
            package_model_candidate(
                runtime_artifacts.finalized.selection.candidate.evaluation,
                runtime_artifacts.model_path,
                tmp_path / "raw-candidate",
                preprocess_schema=canonical_preprocess_schema(),
                training_coverage=coverage,
                model_name="test",
                model_version="raw",
            )
        with pytest.raises(ValueError) as promotion_error:
            promote_model_package(
                runtime_artifacts.finalized,
                tmp_path / "raw-promotion",
                preprocess_schema=canonical_preprocess_schema(),
                training_coverage=coverage,
                model_name="test",
                model_version="raw",
            )
        with pytest.raises(ValueError) as activate_error:
            promote_and_activate_model_package(
                runtime_artifacts.finalized,
                tmp_path / "raw-promote-activate",
                tmp_path / "raw-active.json",
                preprocess_schema=canonical_preprocess_schema(),
                training_coverage=coverage,
                model_name="test",
                model_version="raw",
            )

        assert "from_dataset" in str(candidate_error.value)
        assert "from_dataset" in str(promotion_error.value)
        assert "from_dataset" in str(activate_error.value)
        assert not (tmp_path / "raw-candidate").exists()
        assert not (tmp_path / "raw-promotion").exists()
        assert not (tmp_path / "raw-promote-activate").exists()
        assert not (tmp_path / "raw-active.json").exists()

    def test_dataset_backed_promote_and_activate_uses_formal_path(
        self, runtime_artifacts, tmp_path
    ):
        package, pointer = promote_and_activate_model_package_from_dataset(
            runtime_artifacts.finalized,
            tmp_path / "dataset-promoted",
            tmp_path / "active.json",
            dataset_paths=(runtime_artifacts.dataset_path,),
            split_manifest=runtime_artifacts.split_path,
            cross_validation_evidence=runtime_artifacts.cross_validation_evidence,
            model_name="test",
            model_version="dataset-promoted",
        )

        assert pointer.active_model_path == package.package_path
        assert pointer.pointer_path == (tmp_path / "active.json").resolve()
        verify_model_package(package.package_path, require_promoted=True)

    def test_dataset_package_rejects_fabricated_validation_assignment(
        self, runtime_artifacts, tmp_path
    ):
        validation = attrs.evolve(
            runtime_artifacts.finalized.selection.candidate.evaluation,
            evaluated_sample_ids=("fabricated-validation-sample",),
            sample_count=1,
        )
        candidate = attrs.evolve(
            runtime_artifacts.finalized.selection.candidate,
            evaluation=validation,
        )

        with pytest.raises(ValueError) as error:
            package_model_candidate_from_dataset(
                candidate,
                tmp_path / "fabricated-candidate",
                dataset_paths=(runtime_artifacts.dataset_path,),
                split_manifest=runtime_artifacts.split_path,
                model_name="test",
                model_version="fabricated",
            )

        assert "persisted dataset証跡" in str(error.value)
        assert not (tmp_path / "fabricated-candidate").exists()

    def test_promotion_recalculates_numeric_gates(self, runtime_artifacts, tmp_path):
        finalized = runtime_artifacts.finalized
        invalid_validation = attrs.evolve(
            finalized.selection.candidate.evaluation,
            primary_accuracy_score=0.2,
            gate_passed=True,
            gate_failures=(),
        )
        invalid_candidate = attrs.evolve(
            finalized.selection.candidate,
            evaluation=invalid_validation,
        )
        invalid = attrs.evolve(
            finalized,
            selection=attrs.evolve(
                finalized.selection,
                candidate=invalid_candidate,
            ),
        )

        with pytest.raises(ValueError) as error:
            promote_model_package_from_dataset(
                invalid,
                tmp_path / "invalid-package",
                dataset_paths=(runtime_artifacts.dataset_path,),
                split_manifest=runtime_artifacts.split_path,
                cross_validation_evidence=runtime_artifacts.cross_validation_evidence,
                model_name="test",
                model_version="invalid",
            )

        assert "gate数値" in str(error.value)

    def test_failed_final_package_verification_removes_destination(
        self, runtime_artifacts, tmp_path
    ):
        finalized = runtime_artifacts.finalized
        benchmark_id = finalized.selection.candidate.benchmark.benchmark_sample_ids[0]
        invalid_benchmark = attrs.evolve(
            finalized.selection.candidate.benchmark,
            benchmark_sample_ids=(benchmark_id, benchmark_id),
        )
        invalid_candidate = attrs.evolve(
            finalized.selection.candidate, benchmark=invalid_benchmark
        )
        invalid_finalized = attrs.evolve(
            finalized,
            selection=attrs.evolve(finalized.selection, candidate=invalid_candidate),
        )
        destination = tmp_path / "failed-final-verification"

        with pytest.raises(ValueError) as error:
            promote_model_package_from_dataset(
                invalid_finalized,
                destination,
                dataset_paths=(runtime_artifacts.dataset_path,),
                split_manifest=runtime_artifacts.split_path,
                cross_validation_evidence=runtime_artifacts.cross_validation_evidence,
                model_name="test",
                model_version="invalid-evidence",
            )

        assert "sample ID" in str(error.value)
        assert not destination.exists()

    def test_promoted_package_preserves_fine_tune_parent_lineage(
        self, runtime_artifacts
    ):
        verified = verify_model_package(
            runtime_artifacts.package.package_path,
            require_promoted=True,
        )

        assert verified.manifest["lineage"] == runtime_artifacts.lineage.to_dict()
        assert verified.evaluation["validation"]["gate_passed"] is True
        assert verified.evaluation["frozen_test"]["gate_passed"] is True

    def test_raw_promotion_cannot_claim_an_unverified_external_holdout(
        self, runtime_artifacts, tmp_path
    ):
        selection = select_model_candidate(
            (
                passing_candidate(
                    runtime_artifacts.model_path, runtime_artifacts.lineage
                ),
            )
        )
        frozen = evaluate_frozen_test_candidate(
            selection,
            predictions=passing_predictions(sample_prefix="external-holdout"),
            evaluation_dataset_fingerprint="sha256:external-holdout-dataset",
            evaluation_split_fingerprint="sha256:external-holdout-split",
        )
        finalized = finalize_selected_model_candidate(selection, frozen)
        with pytest.raises(ValueError) as error:
            promote_model_package(
                finalized,
                tmp_path / "external-holdout-package",
                preprocess_schema=canonical_preprocess_schema(),
                training_coverage=TrainingCoverage(1.0, 100.0, 32, 1024, 32, 1024),
                model_name="fine-tuned",
                model_version="external-holdout-v1",
            )

        assert "from_dataset" in str(error.value)

    def test_dataset_pipeline_promotes_fine_tune_with_external_holdout(self, tmp_path):
        training_data = tmp_path / "fine-tune-data"
        for index in range(2):
            write_synthetic_session(
                training_data,
                f"fine-tune-session-{index}",
                pad_count=1,
                views_per_pad=6,
            )
        training_composite = resolve_dataset_inputs(roots=(training_data,))
        training_samples = build_sample_index(training_composite)
        training_split = create_session_split(
            training_samples,
            training_composite.composite_fingerprint,
            mode="finetune",
        )
        training_split_path = tmp_path / "fine-tune-split.json"
        save_split_manifest(training_split, training_split_path)

        holdout_data = tmp_path / "holdout-data"
        for index in range(3):
            write_synthetic_session(
                holdout_data,
                f"holdout-session-{index}",
                pad_count=1,
                views_per_pad=3,
            )
        holdout_composite = resolve_dataset_inputs(roots=(holdout_data,))
        holdout_samples = build_sample_index(holdout_composite)
        holdout_split = create_session_split(
            holdout_samples,
            holdout_composite.composite_fingerprint,
            mode="base",
        )
        holdout_split_path = tmp_path / "holdout-split.json"
        save_split_manifest(holdout_split, holdout_split_path)

        lineage = ArtifactLineage(
            source_run_id="fine-tune-run",
            source_checkpoint_sha256="sha256:" + "8" * 64,
            dataset_fingerprint=training_composite.composite_fingerprint,
            split_fingerprint=training_split.split_fingerprint,
            training_protocol_fingerprint="sha256:" + "a" * 64,
            parent_run_id="base-run",
            parent_checkpoint_id="sha256:" + "9" * 64,
        )
        weights = write_test_weights(tmp_path / "fine-tuned-weights.pt", lineage)
        fp32_reference_path = export_onnx_model_from_formal_weights(
            formal_test_weights(weights, lineage),
            tmp_path / "fine-tuned.onnx",
            verification_shapes=((32, 32), (64, 96)),
        ).model_path
        model_path = optimize_model(
            fp32_reference_path,
            tmp_path / "fine-tuned-optimized",
            calibration_data=(training_data,),
            split_manifest=training_split_path,
        ).optimized_fp32_path
        validation = evaluate_validation_candidate_from_dataset(
            model_path,
            fp32_reference_path,
            model_format="onnx-fp32",
            dataset_paths=(training_data,),
            split_manifest=training_split_path,
        )
        candidate = build_model_candidate_validation(
            validation,
            passing_benchmark(validation, model_path),
            passing_compile_parity(model_path, lineage),
            passing_export_parity(validation, model_path),
            model_path,
        )
        selection = select_model_candidate((candidate,))

        with pytest.raises(ValueError):
            evaluate_frozen_test_candidate_from_dataset(
                selection,
                fp32_reference_path,
                dataset_paths=(holdout_data,),
                split_manifest=holdout_split_path,
            )
        frozen = evaluate_frozen_test_candidate_from_dataset(
            selection,
            fp32_reference_path,
            dataset_paths=(holdout_data,),
            split_manifest=holdout_split_path,
            allow_external_split=True,
        )
        finalized = finalize_selected_model_candidate(selection, frozen)
        cross_validation_evidence = passing_cross_validation_evidence(
            training_data, lineage, tmp_path / "fine-tune-cross-validation"
        )
        package = promote_model_package_from_dataset(
            finalized,
            tmp_path / "dataset-backed-package",
            dataset_paths=(training_data,),
            split_manifest=training_split_path,
            cross_validation_evidence=cross_validation_evidence,
            frozen_test_dataset_paths=(holdout_data,),
            frozen_test_split_manifest=holdout_split_path,
            model_name="fine-tuned",
            model_version="external-holdout-v2",
        )

        verified = verify_model_package(package.package_path, require_promoted=True)
        assert verified.manifest["lineage"]["split_fingerprint"] == (
            training_split.split_fingerprint
        )
        assert (
            verified.manifest["release_evaluation"]["frozen_test"]["split_fingerprint"]
            == holdout_split.split_fingerprint
        )

    @pytest.mark.parametrize(
        "pointer_name",
        (".", "active-model.json", "manifest.json", "model.onnx", "SHA256SUMS"),
    )
    def test_direct_activation_never_writes_inside_the_model_package(
        self, tmp_path, runtime_artifacts, pointer_name
    ):
        package = tmp_path / "immutable-package"
        shutil.copytree(runtime_artifacts.package.package_path, package)
        before = {
            path.relative_to(package): path.read_bytes()
            for path in package.iterdir()
            if path.is_file()
        }

        with pytest.raises(ValueError) as error:
            activate_model_package(package, package / pointer_name)

        assert "packageの外" in str(error.value)
        after = {
            path.relative_to(package): path.read_bytes()
            for path in package.iterdir()
            if path.is_file()
        }
        assert after == before
        verify_model_package(package, require_promoted=True)


class TestReleasePipelineOrdering:
    def test_frozen_test_and_promotion_are_unreachable_before_selection(self, tmp_path):
        pipeline = PasteVolumeReleasePipeline(
            weights_path=tmp_path / "weights.pt",
            workspace=tmp_path / "workspace",
        )

        with pytest.raises(RuntimeError):
            pipeline.evaluate_frozen_test(passing_predictions())
        with pytest.raises(RuntimeError):
            pipeline.promote()

    def test_high_level_pipeline_rejects_raw_frozen_predictions(
        self, tmp_path, runtime_artifacts
    ):
        pipeline = PasteVolumeReleasePipeline(
            weights_path=runtime_artifacts.directory / "export" / "weights.pt",
            workspace=tmp_path / "workspace",
        )
        pipeline.export(verification_shapes=((32, 32),))
        pipeline.optimize(
            calibration_data=(runtime_artifacts.dataset_path,),
            split_manifest=runtime_artifacts.split_path,
            calibration_sample_limit=1,
        )
        pipeline.select((runtime_artifacts.finalized.selection.candidate,))

        with pytest.raises(ValueError) as error:
            pipeline.evaluate_frozen_test(
                passing_predictions(sample_prefix="unpersisted-frozen-test")
            )

        assert "from_dataset" in str(error.value)
        frozen = pipeline.evaluate_frozen_test_from_dataset(
            runtime_artifacts.fp32_reference_path,
            dataset_paths=(runtime_artifacts.dataset_path,),
            split_manifest=runtime_artifacts.split_path,
        )
        assert frozen.evaluated_sample_ids == (
            runtime_artifacts.finalized.frozen_test.evaluated_sample_ids
        )
