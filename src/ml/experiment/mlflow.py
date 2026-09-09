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
from urllib.parse import urlsplit

import attrs
import mlflow
from mlflow.store.db.db_types import DATABASE_ENGINES

from ml.experiment.logger import ExperimentLogger, RunStatus, Scalar
from ml.experiment.provenance import sanitize_persisted_uri

# MLflow が filesystem backend とみなす tracking URI の scheme。
#
# 3.15 の file store は maintenance mode で、``MLFLOW_ALLOW_FILE_STORE=true`` を
# 立てない限り store を作る時点で例外になる。環境変数で使えたり使えなかったり
# する記録先は選ばない（仕様書 §3「環境変数を暗黙参照しない」）。
_FILE_STORE_SCHEMES = ("", "file")

# 置き場所の比較で file URI と素の path を同一視する scheme。
_LOCAL_PATH_SCHEMES = ("", "file")


def database_backend_scheme(tracking_uri: str) -> str | None:
    """Client が直接 database を開く tracking URI なら、その engine 名を返す.

    engine の一覧は MLflow 自身のもの（``DATABASE_ENGINES``）を引く。

    ここへ写すと、MLflow が engine を足したときに片方だけが古くなる。

    ``postgresql+psycopg2`` のような driver 付きの scheme も engine 名で判定する。
    """

    scheme = urlsplit(tracking_uri).scheme.split("+", maxsplit=1)[0]
    return scheme if scheme in DATABASE_ENGINES else None


def unusable_tracking_uri_reason(tracking_uri: str) -> str | None:
    """MLflow が store を作れない tracking URI なら理由を返す.

    filesystem backend（``file://`` と scheme なしの path）は maintenance mode で、
    ``MLFLOW_ALLOW_FILE_STORE=true`` を立てない限り ``MlflowException`` になる
    （3.15.2 で実測）。

    logger を組み立てる前に落とさないと、index も split も model も作ったあとで
    学習 loop の内側から raw な例外が出る。
    """

    if urlsplit(tracking_uri).scheme not in _FILE_STORE_SCHEMES:
        return None
    return (
        "MLflow の filesystem backend は使えません（maintenance mode。"
        "MLFLOW_ALLOW_FILE_STORE=true でしか動かない記録先は選びません）: "
        f"{tracking_uri!r}。"
        "sqlite:////abs/mlflow.db のような database backend か、"
        "http:// の tracking server を指定してください"
    )


@attrs.frozen
class MLflowRunTarget:
    """記録先の tracking server と experiment、および resume 対象の run."""

    tracking_uri: str
    experiment_name: str
    run_name: str | None = None
    resume_run_id: str | None = None

    artifact_location: str | None = None
    """Experiment を新規に作るときの成果物の置き場所.

    database backend の tracking store は、experiment を作るときに成果物の
    置き場所を決めないと現在 directory の下（``./mlruns``）を使う。

    起動した directory で成果物の所在が変わると、あとから run を開いても
    artifact を辿れない。

    既存の experiment には効かない（作成時にしか決められない）ので、要求と違う
    場所で作られていたら ``start`` が理由を添えて失敗する。
    """

    def validate(self) -> str | None:
        """記録先の指定が揃っているかを検証する."""

        if not self.tracking_uri:
            return "tracking_uri は空にできません"
        if not self.experiment_name:
            return "experiment_name は空にできません"
        return unusable_tracking_uri_reason(self.tracking_uri)

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
        self._select_experiment()
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

    def _select_experiment(self) -> None:
        """成果物の置き場所を確かめたうえで experiment を選ぶ.

        置き場所を指定しないと ``mlflow.set_experiment`` が現在 directory の
        相対 path を焼き付ける。

        置き場所は experiment を作るときにしか決められないので、要求と違う場所で
        既に作られていたら理由を添えて失敗する。

        黙って既存の場所を使うと、``artifact_location`` を渡した run の成果物が
        指定と別の場所（多くは起動した directory の ``./mlruns``）に落ちる。
        """

        name = self._target.experiment_name
        location = self._target.artifact_location
        existing = mlflow.get_experiment_by_name(name)
        if location is not None:
            if existing is None:
                mlflow.create_experiment(name, artifact_location=location)
            elif not _same_artifact_location(existing.artifact_location, location):
                raise ValueError(
                    f"experiment {name!r} の成果物の置き場所が要求と違います: "
                    f"既存 {existing.artifact_location!r}、"
                    f"要求 {location!r}"
                    "（artifact_location は experiment を作るときにしか"
                    "決められません。別の experiment_name を使ってください）"
                )
        mlflow.set_experiment(name)

    def _require_active(self) -> str:
        if self._run_id is None:
            raise RuntimeError("MLflow run をまだ start していません")
        if self._ended:
            raise RuntimeError("MLflow run はすでに end しています")
        return self._run_id


def _same_artifact_location(existing: str | None, requested: str) -> bool:
    """既存 experiment の置き場所が、要求した置き場所と同じかを返す."""

    if existing is None:
        return False
    return _normalized_artifact_location(existing) == _normalized_artifact_location(
        requested
    )


def _normalized_artifact_location(location: str) -> str:
    """``file://`` 付きと素の絶対 path を同じ表記へ寄せる."""

    parsed = urlsplit(location)
    path = parsed.path if parsed.scheme in _LOCAL_PATH_SCHEMES else location
    return path.rstrip("/")


__all__ = [
    "MLflowExperimentLogger",
    "MLflowRunTarget",
    "database_backend_scheme",
    "unusable_tracking_uri_reason",
]
