#!/usr/bin/env python3
"""ML 開発環境の smoke check.

`画像ベース吐出量推定 ML 実装計画 <../docs/image-based-dispense-calibration-ml-plan.md>`_
の「開発環境の確認」に対応する。依存を入れ替えたあと、学習を回す前に 1 回叩いて
version と最低限の動作を固定する。

各 check は ``ok`` / ``skip`` / ``fail`` を 1 行で報告し、``fail`` が 1 件でもあれば
終了コード 1 を返す。``skip`` は環境がその check の対象でないことを意味する
（GPU が無い、MLflow tracking server を指定していない、対象がまだ未実装）。

`make ml-smoke` から実行する。MLflow の check は tracking server が要るので
``--mlflow-tracking-uri`` を渡したときだけ実行する。
"""

from __future__ import annotations

import argparse
import importlib.metadata
import platform
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

# 計画の v1 encoder（3 段 stem + 3 residual stage）。smoke の目的は形の確認なので
# ドメイン側の model 定義ではなく、この値をここに直接置く。
_INPUT_CHANNELS = 6
_STEM_CHANNELS = (24, 32, 48)
_STEM_STRIDES = (2, 2, 2)
_STAGE_CHANNELS = (48, 96, 160)
_STAGE_STRIDES = (1, 2, 2)
_BLOCKS_PER_STAGE = (2, 2, 2)

# 計画の「64 × 64 と 1024 × 256 の dummy input」。
_DUMMY_SHAPES = ((64, 64), (1024, 256))

_REPORTED_PACKAGES = (
    "torch",
    "torchvision",
    "hydra-core",
    "optuna",
    "mlflow",
    "onnx",
    "onnxruntime",
    "onnxscript",
)


class Report:
    """check 結果を順に出しつつ、失敗があったかを覚える."""

    def __init__(self) -> None:
        self._failed = False

    @property
    def failed(self) -> bool:
        """1 件でも fail したか."""

        return self._failed

    def ok(self, name: str, detail: str = "") -> None:
        """check が通ったことを報告する."""

        self._line("ok", name, detail)

    def skip(self, name: str, detail: str) -> None:
        """この環境が check の対象外であることを報告する."""

        self._line("skip", name, detail)

    def fail(self, name: str, detail: str) -> None:
        """check が落ちたことを報告する."""

        self._failed = True
        self._line("fail", name, detail)

    def _line(self, status: str, name: str, detail: str) -> None:
        suffix = f"  {detail}" if detail else ""
        print(f"[{status:>4}] {name}{suffix}", flush=True)


@contextmanager
def _checked(report: Report, name: str) -> Iterator[None]:
    """check 本体が投げた例外を fail 1 行へ落とす."""

    try:
        yield
    except Exception as error:  # smoke なので原因の型を問わず 1 行へ落とす
        report.fail(name, f"{type(error).__name__}: {error}")


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "未 install"


def check_versions(report: Report) -> None:
    """Python と ML 依存の version を表示する."""

    report.ok("python", f"{platform.python_version()} ({sys.executable})")
    for name in _REPORTED_PACKAGES:
        report.ok(f"version {name}", _package_version(name))

    with _checked(report, "version cuda"):
        import torch

        if not torch.cuda.is_available():
            report.skip("version cuda", "CUDA device がありません")
            return
        report.ok(
            "version cuda",
            f"torch build {torch.version.cuda}, cuDNN "
            f"{torch.backends.cudnn.version()}, "
            f"device {torch.cuda.get_device_name(0)}",
        )


def _convolution_forward_backward(device_name: str) -> str:
    """1 層の畳み込みで forward と backward を通し、勾配の形を返す."""

    import torch
    from torch import nn

    device = torch.device(device_name)
    convolution = nn.Conv2d(3, 4, kernel_size=3, padding=1).to(device)
    images = torch.randn(2, 3, 16, 16, device=device)
    convolution(images).square().mean().backward()
    gradient = convolution.weight.grad
    if gradient is None:
        raise AssertionError("畳み込み weight へ勾配が流れていません")
    if not bool(torch.isfinite(gradient).all()):
        raise AssertionError("畳み込み weight の勾配が非有限です")
    return f"gradient {tuple(gradient.shape)}"


def check_cpu_convolution(report: Report) -> None:
    """CPU で畳み込みの forward / backward が通る."""

    name = "cpu convolution"
    with _checked(report, name):
        report.ok(name, _convolution_forward_backward("cpu"))


def check_cuda_convolution(report: Report) -> None:
    """CUDA tensor で畳み込みの forward / backward が通る."""

    name = "cuda convolution"
    with _checked(report, name):
        import torch

        if not torch.cuda.is_available():
            report.skip(name, "CUDA device がありません")
            return
        report.ok(name, _convolution_forward_backward("cuda"))


