"""探索 study の同一性・storage・成果物の公開契約.

計画 §13.3 / §15.3 に対応する。この層は optuna を import しない純関数だけで構成する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ml.serialization import make_strict_converter
from ml.tuning.study import (
    Direction,
    StudyIdentity,
    StudyResults,
    StudyStorage,
    TrialRecord,
)

# 秘匿が破れうる形を網羅する。``://`` を持つ形しか通していなかったため、
# 変異 #19 / #58 は must-fix 1 を取り逃していた。
CREDENTIAL_URIS = (
    "postgresql://operator:secret@db.example:5433/hpo",
    "postgresql://db.example:5433/hpo?password=secret",
    "postgresql://db.example:5433/hpo#secret",
    # ``://`` を持たない形。userinfo の位置を決められない
    "postgresql:operator:secret@host/db",
    "operator:secret@host/db",
    # scheme の位置に credential が来る形
    "operator:secret@host://db",
    # sqlite 分岐（query / fragment / userinfo つきの相対パス）
    "sqlite:///relative.db?password=secret",
    "sqlite:///relative.db#secret",
    "sqlite:///operator:secret@relative.db",
    "sqlite://?password=secret",
    # userinfo に生の "/" が混じった形
    "postgresql://operator:sec/secret@db.example/hpo",
    # 不正な port（``urlsplit(...).port`` が例外を投げる形）
    "postgresql://operator:secret@db.example:notaport/hpo",
    # 不正な IPv6 表記（``urlsplit`` 自体が例外を投げる形）
    "postgresql://operator:secret@[::1/hpo",
)

# 上のうち ``validate()`` が理由を返すもの。理由文字列にも credential を残さない。
REPORTED_CREDENTIAL_URIS = (
    "postgresql:operator:secret@host/db",
    "operator:secret@host/db",
    "operator:secret@host://db",
    "sqlite:///relative.db?password=secret",
    "sqlite:///relative.db#secret",
    "sqlite:///operator:secret@relative.db",
    "sqlite://?password=secret",
    "postgresql://operator:sec/secret@db.example/hpo",
)

# ``validate()`` が受ける形と拒否する形。「受理 ⇒ 記録できる」の不変条件に使う。
ACCEPTED_URIS = (
    "sqlite:////var/lib/pcbasm/hpo/study.db",
    "postgresql://host/db",
    "postgresql+psycopg://host/db",
    "mysql://host/db",
    "postgresql://operator:sec%2Fsecret@db.example/hpo",
)

REJECTED_URIS = (
    "sqlite:///study.db",
    "",
    "sqlite://",
    "redis://host/0",
    "postgresql://host",
    "postgresql:///db",
    "SQLITE:////var/lib/pcbasm/hpo/study.db",
    "POSTGRESQL://host/db",
)

# ``redacted_uri`` が識別を諦めたときに返す固定の代替表現。
UNREADABLE_URI = "<解釈できない storage URI>"

DATASET_HEX = "a" * 64
SPACE_HEX = "b" * 64
DATASET_FINGERPRINT = f"sha256:{DATASET_HEX}"
SPACE_FINGERPRINT = f"sha256:{SPACE_HEX}"


def _identity(
    *,
    model_family: str = "gaussian-regressor",
    dataset_fingerprint: str = DATASET_FINGERPRINT,
    search_space_fingerprint: str = SPACE_FINGERPRINT,
) -> StudyIdentity:
    identity, error = StudyIdentity.build(
        model_family=model_family,
        dataset_fingerprint=dataset_fingerprint,
        search_space_fingerprint=search_space_fingerprint,
    )
    assert error is None
    assert identity is not None
    return identity


def _storage() -> StudyStorage:
    return StudyStorage(uri="sqlite:////var/lib/pcbasm/hpo/study.db")


def _trial(
    number: int,
    *,
    state: str = "COMPLETE",
    value: float | None = 0.5,
    experiment_run_id: str | None = "run-0",
) -> TrialRecord:
    return TrialRecord(
        number=number,
        state=state,
        value=value,
        parameters={"trainer.learning_rate": 1e-4},
        experiment_run_id=experiment_run_id,
    )


def _results(
    *,
    direction: Direction = "minimize",
    trials: tuple[TrialRecord, ...] = (),
    storage: StudyStorage | None = None,
) -> StudyResults:
    results, error = StudyResults.build(
        identity=_identity(),
        storage=storage or _storage(),
        direction=direction,
        trials=trials,
    )
    assert error is None
    assert results is not None
    return results


class TestStudyIdentity:
    """3 要素から決まる study 名."""

    def test_builds_the_same_name_from_the_same_elements(self):
        assert _identity().study_name == _identity().study_name

    def test_sanitizes_the_model_family_and_truncates_the_fingerprints(self):
        identity = _identity(model_family="paste volume/v2")

        assert identity.study_name == (
            f"paste-volume-v2-{DATASET_HEX[:12]}-{SPACE_HEX[:12]}"
        )

    def test_drops_the_fingerprint_prefix(self):
        assert "sha256" not in _identity().study_name

    @pytest.mark.parametrize(
        ("model_family", "dataset_fingerprint", "search_space_fingerprint"),
        [
            ("other-family", DATASET_FINGERPRINT, SPACE_FINGERPRINT),
            ("gaussian-regressor", f"sha256:{'c' * 64}", SPACE_FINGERPRINT),
            ("gaussian-regressor", DATASET_FINGERPRINT, f"sha256:{'d' * 64}"),
        ],
    )
    def test_changes_the_name_when_any_element_changes(
        self,
        model_family: str,
        dataset_fingerprint: str,
        search_space_fingerprint: str,
    ):
        changed = _identity(
            model_family=model_family,
            dataset_fingerprint=dataset_fingerprint,
            search_space_fingerprint=search_space_fingerprint,
        )

        assert changed.study_name != _identity().study_name

    @pytest.mark.parametrize("model_family", ["", "   ", "///"])
    def test_reports_a_model_family_without_usable_characters(self, model_family: str):
        identity, error = StudyIdentity.build(
            model_family=model_family,
            dataset_fingerprint=DATASET_FINGERPRINT,
            search_space_fingerprint=SPACE_FINGERPRINT,
        )

        assert identity is None
        assert error is not None
        assert "model_family" in error

    @pytest.mark.parametrize(
        ("dataset_fingerprint", "search_space_fingerprint", "expected_field"),
        [
            ("", SPACE_FINGERPRINT, "dataset_fingerprint"),
            (DATASET_FINGERPRINT, "", "search_space_fingerprint"),
        ],
    )
    def test_reports_an_empty_fingerprint(
        self,
        dataset_fingerprint: str,
        search_space_fingerprint: str,
        expected_field: str,
    ):
        identity, error = StudyIdentity.build(
            model_family="gaussian-regressor",
            dataset_fingerprint=dataset_fingerprint,
            search_space_fingerprint=search_space_fingerprint,
        )

        assert identity is None
        assert error is not None
        assert expected_field in error


class TestStudyStorage:
    """永続 storage だけを許す検証と credential の秘匿."""

    @pytest.mark.parametrize("uri", ACCEPTED_URIS)
    def test_accepts_persistent_storage(self, uri: str):
        assert StudyStorage(uri=uri).validate() is None

    @pytest.mark.parametrize("uri", REJECTED_URIS)
    def test_reports_storage_that_cannot_be_shared(self, uri: str):
        assert StudyStorage(uri=uri).validate() is not None

    @pytest.mark.parametrize(
        "uri", ["SQLITE:////var/lib/pcbasm/hpo/study.db", "POSTGRESQL://host/db"]
    )
    def test_reports_an_upper_case_scheme(self, uri: str):
        """大文字の scheme は optuna が使えないので受けない.

        SQLAlchemy は ``NoSuchModuleError`` を投げるだけなので、こちらで理由を返す。
        """

        error = StudyStorage(uri=uri).validate()

        assert error is not None
        assert "scheme" in error

    def test_separates_in_memory_sqlite_from_a_relative_path(self):
        """``sqlite://`` と ``sqlite:///x.db`` を別の理由で拒否する."""

        # どちらも拒否されるので「拒否されたか」だけを見る検査では、
        # 一方の分岐を落としても気付けない。理由まで固定する。
        in_memory = StudyStorage(uri="sqlite://").validate()
        relative = StudyStorage(uri="sqlite:///study.db").validate()

        assert in_memory is not None
        assert relative is not None
        assert "in-memory" in in_memory
        assert "絶対パス" in relative

    def test_redacts_the_password(self):
        redacted = StudyStorage(
            uri="postgresql://operator:secret@db.example:5433/hpo"
        ).redacted_uri

        assert "secret" not in redacted
        assert "hpo" in redacted

    @pytest.mark.parametrize(
        "uri",
        [
            "postgresql://operator:secret@db.example:5433/hpo",
            "postgresql://db.example:5433/hpo?password=secret",
            "postgresql://db.example:5433/hpo#secret",
        ],
    )
    def test_drops_every_place_a_credential_can_hide(self, uri: str):
        """秘匿情報が隠れうる 3 箇所すべてを落とす（裁定 3）."""

        redacted = StudyStorage(uri=uri).redacted_uri

        assert "secret" not in redacted
        assert "db.example:5433" in redacted

    def test_keeps_the_port_in_the_redacted_uri(self):
        redacted = StudyStorage(
            uri="postgresql://operator:secret@db.example:5433/hpo"
        ).redacted_uri

        assert "db.example:5433" in redacted


