"""Git / 依存版の provenance 収集と、永続化前の秘匿情報除去の公開契約."""

from __future__ import annotations

import importlib.metadata
import subprocess
from pathlib import Path

import pytest

from ml.experiment.provenance import (
    TRACKED_PACKAGE_NAMES,
    DependencyVersions,
    GitProvenance,
    sanitize_persisted_text,
    sanitize_persisted_uri,
)

# diff に混ぜても tag へ漏れてはならない目印
SECRET_MARKER = "do-not-persist-marker-9f2c"


def _run_git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """1 commit だけ持つ実 git repository を作る."""

    root = tmp_path / "repository"
    root.mkdir()
    _run_git(root, "init", "--initial-branch", "main")
    _run_git(root, "config", "user.email", "tester@example.invalid")
    _run_git(root, "config", "user.name", "tester")
    (root / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _run_git(root, "add", "tracked.txt")
    _run_git(root, "commit", "-m", "initial")
    return root


def _capture(repository: Path) -> GitProvenance:
    provenance, reason = GitProvenance.capture(repository)

    assert reason is None
    assert provenance is not None
    return provenance


class TestGitProvenanceCapture:
    """実 git repository から commit / branch / dirty を取り出す."""

    def test_clean_repository_reports_commit_and_branch(self, repository: Path):
        provenance = _capture(repository)

        assert len(provenance.commit) == 40
        assert provenance.branch == "main"
        assert provenance.dirty is False
        assert provenance.untracked_files == ()

    def test_modified_file_makes_the_repository_dirty(self, repository: Path):
        clean = _capture(repository)

        (repository / "tracked.txt").write_text(
            f"changed {SECRET_MARKER}\n", encoding="utf-8"
        )
        dirty = _capture(repository)

        assert dirty.dirty is True
        assert SECRET_MARKER in dirty.diff
        assert dirty.diff_fingerprint != clean.diff_fingerprint

    def test_untracked_file_is_listed(self, repository: Path):
        (repository / "extra.txt").write_text(
            f"untracked {SECRET_MARKER}\n", encoding="utf-8"
        )

        provenance = _capture(repository)

        assert "extra.txt" in provenance.untracked_files
        assert SECRET_MARKER in provenance.untracked_content

    def test_directory_outside_a_repository_is_rejected(self, tmp_path: Path):
        outside = tmp_path / "plain"
        outside.mkdir()

        provenance, reason = GitProvenance.capture(outside)

        assert provenance is None
        assert reason is not None
        assert "git" in reason


class TestGitProvenanceTags:
    """タグには要約だけを出し、生 diff を永続化しない."""

    def test_tags_expose_the_expected_keys(self, repository: Path):
        provenance = _capture(repository)

        tags = provenance.as_tags()

        assert set(tags) == {
            "git.commit",
            "git.branch",
            "git.dirty",
            "git.diff_fingerprint",
            "git.untracked_file_count",
        }
        assert tags["git.commit"] == provenance.commit
        assert tags["git.branch"] == "main"

    def test_tags_contain_neither_raw_diff_nor_untracked_content(
        self, repository: Path
    ):
        (repository / "tracked.txt").write_text(
            f"changed {SECRET_MARKER}\n", encoding="utf-8"
        )
        (repository / "extra.txt").write_text(
            f"untracked {SECRET_MARKER}\n", encoding="utf-8"
        )
        provenance = _capture(repository)

        tags = provenance.as_tags()

        assert SECRET_MARKER not in "".join(tags.values())
        assert tags["git.dirty"] == "true"
        assert tags["git.untracked_file_count"] == "1"
        assert tags["git.diff_fingerprint"].startswith("sha256:")


class TestDependencyVersions:
    """収集した依存の版が実際の install 状態と一致する."""

    def test_tracked_packages_match_importlib_metadata(self):
        versions = DependencyVersions.collect().versions

        for name in TRACKED_PACKAGE_NAMES:
            try:
                expected = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                expected = "not-installed"
            assert versions[name] == expected

    def test_python_version_is_collected(self):
        versions = DependencyVersions.collect().versions

        assert versions["python"] != ""

    def test_as_params_prefixes_every_entry(self):
        collected = DependencyVersions.collect()

        params = collected.as_params()

        assert set(params) == {f"dependency.{name}" for name in collected.versions}
        assert params["dependency.torch"] == collected.versions["torch"]


class TestSanitizePersistedUri:
    """永続化する URI から credential と query / fragment を落とす."""

    def test_credentials_query_and_fragment_are_removed(self):
        sanitized = sanitize_persisted_uri(
            "postgresql://user:secret@host:5432/db?sslmode=require"
        )

        assert sanitized == "postgresql://host:5432/db"

    def test_fragment_is_removed(self):
        sanitized = sanitize_persisted_uri("https://tracking.invalid/path#section")

        assert sanitized == "https://tracking.invalid/path"

    def test_uri_without_credentials_is_preserved(self):
        sanitized = sanitize_persisted_uri("http://127.0.0.1:5000")

        assert sanitized == "http://127.0.0.1:5000"

    def test_plain_string_without_scheme_is_unchanged(self):
        assert sanitize_persisted_uri("not-a-uri") == "not-a-uri"


class TestSanitizePersistedText:
    """自由文に埋まった URI もすべて sanitize する."""

    def test_every_uri_in_the_text_is_sanitized(self):
        text = (
            "tracking は postgresql://user:secret@host:5432/db?sslmode=require で、"
            "artifact は https://token:pass@artifacts.invalid/run?sig=abc にある"
        )

        sanitized = sanitize_persisted_text(text)

        assert "secret" not in sanitized
        assert "pass@" not in sanitized
        assert "sslmode" not in sanitized
        assert "sig=abc" not in sanitized
        assert "postgresql://host:5432/db" in sanitized
        assert "https://artifacts.invalid/run" in sanitized

    def test_text_without_uri_is_unchanged(self):
        text = "学習が 2 epoch で終了しました"

        assert sanitize_persisted_text(text) == text