def check_model_forward(report: Report) -> None:
    """v1 と同じ形の model が dummy input を forward できる."""

    name = "model forward"
    with _checked(report, name):
        import torch

        from ml.model.blocks import ImageEncoder, ImageEncoderConfig
        from ml.model.heads import (
            GaussianHeadConfig,
            GaussianImageRegressor,
            GaussianRegressionHead,
        )

        encoder_config = ImageEncoderConfig(
            input_channels=_INPUT_CHANNELS,
            stem_channels=_STEM_CHANNELS,
            stem_strides=_STEM_STRIDES,
            stage_channels=_STAGE_CHANNELS,
            stage_strides=_STAGE_STRIDES,
            blocks_per_stage=_BLOCKS_PER_STAGE,
        )
        encoder = ImageEncoder(encoder_config)
        head = GaussianRegressionHead(
            GaussianHeadConfig(
                input_features=encoder_config.output_features,
                conditioning_features=1,
            )
        )
        model = GaussianImageRegressor(encoder, head).eval()

        parameter_count = sum(
            parameter.numel() for parameter in model.parameters(recurse=True)
        )
        for height, width in _DUMMY_SHAPES:
            images = torch.zeros(1, _INPUT_CHANNELS, height, width)
            valid_pixel_mask = torch.ones(1, 1, height, width, dtype=torch.bool)
            pixel_per_mm = torch.zeros(1, 1)
            with torch.inference_mode():
                mean, log_variance = model(images, valid_pixel_mask, pixel_per_mm)
            if tuple(mean.shape) != (1, 1) or tuple(log_variance.shape) != (1, 1):
                raise AssertionError(
                    f"{height}x{width} の出力 shape が [1, 1] ではありません: "
                    f"{tuple(mean.shape)}、{tuple(log_variance.shape)}"
                )
        shapes = ", ".join(f"{height}x{width}" for height, width in _DUMMY_SHAPES)
        report.ok(name, f"{parameter_count} parameter、{shapes} を forward")


def check_png_decode(report: Report) -> None:
    """torchvision が lossless PNG を RGB の CHW tensor として読む."""

    name = "png decode"
    with _checked(report, name):
        import torch
        from torchvision.io import write_png

        from ml.data.image import decode_rgb_image

        # channel ごとに違う値を置き、RGB 順が入れ替わったら落ちるようにする
        expected = torch.zeros(3, 4, 5, dtype=torch.uint8)
        expected[0] = 10
        expected[1] = 120
        expected[2] = 230
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controlled-rgb.png"
            write_png(expected, str(path))
            decoded = decode_rgb_image(path)
        if not torch.equal(decoded, expected):
            raise AssertionError(
                "decode 結果が期待値と一致しません: "
                f"channel 平均 {decoded.float().mean(dim=(1, 2)).tolist()}"
            )
        report.ok(name, f"shape {tuple(decoded.shape)}、dtype {decoded.dtype}")


def check_hydra_compose(report: Report) -> None:
    """packaged Hydra config を compose して解決済み設定を表示する."""

    report.skip(
        "hydra compose",
        "packaged config (pcbasm.pasting.paste_volume.conf) は未実装",
    )


def check_mlflow(report: Report, tracking_uri: str | None) -> None:
    """MLflow tracking server へ run、metric、artifact を書いて読み戻す."""

    name = "mlflow tracking"
    if tracking_uri is None:
        report.skip(name, "--mlflow-tracking-uri を渡すと実行する")
        return

    with _checked(report, name):
        import mlflow

        mlflow.set_tracking_uri(tracking_uri)
        client = mlflow.MlflowClient(tracking_uri=tracking_uri)
        mlflow.set_experiment("pcbasm-ml-smoke")
        with mlflow.start_run(run_name="smoke") as run:
            run_id = run.info.run_id
            mlflow.log_param("smoke", "1")
            mlflow.log_metric("smoke_metric", 1.5, step=0)
            with tempfile.TemporaryDirectory() as directory:
                artifact = Path(directory) / "smoke.txt"
                artifact.write_text("smoke\n", encoding="utf-8")
                mlflow.log_artifact(str(artifact))

        stored = client.get_run(run_id)
        if stored.data.metrics.get("smoke_metric") != 1.5:
            raise AssertionError(
                f"metric を読み戻せません: {stored.data.metrics}",
            )
        artifacts = [item.path for item in client.list_artifacts(run_id)]
        if "smoke.txt" not in artifacts:
            raise AssertionError(f"artifact を読み戻せません: {artifacts}")
        report.ok(name, f"run {run_id} を読み戻し")


def main(argv: list[str] | None = None) -> int:
    """smoke check を順に実行し、fail があれば 1 を返す."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mlflow-tracking-uri",
        default=None,
        help="MLflow tracking server の URI。渡すと run の書き戻しまで確認する",
    )
    arguments = parser.parse_args(argv)

    report = Report()
    checks: tuple[Callable[[Report], None], ...] = (
        check_versions,
        check_cpu_convolution,
        check_cuda_convolution,
        check_model_forward,
        check_png_decode,
        check_hydra_compose,
    )
    for check in checks:
        check(report)
    check_mlflow(report, arguments.mlflow_tracking_uri)

    print()
    print("smoke check: " + ("失敗あり" if report.failed else "すべて通過"))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
