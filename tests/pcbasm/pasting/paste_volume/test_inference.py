from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path

import numpy as np
import pytest

from pcbasm.pasting.paste_volume.export import (
    activate_model_package,
    package_model_candidate_from_dataset,
    rollback_active_model,
)
from pcbasm.pasting.paste_volume.inference import (
    PasteVolumeModelPackageError,
    PasteVolumePrediction,
    load_active_paste_volume_estimator,
    load_candidate_paste_volume_estimator,
    load_configured_paste_volume_estimator,
    load_paste_volume_estimator,
)
from tests.pcbasm.pasting.paste_volume.support_runtime import (
    promote_test_model,
    runtime_artifacts,
)


def _rgb_pair(height: int = 64, width: int = 80) -> tuple[np.ndarray, np.ndarray]:
    rows, columns = np.indices((height, width))
    pre = np.stack(
        (
            (rows + columns) % 256,
            (rows * 2 + columns) % 256,
            (rows + columns * 2) % 256,
        ),
        axis=-1,
    ).astype(np.uint8)
    post = pre.copy()
    post[height // 4 : height // 2, width // 4 : width // 2] = 255
    return pre, post


def _rewrite_package_checksums(package: Path) -> None:
    filenames = (
        "manifest.json",
        "model.onnx",
        "preprocess.json",
        "evaluation.json",
    )
    sums = {
        name: hashlib.sha256((package / name).read_bytes()).hexdigest()
        for name in filenames
    }
    (package / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items())),
        encoding="utf-8",
    )


class TestPasteVolumePrediction:
    def test_json_representation_preserves_domain_nans_as_null(self):
        prediction = PasteVolumePrediction(
            mean_volume_ul=math.nan,
            std_volume_ul=math.nan,
            relative_std=math.nan,
            accepted=False,
            rejection_reason="runtime failure",
            model_id="test-model",
        )

        payload = prediction.to_dict()

        assert math.isnan(prediction.mean_volume_ul)
        assert payload["mean_volume_ul"] is None
        assert payload["std_volume_ul"] is None
        assert payload["relative_std"] is None
        json.dumps(payload, allow_nan=False)


class TestPasteVolumeEstimator:
    def test_promoted_package_runs_real_onnx_runtime_end_to_end(
        self, runtime_artifacts
    ):
        estimator = load_paste_volume_estimator(runtime_artifacts.package.package_path)
        pre, post = _rgb_pair()

        first = estimator.predict(pre, post, pixel_per_mm=20.0)
        second = estimator.predict(pre, post, pixel_per_mm=20.0)

        assert first == second
        assert first.accepted
        assert first.mean_volume_ul == pytest.approx(0.11, rel=1e-5)
        assert first.std_volume_ul == pytest.approx(1.0)
        assert first.model_id == estimator.model_info.model_id
        assert estimator.model_info.model_checksum == (
            runtime_artifacts.package.model_artifact_sha256
        )

    def test_training_coverage_rejection_is_nonfatal(self, runtime_artifacts):
        estimator = load_paste_volume_estimator(runtime_artifacts.package.package_path)
        pre, post = _rgb_pair()

        prediction = estimator.predict(pre, post, pixel_per_mm=101.0)

        assert not prediction.accepted
        assert "training coverage外" in str(prediction.rejection_reason)
        assert np.isfinite(prediction.mean_volume_ul)

    @pytest.mark.parametrize(
        ("mutation", "message"),
        [
            (lambda image: image.astype(np.float32), "uint8"),
            (lambda image: image[:, :, :2], "3-channel"),
            (lambda image: image[0], "3-channel"),
        ],
    )
    def test_invalid_public_rgb_contract_is_rejected(
        self, runtime_artifacts, mutation, message
    ):
        estimator = load_paste_volume_estimator(runtime_artifacts.package.package_path)
        pre, post = _rgb_pair()

        with pytest.raises(ValueError) as error:
            estimator.predict(mutation(pre), post, pixel_per_mm=20.0)

        assert message in str(error.value)


