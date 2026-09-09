"""探索 study の同一性・storage・成果物.

study 名は内容から決まる。

同じ model と dataset と探索空間に対する探索は 1 個の study へ合流させたいので、
名前を人が付けると綴りの揺れで別 study に分かれてしまう。

storage は永続のものだけを許す。

in-memory storage はプロセスをまたげず、「並列 run を 1 個の study へ合流させる」
という前提を静かに壊すため。

成果物には秘匿済みの storage URI しか載せない。

探索結果は共有・添付される前提なので、credential を含む文字列を残さない。

秘匿は「解釈できる形の URI」に限らない。

検証に失敗する URI ほど credential を含んでいる可能性が高いので、理由文字列にも
秘匿済みの値しか載せない。

この module は optuna を import しない。

``ml-runtime`` すら install していない環境で成果物を読めるようにするため
（探索空間の宣言は :mod:`ml.tuning.search_space` 側に置く）。
"""

from __future__ import annotations

import math
import operator
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import ClassVar, Literal

import attrs
from cattrs import Converter

from ml.artifact.document import DocumentKind
from ml.experiment.logger import Scalar

type Direction = Literal["minimize", "maximize"]

DIRECTIONS: tuple[Direction, ...] = ("minimize", "maximize")

_COMPLETE_STATE = "COMPLETE"
_FINGERPRINT_PREFIX = "sha256:"
_SHORT_FINGERPRINT_LENGTH = 12
_UNSAFE_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_.-]+")
_SCHEME_SEPARATOR = "://"
_SCHEME_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*")
_UNREADABLE_URI = "<解釈できない storage URI>"
_SQLITE_SCHEME = "sqlite"
_SERVER_SCHEMES = frozenset({"postgresql", "postgresql+psycopg", "mysql"})


@attrs.frozen
class StudyIdentity:
    """内容から決まる study の同一性.

    3 要素のどれかが変われば別の study になる。

    空間の違う trial を混ぜると best trial の意味が変わるため。
    """

    model_family: str
    dataset_fingerprint: str
    search_space_fingerprint: str

    @classmethod
    def build(
        cls,
        *,
        model_family: str,
        dataset_fingerprint: str,
        search_space_fingerprint: str,
    ) -> tuple[StudyIdentity | None, str | None]:
        """3 要素を検証してから同一性を作る."""

        identity = cls(
            model_family=model_family,
            dataset_fingerprint=dataset_fingerprint,
            search_space_fingerprint=search_space_fingerprint,
        )
        if error := identity.validate():
            return None, error
        return identity, None

    def validate(self) -> str | None:
        """Study 名を作れる 3 要素が揃っているかを検証する."""

        if not _safe_name(self.model_family).strip("-"):
            return f"model_family は英数字を含む必要があります: {self.model_family!r}"
        if not _short_fingerprint(self.dataset_fingerprint):
            return (
                "dataset_fingerprint は空にできません: " f"{self.dataset_fingerprint!r}"
            )
        if not _short_fingerprint(self.search_space_fingerprint):
            return (
                "search_space_fingerprint は空にできません: "
                f"{self.search_space_fingerprint!r}"
            )
        return None

    @property
    def study_name(self) -> str:
        """RDB storage と実験記録で共有する決定論的な study 名."""

        return "-".join(
            (
                _safe_name(self.model_family),
                _short_fingerprint(self.dataset_fingerprint),
                _short_fingerprint(self.search_space_fingerprint),
            )
        )


