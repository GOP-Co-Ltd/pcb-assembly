"""Lazy ONNX Runtime session, signature, metadata, and execution mechanics."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class OnnxSessionError(RuntimeError):
    """ONNX Runtime is unavailable or a session cannot be initialized."""


class OnnxSignatureError(OnnxSessionError):
    """A loaded graph does not implement its declared tensor contract."""


class OnnxRunError(RuntimeError):
    """ONNX Runtime failed while evaluating one input feed."""


@dataclass(frozen=True)
class OnnxTensorSpec:
    name: str
    element_type: str

    def __post_init__(self) -> None:
        if not self.name or not self.element_type:
            raise ValueError("ONNX tensor name and element type must be non-empty")


@dataclass(frozen=True)
class OnnxSignature:
    inputs: tuple[OnnxTensorSpec, ...]
    outputs: tuple[OnnxTensorSpec, ...]

    def __post_init__(self) -> None:
        for kind, values in (("input", self.inputs), ("output", self.outputs)):
            names = tuple(item.name for item in values)
            if not names or len(set(names)) != len(names):
                raise ValueError(
                    f"ONNX {kind} tensor names must be non-empty and unique"
                )


class OnnxSession:
    """Small, format-neutral wrapper around one initialized ORT session."""

    def __init__(self, session: Any, *, runtime_version: str) -> None:
        self._session = session
        self._runtime_version = runtime_version

    @classmethod
    def load_cpu(
        cls,
        model_path: Path,
        *,
        minimum_version: str | None = None,
        optimize_graph: bool = True,
    ) -> OnnxSession:
        """Load one CPU session without importing ONNX Runtime at module
        import."""

        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - dependency error only
            raise OnnxSessionError("ONNX Runtime is not installed") from exc

        if minimum_version is not None and _version_tuple(
            ort.__version__
        ) < _version_tuple(minimum_version):
            raise OnnxSessionError(
                f"onnxruntime>={minimum_version} is required: installed={ort.__version__}"
            )
        if "CPUExecutionProvider" not in set(ort.get_available_providers()):
            raise OnnxSessionError("ONNX Runtime CPUExecutionProvider is unavailable")

        options = ort.SessionOptions()
        if optimize_graph:
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        try:
            session = ort.InferenceSession(
                str(Path(model_path)),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:
            raise OnnxSessionError(
                f"ONNX Runtime session initialization failed: {exc}"
            ) from exc
        return cls(session, runtime_version=ort.__version__)

    @property
    def runtime_version(self) -> str:
        return self._runtime_version

    @property
    def metadata(self) -> Mapping[str, str]:
        return dict(self._session.get_modelmeta().custom_metadata_map)

    @property
    def signature(self) -> OnnxSignature:
        return OnnxSignature(
            inputs=tuple(
                OnnxTensorSpec(item.name, item.type)
                for item in self._session.get_inputs()
            ),
            outputs=tuple(
                OnnxTensorSpec(item.name, item.type)
                for item in self._session.get_outputs()
            ),
        )

    def require_signature(self, expected: OnnxSignature) -> None:
        actual = self.signature
        if actual.inputs != expected.inputs:
            raise OnnxSignatureError(
                f"ONNX input signature mismatch: {dict(_pairs(actual.inputs))}"
            )
        if actual.outputs != expected.outputs:
            raise OnnxSignatureError(
                f"ONNX output signature mismatch: {dict(_pairs(actual.outputs))}"
            )

    def run(
        self,
        output_names: Sequence[str],
        feeds: Mapping[str, object],
    ) -> tuple[object, ...]:
        """Evaluate explicit outputs and normalize ORT failures at the
        boundary."""

        try:
            result = self._session.run(list(output_names), dict(feeds))
        except Exception as exc:
            raise OnnxRunError(f"ONNX Runtime inference failed: {exc}") from exc
        return tuple(result)


def onnxruntime_version() -> str:
    """Return the installed runtime version without constructing a session."""

    try:
        import onnxruntime as ort
    except ImportError as exc:  # pragma: no cover - dependency error only
        raise OnnxSessionError("ONNX Runtime is not installed") from exc
    return ort.__version__


def _pairs(specs: Sequence[OnnxTensorSpec]) -> tuple[tuple[str, str], ...]:
    return tuple((item.name, item.element_type) for item in specs)


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(number) for number in re.findall(r"\d+", value)[:3])
