"""実験記録の最小契約と、固定タグを被せる decorator.

学習 core は「run を開始し、params / metrics / artifact / tag を記録して終える」
以上のことを知らない。

MLflow などの具体的な記録先はこの ABC の裏へ隠し、``ml.training`` から見えない
ようにする。

この module は ``ml-runtime`` すら要求しない。

torch も mlflow も import しないので、記録先を持たない環境でも読み込める。
"""

from __future__ import annotations

import abc
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, override

type Scalar = bool | int | float | str
type RunStatus = Literal["FINISHED", "FAILED", "KILLED"]


class ExperimentLogger(abc.ABC):
    """1 回の run を記録する境界.

    実装は ``start`` から ``end`` までを 1 本の run として扱う。

    ``end`` のあとに記録を続けることは契約違反であり、実装は
    :class:`RuntimeError` を送出してよい。
    """

    @property
    @abc.abstractmethod
    def run_id(self) -> str:
        """開始済み run の識別子.

        ``start`` より前に読むのは契約違反とする。
        """

    @abc.abstractmethod
    def start(
        self,
        *,
        run_kind: str,
        run_name: str | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> str:
        """Run を開始し、その識別子を返す."""

    @abc.abstractmethod
    def log_params(self, params: Mapping[str, Scalar]) -> None:
        """Run 中に変わらない設定値を記録する."""

    @abc.abstractmethod
    def log_metrics(self, metrics: Mapping[str, float], *, step: int) -> None:
        """時系列の metric を、指定した step 軸の位置へ記録する."""

    @abc.abstractmethod
    def log_artifact(self, path: Path, *, artifact_path: str | None = None) -> None:
        """既存ファイルを run の成果物として記録する."""

    @abc.abstractmethod
    def set_tags(self, tags: Mapping[str, str]) -> None:
        """Run のタグを追加・更新する."""

    @abc.abstractmethod
    def flush(self) -> None:
        """未送信の記録を送りきる."""

    @abc.abstractmethod
    def end(self, *, status: RunStatus = "FINISHED") -> None:
        """Run を指定した状態で終了する."""


class TaggedExperimentLogger(ExperimentLogger):
    """固定タグを必ず付けて内側の logger へ委譲する.

    ``start`` でタグを合成するとき、固定タグを呼び出し側の指定より優先する。

    provenance のような由来タグを、呼び出し側が黙って上書きできないようにするため。

    他のメソッドは内側の logger へ素通しする。
    """

    def __init__(self, inner: ExperimentLogger, *, tags: Mapping[str, str]) -> None:
        self._inner = inner
        self._tags = dict(tags)

    @property
    @override
    def run_id(self) -> str:
        """内側の logger が持つ run 識別子."""

        return self._inner.run_id

    @override
    def start(
        self,
        *,
        run_kind: str,
        run_name: str | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> str:
        """固定タグを勝たせた tag 集合で内側の run を開始する."""

        return self._inner.start(
            run_kind=run_kind,
            run_name=run_name,
            tags={**dict(tags or {}), **self._tags},
        )

    @override
    def log_params(self, params: Mapping[str, Scalar]) -> None:
        """内側の logger へ params を素通しする."""

        self._inner.log_params(params)

    @override
    def log_metrics(self, metrics: Mapping[str, float], *, step: int) -> None:
        """内側の logger へ metrics を素通しする."""

        self._inner.log_metrics(metrics, step=step)

    @override
    def log_artifact(self, path: Path, *, artifact_path: str | None = None) -> None:
        """内側の logger へ artifact を素通しする."""

        self._inner.log_artifact(path, artifact_path=artifact_path)

    @override
    def set_tags(self, tags: Mapping[str, str]) -> None:
        """内側の logger へ tag を素通しする."""

        self._inner.set_tags(tags)

    @override
    def flush(self) -> None:
        """内側の logger を flush する."""

        self._inner.flush()

    @override
    def end(self, *, status: RunStatus = "FINISHED") -> None:
        """内側の run を終了する."""

        self._inner.end(status=status)


__all__ = [
    "ExperimentLogger",
    "RunStatus",
    "Scalar",
    "TaggedExperimentLogger",
]