@attrs.frozen
class StudyStorage:
    """複数プロセスが共有する trial の置き場所."""

    uri: str

    def validate(self) -> str | None:
        """プロセスをまたいで共有できる storage かを検証する.

        理由文字列へ載せるのは :attr:`redacted_uri` だけとする。

        生の URI を埋めると、検証に失敗した URI の credential がログへ流れる。

        :attr:`redacted_uri` が代替表現へ落ちる URI は必ず拒否する。

        受けてしまうと、成果物へ storage を識別できない文字列が記録される。
        """

        if not self.uri:
            return "storage URI は空にできません"
        scheme, host, path = _split_uri(self.uri)
        if not scheme:
            return f"storage URI は scheme://... の形が必要です: {self.redacted_uri}"
        if self.redacted_uri == _UNREADABLE_URI:
            # 秘匿した形で記録できない URI は最初から受けない。受けると成果物の
            # storage_uri_redacted から storage の識別情報が 1 文字も残らない。
            return (
                "storage URI を秘匿した形で記録できません。userinfo に含まれる "
                "'/' と '@' は percent-encode してください（'/' は %2F）"
            )
        if scheme == _SQLITE_SCHEME:
            database = path.removeprefix("/")
            if not database:
                return (
                    "in-memory の sqlite storage は共有できません: "
                    f"{self.redacted_uri}"
                )
            if not Path(database).is_absolute():
                return (
                    "sqlite storage は絶対パスが必要です（sqlite:////... の 4 連 "
                    f"slash）: {self.redacted_uri}"
                )
            return None
        if scheme in _SERVER_SCHEMES:
            if not host.partition(":")[0]:
                return f"storage URI に host がありません: {self.redacted_uri}"
            if not path.strip("/"):
                return f"storage URI に database 名がありません: {self.redacted_uri}"
            return None
        return (
            f"未対応の storage scheme です: {scheme!r}"
            f"（{_SQLITE_SCHEME} と {sorted(_SERVER_SCHEMES)} のみ）"
        )

    @property
    def redacted_uri(self) -> str:
        """Credential を落とした、記録に載せてよい URI.

        userinfo に加えて query と fragment も丸ごと落とす。

        この値は成果物へ記録されるので、storage の識別に要らない部分は残さない。

        ``scheme://`` の形に読めない URI は 1 文字も返さず固定の代替表現へ落とす。

        どこまでが credential か決められない入力を素通しすると秘匿が破れる。

        検証を通っていない URI でも呼べるようにする。

        失敗の理由文字列にも使うため、例外を投げると秘匿できない経路ができる。
        """

        scheme, host, path = _split_uri(self.uri)
        if not scheme:
            return _UNREADABLE_URI
        redacted = f"{scheme}{_SCHEME_SEPARATOR}{host}{path}"
        if "@" in redacted:
            # userinfo に生の "/" が混じった形。どこからが credential か決められない。
            return _UNREADABLE_URI
        return redacted


@attrs.frozen
class TrialRecord:
    """1 個の trial の結果.

    ``state`` は optuna の ``TrialState`` 名をそのまま持つ。
    """

    number: int
    state: str
    value: float | None
    parameters: Mapping[str, Scalar]
    experiment_run_id: str | None = None

    def validate(self) -> str | None:
        """Trial 記録の整合を検証する."""

        if self.number < 0:
            return f"trial 番号は 0 以上が必要です: {self.number}"
        if not self.state:
            return f"trial {self.number} の state が空です"
        if self.value is not None and not math.isfinite(self.value):
            return f"trial {self.number} の value が非有限です: {self.value}"
        return None

    @property
    def is_complete(self) -> bool:
        """この trial が完走したか."""

        return self.state == _COMPLETE_STATE


