"""Environment smoke command for the paste-volume ML workflow."""

from __future__ import annotations

import argparse
import platform
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ml.cli.tracked_operation import require_tracking_server_uri


def _exercise_model(device_name: str) -> list[dict[str, Any]]:
    import torch

    from ml.paste_volume.model import PasteVolumeResNet

    device = torch.device(device_name)
    model = PasteVolumeResNet().to(device)
    model.train()
    checks: list[dict[str, Any]] = []
    for height, width in ((64, 64), (1024, 256)):
        model.zero_grad(set_to_none=True)
        images = torch.randn((1, 6, height, width), device=device)
        valid_mask = torch.ones((1, 1, height, width), dtype=torch.bool, device=device)
        pixel_per_mm = torch.tensor([[30.0]], device=device)
        mean, log_variance = model(images, valid_mask, pixel_per_mm)
        if mean.shape != (1, 1) or log_variance.shape != (1, 1):
            raise RuntimeError("paste-volume model returned an unexpected output shape")
        (mean.sum() + log_variance.sum()).backward()
        if not any(parameter.grad is not None for parameter in model.parameters()):
            raise RuntimeError("paste-volume model backward produced no gradients")
        checks.append({"device": device_name, "height": height, "width": width})
    return checks


def _check_rgb_decode() -> dict[str, Any]:
    import torch
    from torchvision.io import ImageReadMode, decode_image, encode_png

    expected = torch.tensor(
        [[[11, 12]], [[21, 22]], [[31, 32]]],
        dtype=torch.uint8,
    )
    encoded = encode_png(expected)
    decoded = decode_image(encoded, mode=ImageReadMode.RGB)
    if not torch.equal(decoded, expected):
        raise RuntimeError("torchvision PNG decoding did not preserve RGB CHW values")
    return {"channel_order": "RGB", "shape": list(decoded.shape)}


def _compose_hydra(overrides: Sequence[str]) -> str:
    from hydra import compose, initialize_config_module
    from omegaconf import OmegaConf

    with initialize_config_module(
        config_module="ml.paste_volume.conf",
        version_base=None,
    ):
        config = compose(config_name="train", overrides=list(overrides))
    return OmegaConf.to_yaml(config, resolve=True)


def _check_mlflow(tracking_uri: str) -> dict[str, str]:
    import mlflow
    from mlflow import MlflowClient

    tracking_uri = require_tracking_server_uri(tracking_uri)
    mlflow.set_tracking_uri(tracking_uri)
    experiment = mlflow.set_experiment("pcbasm-paste-volume-smoke")
    with tempfile.TemporaryDirectory(prefix="pcbasm-ml-smoke-") as temporary_directory:
        artifact = Path(temporary_directory) / "smoke.txt"
        artifact.write_text("pcbasm paste-volume smoke\n", encoding="utf-8")
        with mlflow.start_run(experiment_id=experiment.experiment_id) as run:
            run_id = run.info.run_id
            mlflow.set_tag("run_kind", "smoke")
            mlflow.log_metric("smoke_metric", 1.0)
            mlflow.log_artifact(str(artifact), artifact_path="smoke")

        client = MlflowClient(tracking_uri=tracking_uri)
        stored = client.get_run(run_id)
        if stored.data.metrics.get("smoke_metric") != 1.0:
            raise RuntimeError("MLflow metric readback failed")
        destination = Path(temporary_directory) / "download"
        downloaded = Path(
            client.download_artifacts(run_id, "smoke/smoke.txt", str(destination))
        )
        if downloaded.read_text(encoding="utf-8") != artifact.read_text(
            encoding="utf-8"
        ):
            raise RuntimeError("MLflow artifact readback failed")
    return {"experiment_id": experiment.experiment_id, "run_id": run_id}


def smoke(args: argparse.Namespace) -> Any:
    import hydra
    import mlflow
    import onnx
    import onnxruntime
    import onnxscript
    import optuna
    import torch
    import torchvision

    if args.require_cuda and not torch.cuda.is_available():
        raise RuntimeError("CUDA is required but torch.cuda.is_available() is false")

    versions = {
        "cuda": torch.version.cuda,
        "cudnn": torch.backends.cudnn.version(),
        "hydra": hydra.__version__,
        "mlflow": mlflow.__version__,
        "onnx": onnx.__version__,
        "onnxruntime": onnxruntime.__version__,
        "onnxscript": onnxscript.__version__,
        "optuna": optuna.__version__,
        "python": platform.python_version(),
        "torch": torch.__version__,
        "torchvision": torchvision.__version__,
    }
    devices = ["cpu"]
    if torch.cuda.is_available():
        devices.append("cuda")
    model_checks = [check for device in devices for check in _exercise_model(device)]
    hydra_overrides = args.hydra_override or [
        "data.manifest=/tmp/pcbasm-paste-volume-smoke.composite.json",
        "trainer.max_epochs=1",
        "trainer.compile_enabled=false",
        f"logger.tracking_uri={args.tracking_uri}",
    ]
    hydra_yaml = _compose_hydra(hydra_overrides)
    mlflow_result = _check_mlflow(args.tracking_uri)
    return {
        "hydra_config": hydra_yaml,
        "mlflow": mlflow_result,
        "model_checks": model_checks,
        "rgb_decode": _check_rgb_decode(),
        "versions": versions,
    }
