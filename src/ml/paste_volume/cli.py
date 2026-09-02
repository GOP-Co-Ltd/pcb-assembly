"""Hydra-independent operational CLI for paste-volume ML workflows."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

from ml.cli.runner import run_parser

from .cli_commands.dataset import (
    dataset_merge as _dataset_merge,
    dataset_summarize as _dataset_summarize,
    dataset_validate as _dataset_validate,
    parse_source as _parse_source,
)
from .cli_commands.operations import (
    activate as _activate,
    compile_parity as _compile_parity,
    export as _export,
    export_parity as _export_parity,
    infer as _infer,
    optimize as _optimize,
    rollback as _rollback,
)
from .cli_commands.release import (
    benchmark as _benchmark,
    candidate_bind as _candidate_bind,
    candidate_evaluate as _candidate_evaluate,
    candidate_finalize as _candidate_finalize,
    candidate_frozen_test as _candidate_frozen_test,
    candidate_package as _candidate_package,
    candidate_select as _candidate_select,
    promote as _promote,
)
from .cli_commands.smoke import smoke as _smoke

_DEFAULT_TRACKING_URI = "http://127.0.0.1:5000"
_DEFAULT_EXPERIMENT_NAME = "paste-volume"
_DEPENDENCY_GROUP_BY_MODULE = {
    "PIL": "ml-runtime",
    "hydra": "ml-train",
    "hydra_plugins": "ml-hpo",
    "mlflow": "ml-train",
    "omegaconf": "ml-train",
    "onnx": "ml-export",
    "onnxruntime": "ml-runtime",
    "onnxscript": "ml-export",
    "optuna": "ml-hpo",
    "torch": "ml-runtime",
    "torchvision": "ml-runtime",
}


def _tracking_uri_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tracking-uri",
        default=os.environ.get("MLFLOW_TRACKING_URI", _DEFAULT_TRACKING_URI),
        help="required MLflow tracking server URI",
    )


def _tracking_arguments(parser: argparse.ArgumentParser) -> None:
    _tracking_uri_argument(parser)
    parser.add_argument(
        "--experiment-name",
        default=_DEFAULT_EXPERIMENT_NAME,
        help=f"MLflow experiment name (default: {_DEFAULT_EXPERIMENT_NAME})",
    )
    parser.add_argument("--run-name")


def _model_format_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model-format",
        choices=("onnx-fp32", "onnx-int8-qdq"),
        required=True,
    )


def _frozen_data_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data", nargs="+", required=True)
    parser.add_argument("--split-manifest", required=True)


def _model_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--model-version", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Operate the image-based paste-volume ML pipeline.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    smoke = commands.add_parser("smoke", help="verify the complete ML environment")
    smoke.add_argument(
        "--tracking-uri",
        default=os.environ.get("MLFLOW_TRACKING_URI", _DEFAULT_TRACKING_URI),
        help="MLflow tracking server URI",
    )
    smoke.add_argument(
        "--hydra-override",
        action="append",
        default=None,
        help="Hydra override used during config composition (repeatable)",
    )
    smoke.add_argument("--require-cuda", action="store_true")
    smoke.set_defaults(handler=_smoke)

    dataset = commands.add_parser("dataset", help="inspect or compose datasets")
    dataset_commands = dataset.add_subparsers(dest="dataset_command", required=True)

    merge = dataset_commands.add_parser("merge", help="write a flat composite manifest")
    merge.add_argument(
        "--source",
        action="append",
        required=True,
        type=_parse_source,
        metavar="SOURCE_ID=PATH",
    )
    merge.add_argument("--output", required=True)
    merge.add_argument("--name")
    merge.set_defaults(handler=_dataset_merge)

    validate = dataset_commands.add_parser(
        "validate", help="validate all dataset inputs"
    )
    validate.add_argument("datasets", nargs="+")
    validate.set_defaults(handler=_dataset_validate)

    summarize = dataset_commands.add_parser(
        "summarize", help="summarize resolved datasets"
    )
    summarize.add_argument("datasets", nargs="+")
    summarize.set_defaults(handler=_dataset_summarize)

    export = commands.add_parser("export", help="export strict weights to ONNX FP32")
    export.add_argument("weights")
    export.add_argument("--output", required=True, help="new export directory")
    export.add_argument("--opset-version", type=int, default=18)
    _tracking_arguments(export)
    export.set_defaults(handler=_export)

    optimize = commands.add_parser(
        "optimize", help="build optimized FP32 and static INT8 candidates"
    )
    optimize.add_argument("onnx_model")
    optimize.add_argument("--calibration-data", nargs="+", required=True)
    optimize.add_argument("--split-manifest", required=True)
    optimize.add_argument("--output", required=True, help="new candidate directory")
    optimize.add_argument("--calibration-sample-limit", type=int, default=256)
    optimize.add_argument("--seed", type=int, default=42)
    _tracking_arguments(optimize)
    optimize.set_defaults(handler=_optimize)

    compile_parity = commands.add_parser(
        "compile-parity",
        help=(
            "verify strict inductor/default torch.compile release parity "
            "from strict weights"
        ),
        description=(
            "Verify torch.compile release parity with the fixed backend=inductor "
            "and mode=default contract."
        ),
    )
    compile_parity.add_argument("weights")
    _frozen_data_arguments(compile_parity)
    compile_parity.add_argument(
        "--output",
        required=True,
        help=("new report JSON path, or a directory containing compile-parity.json"),
    )
    compile_parity.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda"),
        default="auto",
        help="compile parity device selection (default: auto)",
    )
    _tracking_arguments(compile_parity)
    compile_parity.set_defaults(
        handler=_compile_parity,
        backend="inductor",
        mode="default",
    )

    export_parity = commands.add_parser(
        "export-parity",
        help="verify eager/FP32 ONNX parity on the persisted validation split",
    )
    export_parity.add_argument("weights")
    export_parity.add_argument("--fp32-model", required=True)
    _frozen_data_arguments(export_parity)
    export_parity.add_argument(
        "--output",
        required=True,
        help=("new report JSON path, or a directory containing export-parity.json"),
    )
    _tracking_arguments(export_parity)
    export_parity.set_defaults(handler=_export_parity)

    candidate = commands.add_parser(
        "candidate", help="evaluate, bind, select, and package model candidates"
    )
    candidate_commands = candidate.add_subparsers(
        dest="candidate_command", required=True
    )

    candidate_evaluate = candidate_commands.add_parser(
        "evaluate", help="evaluate one candidate on the frozen validation split"
    )
    candidate_evaluate.add_argument("model")
    candidate_evaluate.add_argument("--fp32-reference", required=True)
    _model_format_argument(candidate_evaluate)
    _frozen_data_arguments(candidate_evaluate)
    candidate_evaluate.add_argument("--output", required=True)
    _tracking_arguments(candidate_evaluate)
    candidate_evaluate.set_defaults(handler=_candidate_evaluate)

    candidate_package = candidate_commands.add_parser(
        "package", help="package a formally bound candidate with release evidence"
    )
    candidate_package.add_argument("candidate")
    candidate_package.add_argument("--output", required=True)
    _frozen_data_arguments(candidate_package)
    _model_identity_arguments(candidate_package)
    _tracking_arguments(candidate_package)
    candidate_package.set_defaults(handler=_candidate_package)

    candidate_bind = candidate_commands.add_parser(
        "bind", help="bind validation, benchmark, and the exact model artifact"
    )
    candidate_bind.add_argument("model")
    candidate_bind.add_argument("--evaluation", required=True)
    candidate_bind.add_argument("--benchmark", required=True)
    candidate_bind.add_argument("--compile-parity", required=True)
    candidate_bind.add_argument("--export-parity", required=True)
    candidate_bind.add_argument("--dependency-complexity-rank", type=int)
    candidate_bind.add_argument("--output", required=True)
    _tracking_arguments(candidate_bind)
    candidate_bind.set_defaults(handler=_candidate_bind)

    candidate_select = candidate_commands.add_parser(
        "select", help="freeze one candidate using validation and Pi benchmark only"
    )
    candidate_select.add_argument("candidates", nargs="+")
    candidate_select.add_argument("--output", required=True)
    _tracking_arguments(candidate_select)
    candidate_select.set_defaults(handler=_candidate_select)

    candidate_frozen_test = candidate_commands.add_parser(
        "frozen-test", help="evaluate the selected candidate once on frozen test"
    )
    candidate_frozen_test.add_argument("selection")
    candidate_frozen_test.add_argument("--fp32-reference", required=True)
    _frozen_data_arguments(candidate_frozen_test)
    candidate_frozen_test.add_argument(
        "--allow-external-split",
        action="store_true",
        help="allow an explicit external holdout for fine-tuned model release testing",
    )
    candidate_frozen_test.add_argument("--output", required=True)
    _tracking_arguments(candidate_frozen_test)
    candidate_frozen_test.set_defaults(handler=_candidate_frozen_test)

    candidate_finalize = candidate_commands.add_parser(
        "finalize", help="bind a selected candidate to a passing frozen-test report"
    )
    candidate_finalize.add_argument("selection")
    candidate_finalize.add_argument("--frozen-test", required=True)
    candidate_finalize.add_argument("--output", required=True)
    _tracking_arguments(candidate_finalize)
    candidate_finalize.set_defaults(handler=_candidate_finalize)

    benchmark = commands.add_parser(
        "benchmark", help="benchmark a candidate with production preprocessing"
    )
    benchmark.add_argument("model")
    _model_format_argument(benchmark)
    _frozen_data_arguments(benchmark)
    benchmark.add_argument("--output", required=True)
    benchmark.add_argument(
        "--power-condition",
        required=True,
        help="operator-declared benchmark power-supply condition",
    )
    benchmark.add_argument(
        "--cooling-condition",
        required=True,
        help="operator-declared benchmark cooling condition",
    )
    benchmark.add_argument(
        "--warmup-iterations",
        type=int,
        choices=(10,),
        default=10,
        help="fixed production benchmark warm-up count (must be 10)",
    )
    benchmark.add_argument(
        "--measured-iterations",
        type=int,
        choices=(100,),
        default=100,
        help="fixed production benchmark measurement count (must be 100)",
    )
    _tracking_arguments(benchmark)
    benchmark.set_defaults(handler=_benchmark)

    promote = commands.add_parser(
        "promote", help="build a production package from a finalized candidate"
    )
    promote.add_argument("finalized")
    promote.add_argument("--output", required=True)
    _frozen_data_arguments(promote)
    promote.add_argument(
        "--frozen-test-data",
        nargs="+",
        help=(
            "external frozen-test dataset root(s) or one composite manifest; "
            "requires --frozen-test-split-manifest"
        ),
    )
    promote.add_argument(
        "--frozen-test-split-manifest",
        help=(
            "persisted split for --frozen-test-data; when both options are omitted, "
            "--data/--split-manifest are reused"
        ),
    )
    promote.add_argument(
        "--cross-validation",
        action="append",
        required=True,
        metavar="REPORT",
        help=(
            "formally attested strict cross-validation report; repeat exactly "
            "once each for machine, paste_lot, and nozzle"
        ),
    )
    _model_identity_arguments(promote)
    _tracking_arguments(promote)
    promote.set_defaults(handler=_promote)

    infer = commands.add_parser("infer", help="run one promoted-package prediction")
    infer.add_argument("model_package")
    infer.add_argument("pre_image")
    infer.add_argument("post_image")
    infer.add_argument("--pixel-per-mm", type=float, required=True)
    infer.set_defaults(handler=_infer)

    activate = commands.add_parser(
        "activate", help="atomically switch an active-model pointer"
    )
    activate.add_argument("model_package")
    activate.add_argument("--pointer", required=True)
    activate.set_defaults(handler=_activate)

    rollback = commands.add_parser(
        "rollback", help="swap an active-model pointer back to its previous package"
    )
    rollback.add_argument("pointer")
    rollback.set_defaults(handler=_rollback)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return run_parser(
        build_parser(),
        argv,
        dependency_groups=_DEPENDENCY_GROUP_BY_MODULE,
    )


if __name__ == "__main__":
    raise SystemExit(main())