@attrs.frozen
class StudyResults:
    """1 個の study の探索結果.

    storage URI は秘匿済みのものしか持たない。
    """

    study_name: str
    storage_uri_redacted: str
    direction: Direction
    search_space_fingerprint: str
    trials: tuple[TrialRecord, ...]

    DOCUMENT_KIND: ClassVar[DocumentKind] = DocumentKind(
        kind="ml-hyperparameter-search-results", schema_version=1
    )

    @classmethod
    def build(
        cls,
        *,
        identity: StudyIdentity,
        storage: StudyStorage,
        direction: Direction,
        trials: Sequence[TrialRecord],
    ) -> tuple[StudyResults | None, str | None]:
        """同一性と storage から成果物を組み立てる."""

        if error := identity.validate():
            return None, error
        if error := storage.validate():
            return None, error
        results = cls(
            study_name=identity.study_name,
            storage_uri_redacted=storage.redacted_uri,
            direction=direction,
            search_space_fingerprint=identity.search_space_fingerprint,
            trials=tuple(trials),
        )
        if error := results.validate():
            return None, error
        return results, None

    def validate(self) -> str | None:
        """成果物の整合を検証する."""

        if not self.study_name:
            return "study_name は空にできません"
        if not self.storage_uri_redacted:
            return "storage_uri_redacted は空にできません"
        if self.direction not in DIRECTIONS:
            return f"direction は {list(DIRECTIONS)} のいずれかが必要です: {self.direction!r}"
        if not self.search_space_fingerprint:
            return "search_space_fingerprint は空にできません"
        for trial in self.trials:
            if error := trial.validate():
                return error
        numbers = [trial.number for trial in self.trials]
        duplicated = sorted({number for number in numbers if numbers.count(number) > 1})
        if duplicated:
            return f"trial 番号が重複しています: {duplicated}"
        return None

    @property
    def best_trial(self) -> TrialRecord | None:
        """Direction に従って選んだ最良の完走 trial."""

        scored = [
            (trial.value, trial)
            for trial in self.trials
            if trial.is_complete and trial.value is not None
        ]
        if not scored:
            return None
        select = max if self.direction == "maximize" else min
        return select(scored, key=operator.itemgetter(0))[1]

    @property
    def completed_trial_count(self) -> int:
        """完走した trial の数."""

        return sum(1 for trial in self.trials if trial.is_complete)

    def verify_lineage(self) -> str | None:
        """完走した trial が実験記録へ紐付いているかを検証する.

        紐付けが無い trial は、あとから「どの run がその値を出したか」を辿れない。

        探索の途中で落ちた trial は run を持たないことがあるので、完走した trial だけを対象にする。
        """

        missing = sorted(
            trial.number
            for trial in self.trials
            if trial.is_complete and not trial.experiment_run_id
        )
        if missing:
            return f"完走した trial に experiment_run_id がありません: {missing}"
        return None

    def save(self, path: Path, *, converter: Converter) -> None:
        """エンベロープ付き JSON として atomic に書き出す."""

        self.DOCUMENT_KIND.save(path, self, converter=converter)

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        converter: Converter,
    ) -> tuple[StudyResults | None, str | None]:
        """書き出した成果物を読み、失敗したら理由を返す."""

        results, error = cls.DOCUMENT_KIND.load(path, cls, converter=converter)
        if results is None:
            return None, error
        if error := results.validate():
            return None, error
        return results, None


def _split_uri(uri: str) -> tuple[str, str, str]:
    """``scheme://host/path`` を 3 つに割り、query と fragment を落とす.

    ``scheme://`` の形に読めなければ scheme を空文字で返す。

    scheme の大小は正規化しない。

    ``SQLITE://...`` を小文字へ寄せて通すと optuna 側が ``NoSuchModuleError`` で落ちる。
    許可リストと突き合わせて理由文字列で拒否したい。

    :func:`~urllib.parse.urlsplit` は使わない。

    不正な IPv6 表記（``postgresql://h@[::1/db``）で ``ValueError`` を投げるため、
    理由文字列を返す約束と秘匿の両方が破れる。
    """

    identifying = uri.split("#", 1)[0].split("?", 1)[0]
    scheme, separator, rest = identifying.partition(_SCHEME_SEPARATOR)
    if not separator or not _SCHEME_PATTERN.fullmatch(scheme):
        return "", "", ""
    authority, slash, path = rest.partition("/")
    return scheme, authority.rpartition("@")[2], f"{slash}{path}"


def _safe_name(value: str) -> str:
    """Study 名に使えない文字を ``-`` へ潰す."""

    return _UNSAFE_NAME_PATTERN.sub("-", value)


def _short_fingerprint(value: str) -> str:
    """``sha256:`` 接頭辞を外し、study 名へ載せる長さに切る."""

    return _safe_name(value.removeprefix(_FINGERPRINT_PREFIX))[
        :_SHORT_FINGERPRINT_LENGTH
    ]


__all__ = [
    "DIRECTIONS",
    "Direction",
    "StudyIdentity",
    "StudyResults",
    "StudyStorage",
    "TrialRecord",
]
