"""推論 package の読み込みと実行.

Raspberry Pi 5 の実運転経路はこの module から始まる。

torch を import しないのは、process 起動から初回予測までの cold latency に
``import torch`` が数秒を直接足すため。

入出力は numpy 配列で受け渡す。

前処理が torch tensor を返す場合、呼び出し側が推論の直前で ``.numpy()`` する。

読み込みは package の検証から始める。

改竄・欠落・manifest との食い違い・runtime 版不足は、session を作る前に弾く。

graph 最適化は読み込み時に、その機体の上で行う。

最適化済み graph をファイルとして持ち歩くと独自 operator が混入するため。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import numpy as np
import onnxruntime
from numpy.typing import NDArray

from ml.artifact.package import ImmutablePackage
from ml.export.manifest import MANIFEST_FILENAME, InferenceManifest

type InputValues = Mapping[str, NDArray[np.float32]]

_CPU_PROVIDERS = ["CPUExecutionProvider"]


class OnnxInferenceModel:
    """検証済み package から作った ONNX Runtime session.

    session は可変資源なので、この class だけ ``attrs.frozen`` にしない。

    内部状態は ``_`` prefix で持ち、読み取り専用の property だけを公開する。
    """

    def __init__(
        self,
        *,
        manifest: InferenceManifest,
        package_path: Path,
        model_path: Path,
        session: onnxruntime.InferenceSession,
    ) -> None:
        self._manifest = manifest
        self._package_path = package_path
        self._model_path = model_path
        self._session = session

    @classmethod
    def load(
        cls,
        package: Path,
        *,
        onnxruntime_version: str | None = None,
    ) -> tuple[OnnxInferenceModel | None, str | None]:
        """Package を検証してから session を作る.

        ``onnxruntime_version`` を渡せる。

        実機へ持ち込む前に、学習機で版数の拒否を確認できるようにするため。
        """

        verified, error = ImmutablePackage.verify(package)
        if verified is None:
            return None, error
        manifest, error = InferenceManifest.load(verified.path / MANIFEST_FILENAME)
        if manifest is None:
            return None, error
        declared = set(manifest.payload_filenames())
        present = set(verified.checksums)
        if declared != present:
            return None, (
                "manifest が宣言する payload と package の中身が一致しません"
                f"（不足: {sorted(declared - present)}、"
                f"余分: {sorted(present - declared)}）"
            )
        version = (
            onnxruntime.__version__
            if onnxruntime_version is None
            else onnxruntime_version
        )
        if error := manifest.verify_runtime(onnxruntime_version=version):
            return None, error

        model_path = verified.path / manifest.model_filename
        options = onnxruntime.SessionOptions()
        options.graph_optimization_level = (
            onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
        )
        try:
            session = onnxruntime.InferenceSession(
                str(model_path), sess_options=options, providers=_CPU_PROVIDERS
            )
        except Exception as error:  # noqa: BLE001 - ORT の失敗は理由文字列にする
            return None, f"ONNX Runtime session を作れません: {model_path}（{error}）"
        return (
            cls(
                manifest=manifest,
                package_path=verified.path,
                model_path=model_path,
                session=session,
            ),
            None,
        )

    @property
    def manifest(self) -> InferenceManifest:
        """読み込んだ package の推論契約."""

        return self._manifest

    @property
    def package_path(self) -> Path:
        """読み込んだ package の絶対パス."""

        return self._package_path

    @property
    def model_path(self) -> Path:
        """Session が使っている ONNX ファイルの絶対パス."""

        return self._model_path

    def predict(
        self, inputs: InputValues
    ) -> tuple[dict[str, NDArray[np.float32]] | None, str | None]:
        """1 回ぶんの推論を実行し、出力名ごとの配列を返す.

        入力名や dtype の食い違いは例外にせず理由文字列で返す。
        """

        names = [value.name for value in self._session.get_outputs()]
        try:
            produced = cast(
                "list[NDArray[np.float32]]", self._session.run(names, dict(inputs))
            )
        except Exception as error:  # noqa: BLE001 - ORT の失敗は理由文字列にする
            return None, f"推論に失敗しました: {error}"
        return dict(zip(names, produced, strict=True)), None


__all__ = [
    "InputValues",
    "OnnxInferenceModel",
]
