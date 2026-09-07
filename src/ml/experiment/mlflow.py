"""MLflow tracking server への :class:`ExperimentLogger` adapter.

MLflow の autolog は使わず、記録するものを呼び出し側が明示する。

metric は同期送信し、この adapter 自身は再送キューを持たない。

キューを持つと「送れたつもり」の状態が生まれ、run の記録が実態とずれるため。

送信できない tracking server は ``start`` の時点で失敗させる。

この module だけが ``mlflow`` を import する。

``ml.training`` は :class:`ExperimentLogger` しか知らないので、``ml-runtime``
だけの環境でも学習経路は import できる。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import override

import attrs
import mlflow

from ml.experiment.logger import ExperimentLogger, RunStatus, Scalar
from ml.experiment.provenance import sanitize_persisted_uri


@attrs.frozen
class MLflowRunTarget:
    """記録先の tracking server と experiment、および resume 対象の run."""

    tracking_uri: str
    experiment_name: str
    run_name: str | None = None
    resume_run_id: str | None = None

    def validate(self) -> str | None:
        """記録先の指定が揃っているかを検証する."""

        if not self.tracking_uri:
            return "tracking_uri は空にできません"
        if not self.experiment_name:
            return "experiment_name は空にできません"
        return None

    @property
    def sanitized_tracking_uri(self) -> str:
        """Credential と接続オプションを落とした tracking URI.

        params やタグへ残すときはこちらを使う。
        """

        return sanitize_persisted_uri(self.tracking_uri)


class MLflowExperimentLogger(ExperimentLogger):
    """MLflow の run 1 本を、``start`` から ``end`` まで受け持つ adapter.

    ``end`` のあとの操作はすべて :class:`RuntimeError` にする。

    終了済み run へ書き込んだ記録は MLflow 上で追跡できないため。
    """

    def __init__(self, target: MLflowRunTarget) -> None:
        if error := target.validate():
            raise ValueError(error)
        self._target = target
        self._run_id: str | None = None
        self._ended = False

    @property
    @override
    def run_id(self) -> str:
        """開始済み MLflow run の識別子."""

        return self._require_active()

    @override
    def start(
        self,
        *,
        run_kind: str,
        run_name: str | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> str:
        """Tracking server へ接続し、run を開始して識別子を返す."""

        if self._ended:
            raise RuntimeError("end 済みの MLflow run は start し直せません")
        if self._run_id is not None:
            raise RuntimeError("MLflow run はすでに start しています")
        mlflow.set_tracking_uri(self._target.tracking_uri)
        mlflow.set_experiment(self._target.experiment_name)
        merged_tags = {"run_kind": run_kind, **dict(tags or {})}
        if self._target.resume_run_id is None:
            run = mlflow.start_run(
                run_name=run_name or self._target.run_name, tags=merged_tags
            )
        else:
            run = mlflow.start_run(run_id=self._target.resume_run_id)
        self._run_id = str(run.info.run_id)
        if self._target.resume_run_id is not None:
            mlflow.set_tags(merged_tags)
        return self._run_id

    @override
    def log_params(self, params: Mapping[str, Scalar]) -> None:
        """Run の設定値を記録する."""

        self._require_active()
        mlflow.log_params(dict(params))

    @override
    def log_metrics(self, metrics: Mapping[str, float], *, step: int) -> None:
        """Metric を同期送信で記録する."""

        self._require_active()
        mlflow.log_metrics(
            {name: float(value) for name, value in metrics.items()},
            step=step,
            synchronous=True,
        )

    @override
    def log_artifact(self, path: Path, *, artifact_path: str | None = None) -> None:
        """既存ファイルを run の成果物として登録する."""

        self._require_active()
        if not path.is_file():
            raise FileNotFoundError(path)
        mlflow.log_artifact(str(path), artifact_path=artifact_path)

    @override
    def set_tags(self, tags: Mapping[str, str]) -> None:
        """Run のタグを追加・更新する."""

        self._require_active()
        mlflow.set_tags(dict(tags))

    @override
    def flush(self) -> None:
        """非同期送信の記録を送りきる."""

        self._require_active()
        mlflow.flush_async_logging()

    @override
    def end(self, *, status: RunStatus = "FINISHED") -> None:
        """未送信の記録を送りきってから run を終了する."""

        self._require_active()
        mlflow.flush_async_logging()
        mlflow.end_run(status=status)
        self._ended = True

    def _require_active(self) -> str:
        if self._run_id is None:
            raise RuntimeError("MLflow run をまだ start していません")
        if self._ended:
            raise RuntimeError("MLflow run はすでに end しています")
        return self._run_id


__all__ = [
    "MLflowExperimentLogger",
    "MLflowRunTarget",
]
