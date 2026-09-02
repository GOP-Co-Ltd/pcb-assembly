from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from base64 import b64encode
from pathlib import Path

import mlflow
import numpy as np
import pytest
from mlflow import MlflowClient
from PIL import Image

from pcbasm.cli.paste_volume import main
from pcbasm.pasting.paste_volume.compile_parity import (
    compile_parity_report_from_dict,
    load_compile_parity_report,
    write_compile_parity_report,
)
from pcbasm.pasting.paste_volume.data import (
    build_sample_index,
    create_session_split,
    resolve_dataset_inputs,
    save_split_manifest,
)
from pcbasm.pasting.paste_volume.export import (
    evaluate_frozen_test_candidate_from_dataset,
    export_parity_report_from_dict,
    finalize_selected_model_candidate,
    load_export_parity_report,
    load_model_candidate_validation,
    save_candidate_evaluation,
    save_export_parity_report,
    save_finalized_model_candidate,
    save_model_benchmark_result,
    save_selected_model_candidate,
    select_model_candidate,
)
from tests.pcbasm.pasting.paste_volume.support_data import write_synthetic_session
from tests.pcbasm.pasting.paste_volume.support_runtime import (
    passing_candidate,
    runtime_artifacts,
    write_test_weights,
)


class _MLflowServer:
    def __init__(self, root: Path) -> None:
        self._root = root
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            self._port = int(server.getsockname()[1])
        self.uri = f"http://127.0.0.1:{self._port}"
        self._process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._process = subprocess.Popen(
            (
                sys.executable,
                "-m",
                "mlflow",
                "server",
                "--backend-store-uri",
                f"sqlite:///{self._root / 'mlflow.db'}",
                "--default-artifact-root",
                (self._root / "artifacts").as_uri(),
                "--host",
                "127.0.0.1",
                "--port",
                str(self._port),
                "--workers",
                "1",
            ),
            cwd=self._root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        client = MlflowClient(tracking_uri=self.uri)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("MLflow server exited during startup")
            try:
                client.search_experiments(max_results=1)
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("MLflow server did not become ready")

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        self._process.wait(timeout=10)
        self._process = None


@pytest.fixture(scope="module")
def mlflow_server(tmp_path_factory: pytest.TempPathFactory, runtime_artifacts):
    # Build the shared ONNX fixture before starting the server to keep peak RSS low.
    assert runtime_artifacts.model_path.is_file()
    server = _MLflowServer(tmp_path_factory.mktemp("paste-volume-cli-mlflow"))
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _sha256_file(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _path_fingerprint(path: Path) -> str:
    if path.is_file():
        return _sha256_file(path)
    digest = hashlib.sha256()
    for item in sorted(
        (candidate for candidate in path.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(path).as_posix(),
    ):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(item).encode("ascii"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def _attestation_path(output: Path) -> Path:
    return output.with_name(f"{output.name}.mlflow-success.json")


def _attest_existing_output(
    output: Path, server: _MLflowServer, *, run_kind: str = "test-prerequisite"
) -> Path:
    resolved = output.resolve(strict=True)
    mlflow.set_tracking_uri(server.uri)
    experiment = mlflow.set_experiment("paste-volume-cli-prerequisite")
    with mlflow.start_run(experiment_id=experiment.experiment_id) as run:
        run_id = run.info.run_id
        training_tags: dict[str, str] = {}
        if run_kind in ("base-train", "finetune"):
            import torch

            from pcbasm.pasting.paste_volume.training import load_model_weights

            weights = load_model_weights(resolved)
            weights["source_run_id"] = run_id
            weights["run_kind"] = run_kind
            torch.save(weights, resolved)
            dataset_fingerprint = weights["dataset_fingerprint"]
            split_fingerprint = weights["split_fingerprint"]
            training_protocol_fingerprint = weights["training_protocol_fingerprint"]
            assert isinstance(dataset_fingerprint, str)
            assert isinstance(split_fingerprint, str)
            assert isinstance(training_protocol_fingerprint, str)
            training_tags = {
                "run_kind": run_kind,
                "dataset_fingerprint": dataset_fingerprint,
                "split_fingerprint": split_fingerprint,
                "training_protocol_fingerprint": training_protocol_fingerprint,
            }
        fingerprint = _path_fingerprint(resolved)
        payload = {
            "kind": "pcbasm-paste-volume-formal-operation-success",
            "schema_version": 1,
            "status": "FINISHED",
            "run_id": run_id,
            "run_kind": run_kind,
            "tracking_uri_sha256": (
                "sha256:" + hashlib.sha256(server.uri.encode("utf-8")).hexdigest()
            ),
            "output_path": str(resolved),
            "output_kind": "directory" if resolved.is_dir() else "file",
            "output_fingerprint": fingerprint,
        }
        mlflow.set_tags(
            {
                "formal_output_path": str(resolved),
                "formal_output_fingerprint": fingerprint,
                "formal_operation_run_kind": run_kind,
                **training_tags,
            }
        )
        if resolved.is_dir():
            mlflow.log_artifacts(str(resolved), artifact_path="outputs")
        else:
            mlflow.log_artifact(str(resolved), artifact_path="outputs")
        with tempfile.TemporaryDirectory(
            prefix="pcbasm-test-formal-attestation-"
        ) as directory:
            remote = Path(directory) / "formal-success.json"
            remote.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            mlflow.log_artifact(str(remote), artifact_path="operation")
    local = _attestation_path(resolved)
    local.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return local


def _compile_parity_run_for_output(output: Path, server: _MLflowServer):
    client = MlflowClient(tracking_uri=server.uri)
    experiment = client.get_experiment_by_name("paste-volume")
    assert experiment is not None
    matches = [
        run
        for run in client.search_runs(
            [experiment.experiment_id],
            filter_string="tags.operation = 'compile-parity'",
        )
        if run.data.params.get("arg.output") == json.dumps(str(output))
    ]
    assert len(matches) == 1
    return matches[0]


def _compile_report_with_graph_break(report):
    payload = report.to_dict()
    payload["success"] = False
    payload["runtime"]["graph_break_count"] = 1
    del payload["content_sha256"]
    payload["content_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    )
    return compile_parity_report_from_dict(payload)


def _failing_export_parity_report(report):
    payload = report.to_dict()
    sample_results = payload["sample_results"]
    assert isinstance(sample_results, list)
    first = sample_results[0]
    assert isinstance(first, dict)
    mean_tolerance = first["mean_tolerance_ul"]
    assert isinstance(mean_tolerance, float)
    first["mean_absolute_error_ul"] = mean_tolerance * 2.0
    first["passed"] = False
    payload["maximum_mean_absolute_error_ul"] = mean_tolerance * 2.0
    payload["success"] = False
    del payload["content_sha256"]
    payload["content_sha256"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    )
    return export_parity_report_from_dict(payload)


def _export_parity_run_for_output(output: Path, server: _MLflowServer):
    client = MlflowClient(tracking_uri=server.uri)
    experiment = client.get_experiment_by_name("paste-volume")
    assert experiment is not None
    matches = [
        run
        for run in client.search_runs(
            [experiment.experiment_id],
            filter_string="tags.operation = 'export-parity'",
        )
        if run.data.params.get("arg.output") == json.dumps(str(output))
    ]
    assert len(matches) == 1
    return matches[0]


def _write_image(path: Path, *, mode: str, image_format: str) -> None:
    channels = {"L": None, "RGB": 3, "RGBA": 4}[mode]
    shape = (64, 80) if channels is None else (64, 80, channels)
    image = np.arange(np.prod(shape), dtype=np.uint8).reshape(shape)
    Image.fromarray(image).save(path, format=image_format)


def _write_external_split(root: Path) -> Path:
    root.mkdir(parents=True)
    for index in range(3):
        write_synthetic_session(
            root,
            f"external-{index}",
            pad_count=1,
            views_per_pad=3,
            content_seed=100 + index,
        )
    composite = resolve_dataset_inputs(roots=(root,))
    samples = build_sample_index(composite)
    split = create_session_split(samples, composite.composite_fingerprint, seed=23)
    split_path = root.parent / "external-split.json"
    save_split_manifest(split, split_path)
    return split_path


def _write_formal_selection(
    path: Path, model_path: Path, runtime_artifacts, server: _MLflowServer
) -> Path:
    selection = select_model_candidate(
        (passing_candidate(model_path, runtime_artifacts.lineage),)
    )
    save_selected_model_candidate(path, selection)
    _attest_existing_output(path, server, run_kind="evaluate")
    return path


@pytest.fixture(scope="module")
def formal_compile_parity(tmp_path_factory, runtime_artifacts, mlflow_server):
    output = tmp_path_factory.mktemp("formal-compile-parity")
    report = runtime_artifacts.finalized.selection.candidate.compile_parity
    report_path = write_compile_parity_report(
        report,
        output / "compile-parity.json",
    )
    _attest_existing_output(output, mlflow_server, run_kind="compile-parity")
    return output, report_path, report


class TestPasteVolumeCli:
    def test_module_import_does_not_load_optional_ml_dependencies(self):
        project_root = Path(__file__).parents[3]
        result = subprocess.run(
            (
                sys.executable,
                "-c",
                "import sys; import pcbasm.cli.paste_volume; "
                "forbidden={'torch','torchvision','hydra','mlflow','onnxruntime'}; "
                "loaded=forbidden.intersection(sys.modules); "
                "assert not loaded, sorted(loaded)",
            ),
            cwd=project_root,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr

    def test_missing_dependency_guidance_preserves_locked_environment(self):
        project_root = Path(__file__).parents[3]
        result = subprocess.run(
            (
                sys.executable,
                "-c",
                "import sys; "
                "from pcbasm.cli.paste_volume import main; "
                "exec('class BlockHydra:\\n'"
                "     '    def find_spec(self, fullname, path=None, target=None):\\n'"
                '     \'        if fullname == \\"hydra\\" or '
                'fullname.startswith(\\"hydra.\\"):\\n\''
                "     '            raise ModuleNotFoundError("
                '\\"blocked for CLI contract test\\", name=\\"hydra\\")\'); '
                "sys.meta_path.insert(0, BlockHydra()); "
                "raise SystemExit(main(['smoke']))",
            ),
            cwd=project_root,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 2
        assert "uv sync --locked --group ml-train" in result.stderr
        assert "uv sync --group ml-train" not in result.stderr

    def test_dataset_merge_rejects_duplicate_source_ids(self, tmp_path, capsys):
        exit_code = main(
            [
                "dataset",
                "merge",
                "--source",
                f"duplicate={tmp_path}",
                "--source",
                f"duplicate={tmp_path}",
                "--output",
                str(tmp_path / "composite.json"),
            ]
        )

        assert exit_code == 2
        assert "source IDs must be unique" in capsys.readouterr().err

    def test_optimize_help_requires_persisted_split_and_tracking(self):
        result = subprocess.run(
            (
                sys.executable,
                "-m",
                "pcbasm.cli.paste_volume",
                "optimize",
                "--help",
            ),
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert "--split-manifest" in result.stdout
        assert "--tracking-uri" in result.stdout

    @pytest.mark.parametrize(
        ("arguments", "required_options"),
        [
            (
                ("compile-parity", "--help"),
                (
                    "--data",
                    "--split-manifest",
                    "--output",
                    "--device",
                    "--tracking-uri",
                ),
            ),
            (
                ("export-parity", "--help"),
                (
                    "--fp32-model",
                    "--data",
                    "--split-manifest",
                    "--output",
                    "--tracking-uri",
                ),
            ),
            (
                ("candidate", "evaluate", "--help"),
                ("--split-manifest", "--fp32-reference", "--tracking-uri"),
            ),
            (
                ("candidate", "frozen-test", "--help"),
                (
                    "--split-manifest",
                    "--fp32-reference",
                    "--allow-external-split",
                    "--tracking-uri",
                ),
            ),
            (
                ("candidate", "package", "--help"),
                ("--data", "--split-manifest", "--tracking-uri"),
            ),
            (
                ("candidate", "bind", "--help"),
                (
                    "--evaluation",
                    "--benchmark",
                    "--compile-parity",
                    "--export-parity",
                    "--tracking-uri",
                ),
            ),
            (
                ("benchmark", "--help"),
                (
                    "--split-manifest",
                    "--power-condition",
                    "--cooling-condition",
                    "--tracking-uri",
                ),
            ),
            (
                ("promote", "--help"),
                (
                    "--data",
                    "--split-manifest",
                    "--frozen-test-data",
                    "--frozen-test-split-manifest",
                    "--cross-validation",
                    "--tracking-uri",
                    "--model-version",
                ),
            ),
            (("activate", "--help"), ("--pointer",)),
        ],
    )
    def test_release_commands_expose_required_safety_inputs(
        self, arguments, required_options
    ):
        result = subprocess.run(
            (sys.executable, "-m", "pcbasm.cli.paste_volume", *arguments),
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        for option in required_options:
            assert option in result.stdout

    def test_compile_parity_help_documents_release_defaults(self):
        result = subprocess.run(
            (
                sys.executable,
                "-m",
                "pcbasm.cli.paste_volume",
                "compile-parity",
                "--help",
            ),
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert "backend=inductor" in result.stdout
        assert "mode=default" in result.stdout
        assert "default: auto" in result.stdout
        assert "--backend" not in result.stdout
        assert "--mode" not in result.stdout

    @pytest.mark.parametrize("option", ("--backend", "--mode"))
    def test_compile_parity_rejects_backend_or_mode_override(self, option):
        result = subprocess.run(
            (
                sys.executable,
                "-m",
                "pcbasm.cli.paste_volume",
                "compile-parity",
                "weights.pt",
                "--data",
                "dataset",
                "--split-manifest",
                "split.json",
                "--output",
                "report.json",
                option,
                "eager",
            ),
            capture_output=True,
            text=True,
        )

        assert result.returncode == 2
        assert "unrecognized arguments" in result.stderr

    @pytest.mark.parametrize(
        ("arguments", "required_option"),
        [
            (
                (
                    "compile-parity",
                    "weights.pt",
                    "--data",
                    "dataset",
                    "--split-manifest",
                    "split.json",
                ),
                "--output",
            ),
            (
                (
                    "candidate",
                    "bind",
                    "model.onnx",
                    "--evaluation",
                    "evaluation.json",
                    "--benchmark",
                    "benchmark.json",
                    "--output",
                    "candidate.json",
                ),
                "--compile-parity",
            ),
            (
                (
                    "candidate",
                    "bind",
                    "model.onnx",
                    "--evaluation",
                    "evaluation.json",
                    "--benchmark",
                    "benchmark.json",
                    "--compile-parity",
                    "compile-parity.json",
                    "--output",
                    "candidate.json",
                ),
                "--export-parity",
            ),
            (
                (
                    "export-parity",
                    "weights.pt",
                    "--data",
                    "dataset",
                    "--split-manifest",
                    "split.json",
                    "--output",
                    "export-parity.json",
                ),
                "--fp32-model",
            ),
            (
                (
                    "benchmark",
                    "model.onnx",
                    "--model-format",
                    "onnx-fp32",
                    "--data",
                    "dataset",
                    "--split-manifest",
                    "split.json",
                    "--output",
                    "benchmark.json",
                    "--cooling-condition",
                    "active-fan",
                ),
                "--power-condition",
            ),
            (
                (
                    "benchmark",
                    "model.onnx",
                    "--model-format",
                    "onnx-fp32",
                    "--data",
                    "dataset",
                    "--split-manifest",
                    "split.json",
                    "--output",
                    "benchmark.json",
                    "--power-condition",
                    "official-27W-supply",
                ),
                "--cooling-condition",
            ),
            (
                (
                    "promote",
                    "finalized.json",
                    "--data",
                    "dataset",
                    "--split-manifest",
                    "split.json",
                    "--output",
                    "promoted",
                    "--model-name",
                    "paste-volume",
                    "--model-version",
                    "v1",
                ),
                "--cross-validation",
            ),
        ],
    )
    def test_release_commands_reject_missing_required_evidence_option(
        self, arguments, required_option
    ):
        result = subprocess.run(
            (sys.executable, "-m", "pcbasm.cli.paste_volume", *arguments),
            capture_output=True,
            text=True,
        )

        assert result.returncode == 2
        assert (
            f"the following arguments are required: {required_option}" in result.stderr
        )

    @pytest.mark.parametrize(
        "arguments",
        [
            ("candidate", "package", "--help"),
            ("promote", "--help"),
        ],
    )
    def test_package_and_promotion_do_not_accept_manual_coverage(self, arguments):
        result = subprocess.run(
            (sys.executable, "-m", "pcbasm.cli.paste_volume", *arguments),
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert "--pixel-per-mm-min" not in result.stdout
        assert "--height-min" not in result.stdout
        assert "--width-min" not in result.stdout

    def test_candidate_package_accepts_only_a_formally_bound_candidate(self):
        result = subprocess.run(
            (
                sys.executable,
                "-m",
                "pcbasm.cli.paste_volume",
                "candidate",
                "package",
                "--help",
            ),
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert "\n  candidate\n" in result.stdout
        assert "--evaluation" not in result.stdout

    @pytest.mark.parametrize("command", ("infer", "activate"))
    def test_runtime_commands_do_not_require_mlflow_tracking(self, command):
        result = subprocess.run(
            (sys.executable, "-m", "pcbasm.cli.paste_volume", command, "--help"),
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert "--tracking-uri" not in result.stdout

    @pytest.mark.parametrize(
        "external_arguments",
        (
            ("--frozen-test-data", "external-data"),
            ("--frozen-test-split-manifest", "external-split.json"),
        ),
    )
    def test_promote_rejects_partial_external_frozen_test_pair(
        self, tmp_path, capsys, external_arguments
    ):
        output = tmp_path / "promoted"

        exit_code = main(
            [
                "promote",
                str(tmp_path / "finalized.json"),
                "--output",
                str(output),
                "--data",
                str(tmp_path / "training-data"),
                "--split-manifest",
                str(tmp_path / "training-split.json"),
                "--model-name",
                "paste-volume",
                "--model-version",
                "test-v1",
                "--cross-validation",
                "machine.json",
                "--cross-validation",
                "paste-lot.json",
                "--cross-validation",
                "nozzle.json",
                *external_arguments,
            ]
        )

        assert exit_code == 2
        assert "両方指定" in capsys.readouterr().err
        assert not output.exists()

    @pytest.mark.parametrize("report_count", (1, 2, 4))
    def test_promote_requires_exactly_three_cross_validation_reports(
        self, tmp_path, capsys, report_count
    ):
        output = tmp_path / "promoted"
        reports = tuple(
            argument
            for index in range(report_count)
            for argument in ("--cross-validation", f"report-{index}.json")
        )

        exit_code = main(
            [
                "promote",
                str(tmp_path / "finalized.json"),
                "--output",
                str(output),
                "--data",
                str(tmp_path / "training-data"),
                "--split-manifest",
                str(tmp_path / "training-split.json"),
                "--model-name",
                "paste-volume",
                "--model-version",
                "test-v1",
                *reports,
            ]
        )

        assert exit_code == 2
        assert "合計3件" in capsys.readouterr().err
        assert not output.exists()

    @pytest.mark.parametrize("attestation_state", ("missing", "wrong-run-kind"))
    def test_promote_rejects_unattested_cross_validation_evidence(
        self,
        tmp_path,
        runtime_artifacts,
        mlflow_server,
        capsys,
        attestation_state,
    ):
        finalized_path = tmp_path / "finalized.json"
        save_finalized_model_candidate(finalized_path, runtime_artifacts.finalized)
        _attest_existing_output(finalized_path, mlflow_server, run_kind="evaluate")
        report_paths: list[Path] = []
        for index, report in enumerate(runtime_artifacts.cross_validation_reports):
            report_path = tmp_path / f"cross-validation-{report.dimension}.json"
            shutil.copy2(report.report_path, report_path)
            if index > 0 or attestation_state == "wrong-run-kind":
                _attest_existing_output(
                    report_path,
                    mlflow_server,
                    run_kind=(
                        "evaluate"
                        if index == 0 and attestation_state == "wrong-run-kind"
                        else "cross-validation-summary"
                    ),
                )
            report_paths.append(report_path)
        output = tmp_path / "promoted"

        exit_code = main(
            [
                "promote",
                str(finalized_path),
                "--output",
                str(output),
                "--data",
                str(runtime_artifacts.dataset_path),
                "--split-manifest",
                str(runtime_artifacts.split_path),
                "--model-name",
                "paste-volume",
                "--model-version",
                "test-v1",
                *(
                    argument
                    for report_path in report_paths
                    for argument in ("--cross-validation", str(report_path))
                ),
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        assert exit_code == 2
        error = capsys.readouterr().err
        if attestation_state == "missing":
            assert "formal MLflow success attestation is missing" in error
        else:
            assert "attestation run_kind is invalid" in error
        assert not output.exists()

    def test_promote_forwards_external_frozen_test_pair_and_records_arguments(
        self, tmp_path, runtime_artifacts, mlflow_server, capsys
    ):
        external_data = tmp_path / "external-data"
        external_split = _write_external_split(external_data)
        external_frozen_test = evaluate_frozen_test_candidate_from_dataset(
            runtime_artifacts.finalized.selection,
            runtime_artifacts.fp32_reference_path,
            dataset_paths=(external_data,),
            split_manifest=external_split,
            allow_external_split=True,
        )
        finalized = finalize_selected_model_candidate(
            runtime_artifacts.finalized.selection,
            external_frozen_test,
        )
        finalized_path = tmp_path / "finalized.json"
        save_finalized_model_candidate(finalized_path, finalized)
        _attest_existing_output(finalized_path, mlflow_server, run_kind="evaluate")
        cross_validation_paths: list[Path] = []
        for report in runtime_artifacts.cross_validation_reports:
            report_path = tmp_path / f"cross-validation-{report.dimension}.json"
            shutil.copy2(report.report_path, report_path)
            _attest_existing_output(
                report_path,
                mlflow_server,
                run_kind="cross-validation-summary",
            )
            cross_validation_paths.append(report_path)
        output = tmp_path / "promoted"

        exit_code = main(
            [
                "promote",
                str(finalized_path),
                "--output",
                str(output),
                "--data",
                str(runtime_artifacts.dataset_path),
                "--split-manifest",
                str(runtime_artifacts.split_path),
                "--frozen-test-data",
                str(external_data),
                "--frozen-test-split-manifest",
                str(external_split),
                "--model-name",
                "paste-volume",
                "--model-version",
                "external-v1",
                *(
                    argument
                    for report_path in cross_validation_paths
                    for argument in ("--cross-validation", str(report_path))
                ),
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 0, captured.err
        evaluation_payload = json.loads(
            (output / "evaluation.json").read_text(encoding="utf-8")
        )
        cross_validation_evidence = evaluation_payload["cross_validation"]
        assert [
            evidence["report"]["dimension"] for evidence in cross_validation_evidence
        ] == ["machine", "paste_lot", "nozzle"]
        assert all(
            evidence["summary_attestation"]["run_kind"] == "cross-validation-summary"
            for evidence in cross_validation_evidence
        )
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        assert [item["dimension"] for item in manifest["cross_validation"]] == [
            "machine",
            "paste_lot",
            "nozzle",
        ]
        attestation = json.loads(_attestation_path(output).read_text(encoding="utf-8"))
        client = MlflowClient(tracking_uri=mlflow_server.uri)
        downloaded = Path(
            client.download_artifacts(
                attestation["run_id"],
                "operation/resolved-arguments.json",
                tmp_path / "resolved-arguments",
            )
        )
        resolved = json.loads(downloaded.read_text(encoding="utf-8"))
        assert resolved["frozen_test_data"] == [str(external_data)]
        assert resolved["frozen_test_split_manifest"] == str(external_split)
        assert resolved["cross_validation"] == [
            str(report_path) for report_path in cross_validation_paths
        ]

    def test_formal_operation_rejects_non_server_tracking_before_writing(
        self, tmp_path, capsys
    ):
        output = tmp_path / "export"

        exit_code = main(
            [
                "export",
                str(tmp_path / "missing-weights.pt"),
                "--output",
                str(output),
                "--tracking-uri",
                f"sqlite:///{tmp_path / 'mlflow.db'}",
            ]
        )

        assert exit_code == 2
        assert "HTTP(S) tracking server" in capsys.readouterr().err
        assert not output.exists()

    @pytest.mark.parametrize("operation", ("export", "compile-parity", "export-parity"))
    def test_formal_weight_consumers_reject_unattested_weights(
        self,
        tmp_path,
        runtime_artifacts,
        mlflow_server,
        capsys,
        operation,
    ):
        weights = tmp_path / f"{operation}-weights.pt"
        shutil.copy2(runtime_artifacts.directory / "export" / "weights.pt", weights)
        output = tmp_path / f"{operation}-output"
        arguments = [operation, str(weights)]
        if operation == "compile-parity":
            arguments.extend(
                (
                    "--data",
                    str(runtime_artifacts.dataset_path),
                    "--split-manifest",
                    str(runtime_artifacts.split_path),
                )
            )
        elif operation == "export-parity":
            arguments.extend(
                (
                    "--fp32-model",
                    str(runtime_artifacts.fp32_reference_path),
                    "--data",
                    str(runtime_artifacts.dataset_path),
                    "--split-manifest",
                    str(runtime_artifacts.split_path),
                )
            )
        arguments.extend(
            (
                "--output",
                str(output),
                "--tracking-uri",
                mlflow_server.uri,
            )
        )

        exit_code = main(arguments)

        assert exit_code == 2
        assert "formal MLflow success attestation is missing" in capsys.readouterr().err
        assert not output.exists()

    def test_formal_weight_consumer_rejects_wrong_operation_attestation(
        self, tmp_path, runtime_artifacts, mlflow_server, capsys
    ):
        weights = tmp_path / "wrong-operation-weights.pt"
        shutil.copy2(runtime_artifacts.directory / "export" / "weights.pt", weights)
        _attest_existing_output(weights, mlflow_server, run_kind="export")
        output = tmp_path / "export"

        exit_code = main(
            [
                "export",
                str(weights),
                "--output",
                str(output),
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        assert exit_code == 2
        assert "attestation run_kind is invalid" in capsys.readouterr().err
        assert not output.exists()

    def test_formal_export_publishes_attestation_after_finished_mlflow_run(
        self, tmp_path, runtime_artifacts, mlflow_server, capsys
    ):
        weights = write_test_weights(tmp_path / "weights.pt", runtime_artifacts.lineage)
        _attest_existing_output(weights, mlflow_server, run_kind="finetune")
        output = tmp_path / "export"

        exit_code = main(
            [
                "export",
                str(weights),
                "--output",
                str(output),
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 0, captured.err
        attestation_path = _attestation_path(output)
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        client = MlflowClient(tracking_uri=mlflow_server.uri)
        run = client.get_run(attestation["run_id"])
        assert run.info.status == "FINISHED"
        assert attestation["output_fingerprint"] == _path_fingerprint(output)
        downloaded = Path(
            client.download_artifacts(
                attestation["run_id"],
                "operation/formal-success.json",
                tmp_path / "download",
            )
        )
        assert json.loads(downloaded.read_text(encoding="utf-8")) == attestation

    def test_formal_operation_omits_credentials_from_git_provenance_artifacts(
        self, tmp_path, runtime_artifacts, mlflow_server, monkeypatch, capsys
    ):
        repository = tmp_path / "repository"
        repository.mkdir()
        tracked = repository / "tracked.yaml"
        tracked.write_text(
            "tracking_uri: https://example.test/mlflow\n", encoding="utf-8"
        )
        subprocess.run(("git", "init", "-q"), cwd=repository, check=True)
        subprocess.run(("git", "add", "tracked.yaml"), cwd=repository, check=True)
        subprocess.run(
            (
                "git",
                "-c",
                "user.name=Paste Volume Test",
                "-c",
                "user.email=paste-volume@example.test",
                "commit",
                "-qm",
                "base",
            ),
            cwd=repository,
            check=True,
        )
        tracked_secret_uri = (
            "https://operator:tracked-secret@example.test/mlflow"
            "?token=tracked-query#tracked-fragment"
        )
        tracked.write_text(f"tracking_uri: {tracked_secret_uri}\n", encoding="utf-8")
        untracked = repository / "src" / "secret.yaml"
        untracked.parent.mkdir()
        untracked_content = (
            "storage: https://agent:untracked-secret@example.test/db"
            "?password=untracked-query#untracked-fragment\n"
        )
        untracked.write_text(untracked_content, encoding="utf-8")
        output = repository / "export"
        weights = repository / "weights.pt"
        shutil.copy2(runtime_artifacts.directory / "export" / "weights.pt", weights)
        _attest_existing_output(weights, mlflow_server, run_kind="finetune")
        monkeypatch.chdir(repository)

        exit_code = main(
            [
                "export",
                str(weights),
                "--output",
                str(output),
                "--tracking-uri",
                mlflow_server.uri,
                "--run-name",
                tracked_secret_uri,
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 0, captured.err
        attestation = json.loads(_attestation_path(output).read_text(encoding="utf-8"))
        client = MlflowClient(tracking_uri=mlflow_server.uri)
        downloaded_git = Path(
            client.download_artifacts(
                attestation["run_id"],
                "operation/git.json",
                tmp_path / "git-provenance",
            )
        )
        downloaded_diff = Path(
            client.download_artifacts(
                attestation["run_id"],
                "operation/git-diff.patch",
                tmp_path / "git-diff",
            )
        )
        downloaded_arguments = Path(
            client.download_artifacts(
                attestation["run_id"],
                "operation/resolved-arguments.json",
                tmp_path / "resolved-arguments-security",
            )
        )
        git_payload = json.loads(downloaded_git.read_text(encoding="utf-8"))
        persisted = (
            downloaded_git.read_text(encoding="utf-8")
            + downloaded_diff.read_text(encoding="utf-8")
            + downloaded_arguments.read_text(encoding="utf-8")
        )
        run = client.get_run(attestation["run_id"])
        assert git_payload["untracked_content"] == "[omitted at persistence boundary]"
        assert (
            b64encode(untracked_content.encode("utf-8")).decode("ascii")
            not in persisted
        )
        assert "https://example.test/mlflow" in persisted
        assert run.data.tags["mlflow.runName"] == "https://example.test/mlflow"
        for secret in (
            "operator",
            "tracked-secret",
            "tracked-query",
            "tracked-fragment",
            "agent",
            "untracked-secret",
            "untracked-query",
            "untracked-fragment",
        ):
            assert secret not in persisted

    def test_formal_failure_artifact_omits_credentials_from_error_message(
        self, tmp_path, mlflow_server, monkeypatch, capsys
    ):
        import pcbasm.pasting.paste_volume.training as training_module

        secret_uri = (
            "https://operator:failure-secret@example.test/mlflow"
            "?token=failure-query#failure-fragment"
        )

        def fail_to_load_weights(*args, **kwargs):
            raise RuntimeError(f"unable to read weights from {secret_uri}")

        monkeypatch.setattr(
            training_module,
            "load_formal_training_weights",
            fail_to_load_weights,
        )
        weights = tmp_path / "weights.pt"
        weights.write_bytes(b"not-used")
        output = tmp_path / "failed-export"

        exit_code = main(
            [
                "export",
                str(weights),
                "--output",
                str(output),
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        assert exit_code == 2
        assert "unable to read weights" in capsys.readouterr().err
        client = MlflowClient(tracking_uri=mlflow_server.uri)
        experiment = client.get_experiment_by_name("paste-volume")
        assert experiment is not None
        matches = [
            run
            for run in client.search_runs(
                [experiment.experiment_id],
                filter_string="tags.run_kind = 'export'",
            )
            if run.data.params.get("arg.output") == json.dumps(str(output))
        ]
        assert len(matches) == 1
        run = matches[0]
        assert run.info.status == "FAILED"
        downloaded = Path(
            client.download_artifacts(
                run.info.run_id,
                "operation/failure.json",
                tmp_path / "failure-artifact",
            )
        )
        persisted = downloaded.read_text(encoding="utf-8")
        assert "https://example.test/mlflow" in persisted
        for secret in (
            "operator",
            "failure-secret",
            "failure-query",
            "failure-fragment",
        ):
            assert secret not in persisted

    def test_compile_parity_success_records_full_report_and_resolved_arguments(
        self, tmp_path, runtime_artifacts, mlflow_server, monkeypatch, capsys
    ):
        import pcbasm.pasting.paste_volume.export as export_module

        report = runtime_artifacts.finalized.selection.candidate.compile_parity
        observed = {}

        def run_compile_parity_from_formal_weights(
            formal_weights, dataset_paths, split_manifest, *, config
        ):
            observed.update(
                formal_weights=formal_weights,
                dataset_paths=dataset_paths,
                split_manifest=split_manifest,
                config=config,
            )
            return report

        monkeypatch.setattr(
            export_module,
            "run_compile_parity_from_formal_weights",
            run_compile_parity_from_formal_weights,
        )
        weights = tmp_path / "weights.pt"
        shutil.copy2(runtime_artifacts.directory / "export" / "weights.pt", weights)
        _attest_existing_output(weights, mlflow_server, run_kind="finetune")
        output = tmp_path / "compile-parity"

        exit_code = main(
            [
                "compile-parity",
                str(weights),
                "--data",
                str(runtime_artifacts.dataset_path),
                "--split-manifest",
                str(runtime_artifacts.split_path),
                "--output",
                str(output),
                "--device",
                "cpu",
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        assert exit_code == 0, capsys.readouterr().err
        report_path = output / "compile-parity.json"
        attestation = json.loads(_attestation_path(output).read_text(encoding="utf-8"))
        client = MlflowClient(tracking_uri=mlflow_server.uri)
        run = client.get_run(attestation["run_id"])

        assert run.info.status == "FINISHED"
        assert run.data.tags["operation"] == "compile-parity"
        assert run.data.tags["compile_parity_sha256"] == report.content_sha256
        assert run.data.metrics["graph_break_count"] == 0
        assert run.data.metrics["passing_case_count"] == len(report.cases)
        downloaded_report = Path(
            client.download_artifacts(
                attestation["run_id"],
                "outputs/compile-parity.json",
                tmp_path / "compile-parity-report",
            )
        )
        assert load_compile_parity_report(downloaded_report) == report
        downloaded_arguments = Path(
            client.download_artifacts(
                attestation["run_id"],
                "operation/resolved-arguments.json",
                tmp_path / "compile-parity-arguments",
            )
        )
        arguments = json.loads(downloaded_arguments.read_text(encoding="utf-8"))
        assert arguments["data"] == [str(runtime_artifacts.dataset_path)]
        assert arguments["split_manifest"] == str(runtime_artifacts.split_path)
        assert arguments["backend"] == "inductor"
        assert arguments["mode"] == "default"
        assert arguments["device"] == "cpu"
        assert observed["formal_weights"].weights_path == weights
        assert observed["formal_weights"].run_kind == "finetune"
        assert observed["dataset_paths"] == (runtime_artifacts.dataset_path,)
        assert observed["split_manifest"] == runtime_artifacts.split_path
        assert observed["config"].backend == "inductor"
        assert observed["config"].mode == "default"
        assert report_path == output / "compile-parity.json"

    def test_compile_failure_preserves_report_and_marks_mlflow_run_failed(
        self, tmp_path, runtime_artifacts, mlflow_server, monkeypatch, capsys
    ):
        import pcbasm.pasting.paste_volume.export as export_module

        report = _compile_report_with_graph_break(
            runtime_artifacts.finalized.selection.candidate.compile_parity
        )
        monkeypatch.setattr(
            export_module,
            "run_compile_parity_from_formal_weights",
            lambda *args, **kwargs: report,
        )
        output = tmp_path / "compile-parity-failure.json"
        weights = tmp_path / "weights.pt"
        shutil.copy2(runtime_artifacts.directory / "export" / "weights.pt", weights)
        _attest_existing_output(weights, mlflow_server, run_kind="finetune")
        arguments = [
            "compile-parity",
            str(weights),
            "--data",
            str(runtime_artifacts.dataset_path),
            "--split-manifest",
            str(runtime_artifacts.split_path),
            "--output",
            str(output),
            "--device",
            "cpu",
            "--tracking-uri",
            mlflow_server.uri,
        ]

        exit_code = main(arguments)

        assert exit_code == 2
        assert "report was preserved" in capsys.readouterr().err
        persisted_report = load_compile_parity_report(output)
        assert persisted_report == report
        assert persisted_report.success is False
        assert persisted_report.runtime.graph_break_count == 1
        assert not _attestation_path(output).exists()
        run = _compile_parity_run_for_output(output, mlflow_server)
        assert run.info.status == "FAILED"
        assert run.data.tags["compile_parity_success"] == "False"
        client = MlflowClient(tracking_uri=mlflow_server.uri)
        downloaded = Path(
            client.download_artifacts(
                run.info.run_id,
                "outputs/compile-parity-failure.json",
                tmp_path / "failed-report",
            )
        )
        assert load_compile_parity_report(downloaded) == report
        original = output.read_bytes()

        retry_exit_code = main(arguments)

        assert retry_exit_code == 2
        assert "already exists" in capsys.readouterr().err
        assert output.read_bytes() == original
        assert not _attestation_path(output).exists()

    def test_export_parity_success_records_full_report_and_resolved_arguments(
        self, tmp_path, runtime_artifacts, mlflow_server, monkeypatch, capsys
    ):
        import pcbasm.pasting.paste_volume.export as export_module

        report = runtime_artifacts.finalized.selection.candidate.export_parity
        observed = {}

        def run_export_parity_from_formal_weights(
            formal_weights,
            fp32_model_path,
            dataset_paths,
            split_manifest,
            *,
            split_name="validation",
        ):
            observed.update(
                formal_weights=formal_weights,
                fp32_model_path=fp32_model_path,
                dataset_paths=dataset_paths,
                split_manifest=split_manifest,
                split_name=split_name,
            )
            return report

        monkeypatch.setattr(
            export_module,
            "run_export_parity_from_formal_weights",
            run_export_parity_from_formal_weights,
        )
        weights = tmp_path / "weights.pt"
        fp32_model = tmp_path / "model.fp32.onnx"
        shutil.copy2(runtime_artifacts.directory / "export" / "weights.pt", weights)
        shutil.copy2(runtime_artifacts.fp32_reference_path, fp32_model)
        _attest_existing_output(weights, mlflow_server, run_kind="finetune")
        _attest_existing_output(fp32_model, mlflow_server, run_kind="export")
        output = tmp_path / "export-parity"

        exit_code = main(
            [
                "export-parity",
                str(weights),
                "--fp32-model",
                str(fp32_model),
                "--data",
                str(runtime_artifacts.dataset_path),
                "--split-manifest",
                str(runtime_artifacts.split_path),
                "--output",
                str(output),
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        assert exit_code == 0, capsys.readouterr().err
        report_path = output / "export-parity.json"
        assert load_export_parity_report(report_path) == report
        attestation = json.loads(_attestation_path(output).read_text(encoding="utf-8"))
        client = MlflowClient(tracking_uri=mlflow_server.uri)
        run = client.get_run(attestation["run_id"])
        assert run.info.status == "FINISHED"
        assert run.data.tags["operation"] == "export-parity"
        assert run.data.tags["export_parity_sha256"] == report.content_sha256
        assert run.data.metrics["passing_sample_count"] == len(report.sample_results)
        assert run.data.metrics["passing_shape_case_count"] == len(report.shape_results)
        assert tuple(shape.case_name for shape in report.shape_results) == (
            "minimum",
            "maximum-area",
            "portrait",
            "landscape",
        )
        downloaded_report = Path(
            client.download_artifacts(
                attestation["run_id"],
                "outputs/export-parity.json",
                tmp_path / "export-parity-report",
            )
        )
        assert load_export_parity_report(downloaded_report) == report
        downloaded_arguments = Path(
            client.download_artifacts(
                attestation["run_id"],
                "operation/resolved-arguments.json",
                tmp_path / "export-parity-arguments",
            )
        )
        arguments = json.loads(downloaded_arguments.read_text(encoding="utf-8"))
        assert arguments["weights"] == str(weights)
        assert arguments["fp32_model"] == str(fp32_model)
        assert arguments["data"] == [str(runtime_artifacts.dataset_path)]
        assert arguments["split_manifest"] == str(runtime_artifacts.split_path)
        assert observed["formal_weights"].weights_path == weights
        assert observed["formal_weights"].run_kind == "finetune"
        assert observed["fp32_model_path"] == fp32_model
        assert observed["dataset_paths"] == (runtime_artifacts.dataset_path,)
        assert observed["split_manifest"] == runtime_artifacts.split_path
        assert observed["split_name"] == "validation"

    def test_export_parity_failure_preserves_report_and_marks_run_failed(
        self, tmp_path, runtime_artifacts, mlflow_server, monkeypatch, capsys
    ):
        import pcbasm.pasting.paste_volume.export as export_module

        report = _failing_export_parity_report(
            runtime_artifacts.finalized.selection.candidate.export_parity
        )
        monkeypatch.setattr(
            export_module,
            "run_export_parity_from_formal_weights",
            lambda *args, **kwargs: report,
        )
        weights = tmp_path / "weights.pt"
        fp32_model = tmp_path / "model.fp32.onnx"
        shutil.copy2(runtime_artifacts.directory / "export" / "weights.pt", weights)
        shutil.copy2(runtime_artifacts.fp32_reference_path, fp32_model)
        _attest_existing_output(weights, mlflow_server, run_kind="finetune")
        _attest_existing_output(fp32_model, mlflow_server, run_kind="export")
        output = tmp_path / "export-parity-failure.json"
        arguments = [
            "export-parity",
            str(weights),
            "--fp32-model",
            str(fp32_model),
            "--data",
            str(runtime_artifacts.dataset_path),
            "--split-manifest",
            str(runtime_artifacts.split_path),
            "--output",
            str(output),
            "--tracking-uri",
            mlflow_server.uri,
        ]

        exit_code = main(arguments)

        assert exit_code == 2
        assert "report was preserved" in capsys.readouterr().err
        assert load_export_parity_report(output) == report
        assert not _attestation_path(output).exists()
        run = _export_parity_run_for_output(output, mlflow_server)
        assert run.info.status == "FAILED"
        assert run.data.tags["export_parity_success"] == "False"
        client = MlflowClient(tracking_uri=mlflow_server.uri)
        downloaded = Path(
            client.download_artifacts(
                run.info.run_id,
                "outputs/export-parity-failure.json",
                tmp_path / "failed-export-parity-report",
            )
        )
        assert load_export_parity_report(downloaded) == report
        original = output.read_bytes()

        retry_exit_code = main(arguments)

        assert retry_exit_code == 2
        assert "File exists" in capsys.readouterr().err
        assert output.read_bytes() == original
        assert not _attestation_path(output).exists()

    @pytest.mark.parametrize("tampered", (False, True))
    def test_candidate_bind_rejects_missing_or_tampered_compile_attestation(
        self, tmp_path, mlflow_server, capsys, tampered
    ):
        compile_parity = tmp_path / "compile-parity.json"
        compile_parity.write_text("{}\n", encoding="utf-8")
        if tampered:
            attestation_path = _attest_existing_output(
                compile_parity,
                mlflow_server,
                run_kind="compile-parity",
            )
            attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
            attestation["output_fingerprint"] = "sha256:" + "0" * 64
            attestation_path.write_text(
                json.dumps(attestation, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        output = tmp_path / "candidate.json"

        exit_code = main(
            [
                "candidate",
                "bind",
                str(tmp_path / "unused-model.onnx"),
                "--evaluation",
                str(tmp_path / "unused-evaluation.json"),
                "--benchmark",
                str(tmp_path / "unused-benchmark.json"),
                "--compile-parity",
                str(compile_parity),
                "--export-parity",
                str(tmp_path / "unused-export-parity.json"),
                "--output",
                str(output),
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        assert exit_code == 2
        error = capsys.readouterr().err
        if tampered:
            assert "attestation does not match artifact" in error
        else:
            assert "formal MLflow success attestation is missing" in error
        assert not output.exists()
        assert not _attestation_path(output).exists()

    @pytest.mark.parametrize("attestation_state", ("missing", "tampered", "wrong-kind"))
    def test_candidate_bind_rejects_invalid_export_parity_attestation(
        self,
        tmp_path,
        formal_compile_parity,
        mlflow_server,
        capsys,
        attestation_state,
    ):
        _, compile_parity_path, _ = formal_compile_parity
        export_parity = tmp_path / "export-parity.json"
        export_parity.write_text("{}\n", encoding="utf-8")
        if attestation_state != "missing":
            attestation_path = _attest_existing_output(
                export_parity,
                mlflow_server,
                run_kind=(
                    "compile-parity"
                    if attestation_state == "wrong-kind"
                    else "export-parity"
                ),
            )
            if attestation_state == "tampered":
                attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
                attestation["output_fingerprint"] = "sha256:" + "0" * 64
                attestation_path.write_text(
                    json.dumps(attestation, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

        output = tmp_path / "candidate.json"
        exit_code = main(
            [
                "candidate",
                "bind",
                str(tmp_path / "unused-model.onnx"),
                "--evaluation",
                str(tmp_path / "unused-evaluation.json"),
                "--benchmark",
                str(tmp_path / "unused-benchmark.json"),
                "--compile-parity",
                str(compile_parity_path),
                "--export-parity",
                str(export_parity),
                "--output",
                str(output),
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        assert exit_code == 2
        error = capsys.readouterr().err
        if attestation_state == "missing":
            assert "formal MLflow success attestation is missing" in error
        elif attestation_state == "tampered":
            assert "attestation does not match artifact" in error
        else:
            assert "attestation run_kind is invalid" in error
        assert not output.exists()

    def test_candidate_bind_loads_and_forwards_typed_compile_parity_report(
        self,
        tmp_path,
        runtime_artifacts,
        formal_compile_parity,
        mlflow_server,
        capsys,
    ):
        _, compile_parity_path, compile_parity = formal_compile_parity
        candidate = runtime_artifacts.finalized.selection.candidate
        evaluation_path = tmp_path / "evaluation.json"
        benchmark_path = tmp_path / "benchmark.json"
        export_parity_path = tmp_path / "export-parity.json"
        model_path = tmp_path / "model.onnx"
        save_candidate_evaluation(evaluation_path, candidate.evaluation)
        save_model_benchmark_result(benchmark_path, candidate.benchmark)
        save_export_parity_report(export_parity_path, candidate.export_parity)
        shutil.copy2(runtime_artifacts.model_path, model_path)
        _attest_existing_output(evaluation_path, mlflow_server, run_kind="evaluate")
        _attest_existing_output(benchmark_path, mlflow_server, run_kind="benchmark")
        _attest_existing_output(
            export_parity_path,
            mlflow_server,
            run_kind="export-parity",
        )
        _attest_existing_output(model_path, mlflow_server, run_kind="optimize")
        output = tmp_path / "candidate.json"

        exit_code = main(
            [
                "candidate",
                "bind",
                str(model_path),
                "--evaluation",
                str(evaluation_path),
                "--benchmark",
                str(benchmark_path),
                "--compile-parity",
                str(compile_parity_path),
                "--export-parity",
                str(export_parity_path),
                "--output",
                str(output),
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 0, captured.err
        bound = load_model_candidate_validation(output)
        assert bound.compile_parity.content_sha256 == compile_parity.content_sha256
        assert (
            bound.export_parity.content_sha256 == candidate.export_parity.content_sha256
        )
        assert _attestation_path(output).is_file()
        package_output = tmp_path / "candidate-package"

        package_exit_code = main(
            [
                "candidate",
                "package",
                str(output),
                "--output",
                str(package_output),
                "--data",
                str(runtime_artifacts.dataset_path),
                "--split-manifest",
                str(runtime_artifacts.split_path),
                "--model-name",
                "paste-volume",
                "--model-version",
                "compile-bound-v1",
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        assert package_exit_code == 0, capsys.readouterr().err
        assert (package_output / "manifest.json").is_file()
        packaged_evidence = json.loads(
            (package_output / "evaluation.json").read_text(encoding="utf-8")
        )
        assert (
            packaged_evidence["compile_parity"]["content_sha256"]
            == compile_parity.content_sha256
        )
        assert (
            packaged_evidence["export_parity"]["content_sha256"]
            == candidate.export_parity.content_sha256
        )
        assert _attestation_path(package_output).is_file()

    def test_downstream_rejects_local_artifact_without_finished_attestation(
        self, tmp_path, runtime_artifacts, mlflow_server, capsys
    ):
        raw_model = tmp_path / "unfinished.onnx"
        shutil.copy2(runtime_artifacts.model_path, raw_model)
        output = tmp_path / "evaluation.json"

        exit_code = main(
            [
                "candidate",
                "evaluate",
                str(raw_model),
                "--fp32-reference",
                str(raw_model),
                "--model-format",
                "onnx-fp32",
                "--data",
                str(tmp_path / "unused-data"),
                "--split-manifest",
                str(tmp_path / "unused-split.json"),
                "--output",
                str(output),
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        assert exit_code == 2
        assert "formal MLflow success attestation" in capsys.readouterr().err
        assert not output.exists()
        assert not _attestation_path(output).exists()

    def test_frozen_test_receipt_prevents_second_evaluation_with_new_output(
        self, tmp_path, runtime_artifacts, mlflow_server, capsys
    ):
        model_path = tmp_path / "model.onnx"
        shutil.copy2(runtime_artifacts.model_path, model_path)
        _attest_existing_output(model_path, mlflow_server, run_kind="export")
        reference_path = tmp_path / "reference.onnx"
        shutil.copy2(runtime_artifacts.fp32_reference_path, reference_path)
        _attest_existing_output(reference_path, mlflow_server, run_kind="export")
        selection_path = _write_formal_selection(
            tmp_path / "selection.json",
            model_path,
            runtime_artifacts,
            mlflow_server,
        )
        data_root = tmp_path / "external-data"
        split_path = _write_external_split(data_root)
        first_output = tmp_path / "frozen-test.json"

        first_exit_code = main(
            [
                "candidate",
                "frozen-test",
                str(selection_path),
                "--fp32-reference",
                str(reference_path),
                "--data",
                str(data_root),
                "--split-manifest",
                str(split_path),
                "--allow-external-split",
                "--output",
                str(first_output),
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        first_capture = capsys.readouterr()
        assert first_exit_code == 0, first_capture.err
        receipt_path = selection_path.with_name(
            f"{selection_path.name}.frozen-test-consumed.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["selection_sha256"] == _sha256_file(selection_path)
        assert receipt["split_manifest_sha256"] == _sha256_file(split_path)
        assert receipt["retry_allowed"] is False
        assert _attestation_path(first_output).is_file()

        second_output = tmp_path / "frozen-test-second.json"
        second_exit_code = main(
            [
                "candidate",
                "frozen-test",
                str(selection_path),
                "--fp32-reference",
                str(reference_path),
                "--data",
                str(data_root),
                "--split-manifest",
                str(split_path),
                "--allow-external-split",
                "--output",
                str(second_output),
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        assert second_exit_code == 2
        assert "output pathを変えても再評価できません" in capsys.readouterr().err
        assert not second_output.exists()

    def test_failed_frozen_test_attempt_remains_permanently_consumed(
        self, tmp_path, runtime_artifacts, mlflow_server, capsys
    ):
        model_path = tmp_path / "model.onnx"
        shutil.copy2(runtime_artifacts.model_path, model_path)
        _attest_existing_output(model_path, mlflow_server, run_kind="export")
        reference_path = tmp_path / "reference.onnx"
        shutil.copy2(runtime_artifacts.fp32_reference_path, reference_path)
        _attest_existing_output(reference_path, mlflow_server, run_kind="export")
        selection_path = _write_formal_selection(
            tmp_path / "selection.json",
            model_path,
            runtime_artifacts,
            mlflow_server,
        )
        data_root = tmp_path / "external-data"
        split_path = _write_external_split(data_root)
        failed_output = tmp_path / "failed-frozen-test.json"

        first_exit_code = main(
            [
                "candidate",
                "frozen-test",
                str(selection_path),
                "--fp32-reference",
                str(model_path),
                "--data",
                str(data_root),
                "--split-manifest",
                str(split_path),
                "--allow-external-split",
                "--output",
                str(failed_output),
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        assert first_exit_code == 2
        capsys.readouterr()
        receipt_path = selection_path.with_name(
            f"{selection_path.name}.frozen-test-consumed.json"
        )
        assert receipt_path.is_file()
        assert not failed_output.exists()
        assert not _attestation_path(failed_output).exists()

        retry_output = tmp_path / "retry-frozen-test.json"
        retry_exit_code = main(
            [
                "candidate",
                "frozen-test",
                str(selection_path),
                "--fp32-reference",
                str(reference_path),
                "--data",
                str(data_root),
                "--split-manifest",
                str(split_path),
                "--allow-external-split",
                "--output",
                str(retry_output),
                "--tracking-uri",
                mlflow_server.uri,
            ]
        )

        assert retry_exit_code == 2
        assert "既に消費済み" in capsys.readouterr().err
        assert not retry_output.exists()

    def test_infer_emits_strict_json_for_a_rejected_prediction(
        self, tmp_path, runtime_artifacts, capsys
    ):
        pre_path = tmp_path / "pre.png"
        post_path = tmp_path / "post.png"
        _write_image(pre_path, mode="RGB", image_format="PNG")
        _write_image(post_path, mode="RGB", image_format="PNG")

        exit_code = main(
            [
                "infer",
                str(runtime_artifacts.package.package_path),
                str(pre_path),
                str(post_path),
                "--pixel-per-mm",
                "101",
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 0, captured.err
        assert "NaN" not in captured.out
        payload = json.loads(captured.out)
        assert payload["prediction"]["accepted"] is False
        assert "training coverage外" in payload["prediction"]["rejection_reason"]

    @pytest.mark.parametrize(
        ("mode", "image_format", "suffix"),
        [
            ("L", "PNG", ".png"),
            ("RGBA", "PNG", ".png"),
            ("RGB", "JPEG", ".jpg"),
        ],
    )
    def test_infer_rejects_non_rgb_or_non_png_input(
        self,
        tmp_path,
        runtime_artifacts,
        capsys,
        mode,
        image_format,
        suffix,
    ):
        pre_path = tmp_path / f"pre{suffix}"
        post_path = tmp_path / "post.png"
        _write_image(pre_path, mode=mode, image_format=image_format)
        _write_image(post_path, mode="RGB", image_format="PNG")

        exit_code = main(
            [
                "infer",
                str(runtime_artifacts.package.package_path),
                str(pre_path),
                str(post_path),
                "--pixel-per-mm",
                "20",
            ]
        )

        assert exit_code == 2
        assert "RGB PNG" in capsys.readouterr().err

    @pytest.mark.parametrize("pointer_name", ["active-model.json", "manifest.json"])
    def test_activate_rejects_pointer_inside_immutable_package(
        self, tmp_path, runtime_artifacts, capsys, pointer_name
    ):
        package = tmp_path / "promoted"
        shutil.copytree(runtime_artifacts.package.package_path, package)
        manifest_before = (package / "manifest.json").read_bytes()
        pointer = package / pointer_name

        exit_code = main(["activate", str(package), "--pointer", str(pointer)])

        assert exit_code == 2
        assert "packageの外" in capsys.readouterr().err
        assert (package / "manifest.json").read_bytes() == manifest_before
        if pointer_name == "active-model.json":
            assert not pointer.exists()

    def test_operational_mlflow_default_matches_hydra_config(self):
        project_root = Path(__file__).parents[3]
        result = subprocess.run(
            (
                sys.executable,
                "-m",
                "pcbasm.cli.paste_volume",
                "export",
                "--help",
            ),
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert "default: paste-volume" in result.stdout
        logger_config = (
            project_root / "src/pcbasm/pasting/paste_volume/conf/logger/mlflow.yaml"
        ).read_text(encoding="utf-8")
        assert "experiment_name: paste-volume" in logger_config.splitlines()

    def test_benchmark_help_and_parser_enforce_fixed_iteration_counts(self):
        help_result = subprocess.run(
            (
                sys.executable,
                "-m",
                "pcbasm.cli.paste_volume",
                "benchmark",
                "--help",
            ),
            capture_output=True,
            text=True,
        )

        assert help_result.returncode == 0, help_result.stderr
        assert "--warmup-iterations {10}" in help_result.stdout
        assert "--measured-iterations {100}" in help_result.stdout

        for option, invalid_value in (
            ("--warmup-iterations", "11"),
            ("--measured-iterations", "99"),
        ):
            result = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "pcbasm.cli.paste_volume",
                    "benchmark",
                    "missing.onnx",
                    "--model-format",
                    "onnx-fp32",
                    "--data",
                    "missing-data",
                    "--split-manifest",
                    "missing-split.json",
                    "--output",
                    "missing-output.json",
                    "--power-condition",
                    "official-27W-supply",
                    "--cooling-condition",
                    "active-fan",
                    option,
                    invalid_value,
                ),
                capture_output=True,
                text=True,
            )

            assert result.returncode == 2
            assert "invalid choice" in result.stderr