class TestStudyStorageRedaction:
    """秘匿は「解釈できる形の URI」に限らず保証される（must-fix 1）.

    検証に失敗する URI ほど credential を含んでいる可能性が高い。
    """

    @pytest.mark.parametrize("uri", CREDENTIAL_URIS)
    def test_never_carries_a_credential_into_the_redacted_uri(self, uri: str):
        assert "secret" not in StudyStorage(uri=uri).redacted_uri

    @pytest.mark.parametrize("uri", CREDENTIAL_URIS)
    def test_never_raises_while_redacting(self, uri: str):
        """理由文字列にも使う値なので、例外を投げる経路を作らない."""

        # 不正な IPv6 表記は ``urlsplit`` が ValueError を投げる形
        assert isinstance(StudyStorage(uri=uri).redacted_uri, str)

    @pytest.mark.parametrize("uri", CREDENTIAL_URIS)
    def test_never_raises_while_validating(self, uri: str):
        """``validate`` は例外ではなく理由文字列で返す.

        不正な IPv6 表記は ``urlsplit`` 自体が ``ValueError`` を投げる形。
        """

        error = StudyStorage(uri=uri).validate()

        assert error is None or "secret" not in error

    @pytest.mark.parametrize("uri", REPORTED_CREDENTIAL_URIS)
    def test_never_carries_a_credential_into_a_reason_string(self, uri: str):
        error = StudyStorage(uri=uri).validate()

        assert error is not None
        assert "secret" not in error

    @pytest.mark.parametrize(
        "uri",
        [
            "postgresql:operator:secret@host/db",
            "operator:secret@host/db",
            "operator:secret@host://db",
            "postgresql://operator:sec/secret@db.example/hpo",
        ],
    )
    def test_falls_back_to_a_fixed_representation_when_it_cannot_parse(self, uri: str):
        """解釈できない URI は生の文字列を 1 文字も返さない."""

        redacted = StudyStorage(uri=uri).redacted_uri

        assert redacted == UNREADABLE_URI

    @pytest.mark.parametrize("uri", (*CREDENTIAL_URIS, *ACCEPTED_URIS, *REJECTED_URIS))
    def test_accepts_only_a_uri_it_can_record(self, uri: str):
        """``validate`` を通る URI は必ず秘匿した形で記録できる（不変条件）.

        代替表現を受けてしまうと、成果物から storage の識別情報が消える。
        """

        storage = StudyStorage(uri=uri)

        # 「受理 ⇒ 記録できる」の含意
        assert storage.validate() is not None or storage.redacted_uri != UNREADABLE_URI

    def test_reports_a_userinfo_that_needs_percent_encoding(self):
        """秘匿した形で記録できない userinfo は percent-encode を促して拒否する.

        SQLAlchemy は password に生の ``/`` を許すが、それでは userinfo と path の
        境界が決められず、成果物へ識別情報を残せない。
        """

        error = StudyStorage(
            uri="postgresql://operator:sec/secret@db.example/hpo"
        ).validate()

        assert error is not None
        assert "secret" not in error
        assert "percent-encode" in error

    def test_accepts_the_same_userinfo_once_it_is_percent_encoded(self):
        """``%2F`` へ percent-encode すれば受理し、識別情報も残る（対照）."""

        storage = StudyStorage(uri="postgresql://operator:sec%2Fsecret@db.example/hpo")

        assert storage.validate() is None
        assert storage.redacted_uri == "postgresql://db.example/hpo"

    def test_keeps_the_identifying_part_of_a_uri_it_can_parse(self):
        """解釈できる URI では識別に必要な部分を落とさない（対照）."""

        redacted = StudyStorage(
            uri="postgresql://operator:secret@db.example:5433/hpo?sslmode=require"
        ).redacted_uri

        assert redacted == "postgresql://db.example:5433/hpo"