class TestStrictModelPackages:
    def test_candidate_is_loadable_only_through_candidate_loader(
        self, tmp_path, runtime_artifacts
    ):
        validation = runtime_artifacts.finalized.selection.candidate
        candidate = package_model_candidate_from_dataset(
            validation,
            tmp_path / "candidate",
            dataset_paths=(runtime_artifacts.dataset_path,),
            split_manifest=runtime_artifacts.split_path,
            model_name="test",
            model_version="candidate",
        )

        with pytest.raises(PasteVolumeModelPackageError) as error:
            load_paste_volume_estimator(candidate.package_path)

        assert "candidate" in str(error.value)
        estimator = load_candidate_paste_volume_estimator(candidate.package_path)
        assert estimator.model_info.model_id == candidate.model_id

    def test_corrupted_artifact_is_rejected_before_runtime_initialization(
        self, tmp_path, runtime_artifacts
    ):
        corrupted = tmp_path / "corrupted"
        shutil.copytree(runtime_artifacts.package.package_path, corrupted)
        with (corrupted / "model.onnx").open("ab") as stream:
            stream.write(b"corrupt")

        with pytest.raises(PasteVolumeModelPackageError) as error:
            load_paste_volume_estimator(corrupted)

        assert "checksum" in str(error.value)

    def test_unknown_preprocess_key_is_rejected_even_with_updated_checksums(
        self, tmp_path, runtime_artifacts
    ):
        package = tmp_path / "unknown-preprocess"
        shutil.copytree(runtime_artifacts.package.package_path, package)
        preprocess_path = package / "preprocess.json"
        preprocess = json.loads(preprocess_path.read_text(encoding="utf-8"))
        preprocess["dataset_mean"] = [0.5, 0.5, 0.5]
        preprocess_path.write_text(json.dumps(preprocess), encoding="utf-8")

        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        preprocess_hash = hashlib.sha256(preprocess_path.read_bytes()).hexdigest()
        canonical = json.dumps(
            preprocess,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        manifest["files"]["preprocess.json"] = preprocess_hash
        manifest["preprocess_schema_sha256"] = hashlib.sha256(canonical).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        sums = {
            name: hashlib.sha256((package / name).read_bytes()).hexdigest()
            for name in (
                "manifest.json",
                "model.onnx",
                "preprocess.json",
                "evaluation.json",
            )
        }
        (package / "SHA256SUMS").write_text(
            "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items())),
            encoding="utf-8",
        )

        with pytest.raises(PasteVolumeModelPackageError) as error:
            load_paste_volume_estimator(package)

        assert "key集合" in str(error.value)

    def test_promoted_manifest_exposes_calibration_and_build_provenance(
        self, runtime_artifacts
    ):
        manifest = json.loads(
            (runtime_artifacts.package.package_path / "manifest.json").read_text(
                encoding="utf-8"
            )
        )

        assert manifest["uncertainty_calibration"] == {
            "kind": "log-variance-offset",
            "log_variance_offset": 0.0,
        }
        assert manifest["provenance"]["export"]["api"] == "torch.onnx.export"
        assert manifest["provenance"]["export"]["mode"] == "dynamo"
        assert manifest["provenance"]["export"]["torch_version"]
        assert manifest["provenance"]["export"]["onnx_version"]
        assert manifest["provenance"]["artifact"]["tool"] == "onnxruntime"
        assert manifest["provenance"]["artifact"]["version"]
        assert [item["dimension"] for item in manifest["cross_validation"]] == [
            "machine",
            "paste_lot",
            "nozzle",
        ]
        assert all(
            set(item)
            == {
                "dimension",
                "report_fingerprint",
                "report_artifact_sha256",
                "summary_run_id",
                "tracking_uri_sha256",
                "attestation_sha256",
            }
            for item in manifest["cross_validation"]
        )

    def test_load_rejects_cross_validation_tamper_after_package_rehash(
        self, tmp_path, runtime_artifacts
    ):
        package = tmp_path / "tampered-cross-validation"
        shutil.copytree(runtime_artifacts.package.package_path, package)
        evaluation_path = package / "evaluation.json"
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
        evaluation["cross_validation"][0]["report"]["protocol_fingerprint"] = (
            "sha256:" + "0" * 64
        )
        evaluation_path.write_text(
            json.dumps(evaluation, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["evaluation.json"] = hashlib.sha256(
            evaluation_path.read_bytes()
        ).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _rewrite_package_checksums(package)

        with pytest.raises(PasteVolumeModelPackageError, match="cross-validation"):
            load_paste_volume_estimator(package)

    def test_load_rejects_missing_export_parity_boundary_shape_after_rehash(
        self, tmp_path, runtime_artifacts
    ):
        package = tmp_path / "tampered-export-parity-shape"
        shutil.copytree(runtime_artifacts.package.package_path, package)
        evaluation_path = package / "evaluation.json"
        manifest_path = package / "manifest.json"
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for report in (
            evaluation["export_parity"],
            manifest["export_parity"],
        ):
            report["shape_results"] = report["shape_results"][:-1]
            content = {
                key: value for key, value in report.items() if key != "content_sha256"
            }
            report["content_sha256"] = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        content,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()
            )
        evaluation_path.write_text(
            json.dumps(evaluation, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest["files"]["evaluation.json"] = hashlib.sha256(
            evaluation_path.read_bytes()
        ).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _rewrite_package_checksums(package)

        with pytest.raises(PasteVolumeModelPackageError, match="dynamic shape"):
            load_paste_volume_estimator(package)

    def test_load_rejects_training_attestation_lineage_tamper_after_rehash(
        self, tmp_path, runtime_artifacts
    ):
        package = tmp_path / "tampered-training-attestation"
        shutil.copytree(runtime_artifacts.package.package_path, package)
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["training_weights_attestation"]["run_id"] = "different-run"
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _rewrite_package_checksums(package)

        with pytest.raises(PasteVolumeModelPackageError, match="attestation"):
            load_paste_volume_estimator(package)

    @pytest.mark.parametrize(
        ("field", "message"),
        (
            ("calibration", "calibration offset"),
            ("exporter", "export provenance"),
        ),
    )
    def test_load_rejects_manifest_provenance_not_bound_to_onnx_metadata(
        self, tmp_path, runtime_artifacts, field, message
    ):
        package = tmp_path / f"tampered-{field}"
        shutil.copytree(runtime_artifacts.package.package_path, package)
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if field == "calibration":
            manifest["uncertainty_calibration"]["log_variance_offset"] = 1.0
        else:
            manifest["provenance"]["export"]["torch_version"] = "0.0-tampered"
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _rewrite_package_checksums(package)

        with pytest.raises(PasteVolumeModelPackageError) as error:
            load_paste_volume_estimator(package)

        assert message in str(error.value)


class TestActiveModelPointer:
    def test_atomic_switch_keeps_previous_and_manual_rollback(
        self, tmp_path, runtime_artifacts
    ):
        second = promote_test_model(
            runtime_artifacts.model_path,
            runtime_artifacts.lineage,
            tmp_path / "promoted-v2",
            version="test-v2",
        )
        pointer_path = tmp_path / "active-model.json"
        first_pointer = activate_model_package(
            runtime_artifacts.package.package_path,
            pointer_path,
        )
        second_pointer = activate_model_package(second.package_path, pointer_path)

        assert first_pointer.previous_model_path is None
        assert second_pointer.previous_model_path == (
            runtime_artifacts.package.package_path
        )
        active = load_active_paste_volume_estimator(pointer_path)
        assert active.model_info.model_version == "test-v2"

        rolled_back = rollback_active_model(pointer_path)

        assert rolled_back.active_model_path == runtime_artifacts.package.package_path
        configured = load_configured_paste_volume_estimator(pointer_path)
        assert configured.model_info.model_version == "test-v1"

    def test_pointer_checksum_tampering_is_rejected(self, tmp_path, runtime_artifacts):
        pointer_path = tmp_path / "active-model.json"
        activate_model_package(runtime_artifacts.package.package_path, pointer_path)
        value = json.loads(pointer_path.read_text(encoding="utf-8"))
        value["active_model_sha256"] = "0" * 64
        pointer_path.write_text(json.dumps(value), encoding="utf-8")

        with pytest.raises(PasteVolumeModelPackageError) as error:
            load_active_paste_volume_estimator(pointer_path)

        assert "checksum" in str(error.value)

    @pytest.mark.parametrize("pointer_name", ["active-model.json", "manifest.json"])
    def test_pointer_inside_target_package_is_rejected_before_package_read(
        self, tmp_path, runtime_artifacts, pointer_name
    ):
        package = tmp_path / "promoted"
        shutil.copytree(runtime_artifacts.package.package_path, package)
        pointer_path = package / pointer_name
        pointer_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "active_model_path": str(package),
                    "active_model_id": runtime_artifacts.package.model_id,
                    "active_model_sha256": (
                        runtime_artifacts.package.model_artifact_sha256
                    ),
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(PasteVolumeModelPackageError) as error:
            load_active_paste_volume_estimator(pointer_path)

        assert "packageの外" in str(error.value)