class TestStudyResults:
    """成果物の組み立てと往復."""

    def test_fills_the_study_name_and_the_redacted_storage_uri(self):
        storage = StudyStorage(uri="postgresql://operator:secret@db.example:5433/hpo")

        results = _results(storage=storage)

        assert results.study_name == _identity().study_name
        assert results.storage_uri_redacted == storage.redacted_uri
        assert results.search_space_fingerprint == _identity().search_space_fingerprint

    @pytest.mark.parametrize(
        ("direction", "expected_number"), [("minimize", 1), ("maximize", 0)]
    )
    def test_selects_the_best_trial_by_direction(
        self, direction: Direction, expected_number: int
    ):
        trials = (_trial(0, value=0.9), _trial(1, value=0.1))

        results = _results(direction=direction, trials=trials)

        assert results.best_trial is not None
        assert results.best_trial.number == expected_number

    def test_does_not_consider_incomplete_trials_as_best(self):
        trials = (
            _trial(0, value=0.9),
            _trial(1, state="FAIL", value=0.0, experiment_run_id=None),
            _trial(2, state="PRUNED", value=0.0, experiment_run_id=None),
        )

        results = _results(trials=trials)

        assert results.best_trial is not None
        assert results.best_trial.number == 0

    @pytest.mark.parametrize(
        "trials",
        [
            (),
            (_trial(0, state="FAIL", value=None, experiment_run_id=None),),
        ],
    )
    def test_has_no_best_trial_without_a_completed_trial(
        self, trials: tuple[TrialRecord, ...]
    ):
        assert _results(trials=trials).best_trial is None

    def test_counts_only_completed_trials(self):
        trials = (
            _trial(0),
            _trial(1),
            _trial(2, state="FAIL", value=None, experiment_run_id=None),
        )

        assert _results(trials=trials).completed_trial_count == 2

    def test_round_trips_through_a_document(self, tmp_path: Path):
        converter = make_strict_converter()
        target = tmp_path / "hyperparameter_search_results.json"
        results = _results(trials=(_trial(0), _trial(1)))

        results.save(target, converter=converter)
        loaded, error = StudyResults.load(target, converter=converter)

        assert error is None
        assert loaded == results

    def test_reports_an_unknown_document_kind(self, tmp_path: Path):
        converter = make_strict_converter()
        target = tmp_path / "results.json"
        _results().save(target, converter=converter)
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["kind"] = "ml-other-document"
        target.write_text(json.dumps(payload), encoding="utf-8")

        loaded, error = StudyResults.load(target, converter=converter)

        assert loaded is None
        assert error is not None
        assert "ml-other-document" in error

    def test_reports_an_unsupported_schema_version(self, tmp_path: Path):
        converter = make_strict_converter()
        target = tmp_path / "results.json"
        _results().save(target, converter=converter)
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["schema_version"] = payload["schema_version"] + 1
        target.write_text(json.dumps(payload), encoding="utf-8")

        loaded, error = StudyResults.load(target, converter=converter)

        assert loaded is None
        assert error is not None
        assert "schema_version" in error

    @pytest.mark.parametrize(
        "uri",
        [
            "postgresql://operator:secret@db.example:5433/hpo",
            "postgresql://db.example:5433/hpo?password=secret",
            "postgresql://db.example:5433/hpo#secret",
        ],
    )
    def test_never_writes_a_credential_into_the_document(
        self, tmp_path: Path, uri: str
    ):
        target = tmp_path / "results.json"
        storage = StudyStorage(uri=uri)

        _results(storage=storage, trials=(_trial(0),)).save(
            target, converter=make_strict_converter()
        )

        # 生の文字列として現れないことを見る。フィールド名だけの検査では
        # 生 URI のフィールドを足した実装を検出できない
        assert "secret" not in target.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "trials",
        [
            (_trial(0), _trial(0)),
            (_trial(-1),),
        ],
    )
    def test_reports_invalid_trial_numbers(self, trials: tuple[TrialRecord, ...]):
        results, error = StudyResults.build(
            identity=_identity(),
            storage=_storage(),
            direction="minimize",
            trials=trials,
        )

        assert results is None
        assert error is not None


class TestVerifyLineage:
    """COMPLETE な trial が実験 run と紐づいていることの事後検証."""

    def test_accepts_results_where_every_completed_trial_has_a_run_id(self):
        trials = (
            _trial(0, experiment_run_id="run-a"),
            _trial(1, experiment_run_id="run-b"),
        )

        assert _results(trials=trials).verify_lineage() is None

    def test_reports_the_numbers_of_completed_trials_without_a_run_id(self):
        trials = (
            _trial(0, experiment_run_id="run-a"),
            _trial(1, experiment_run_id=None),
            _trial(7, experiment_run_id=None),
        )

        error = _results(trials=trials).verify_lineage()

        assert error is not None
        assert "1" in error
        assert "7" in error

    def test_allows_a_missing_run_id_on_failed_or_pruned_trials(self):
        trials = (
            _trial(0, experiment_run_id="run-a"),
            _trial(1, state="FAIL", value=None, experiment_run_id=None),
            _trial(2, state="PRUNED", value=None, experiment_run_id=None),
        )

        assert _results(trials=trials).verify_lineage() is None
