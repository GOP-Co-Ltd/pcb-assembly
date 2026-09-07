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

# untracked 本文を読み込む 1 ファイルあたりの上限。
#
# ``src/ml/experiment/provenance.py`` の ``_UNTRACKED_FILE_BYTE_LIMIT`` と揃える。
UNTRACKED_FILE_BYTE_LIMIT = 1024 * 1024

# 上限超過ファイルの本文を落としたことを示す見出し.
OMITTED_BODY_MARKER = "[本文は大きすぎるため省略]"


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


class TestUntrackedContentSizeLimit:
    """大きな untracked ファイルは本文を落とし、digest だけ残す."""

    def _write_oversized(self, repository: Path, *, filler: bytes) -> int:
        """上限を 1 byte だけ超える untracked ファイルを置き、その大きさを返す."""

        size = UNTRACKED_FILE_BYTE_LIMIT + 1
        head = SECRET_MARKER.encode("utf-8")
        body = head + filler * (size - len(head))
        path = repository / "oversized.bin"
        path.write_bytes(body[:size])
        return size

    def test_oversized_file_body_is_omitted(self, repository: Path):
        size = self._write_oversized(repository, filler=b"a")

        provenance = _capture(repository)

        assert "oversized.bin" in provenance.untracked_files
        assert OMITTED_BODY_MARKER in provenance.untracked_content
        assert SECRET_MARKER not in provenance.untracked_content
        assert f"size {size}; sha256:" in provenance.untracked_content

    def test_small_file_body_is_kept(self, repository: Path):
        (repository / "small.txt").write_text(
            f"small {SECRET_MARKER}\n", encoding="utf-8"
        )

        provenance = _capture(repository)

        assert OMITTED_BODY_MARKER not in provenance.untracked_content
        assert SECRET_MARKER in provenance.untracked_content

    def test_fingerprint_follows_the_content_of_an_omitted_file(self, repository: Path):
        """本文を落としても、内容が変われば fingerprint が変わる.

        省略側も digest を見出しへ載せるので、この性質は保たれる。
        """

        self._write_oversized(repository, filler=b"a")
        first = _capture(repository).diff_fingerprint

        self._write_oversized(repository, filler=b"b")
        second = _capture(repository).diff_fingerprint

        assert first.startswith("sha256:")
        assert first != second


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

    @pytest.mark.parametrize(
        ("uri", "expected"),
        [
            # authority を持たない URI は組み立て直さず、そのまま通す
            ("file:./mlruns", "file:./mlruns"),
            ("mailto:user@example.com", "mailto:user@example.com"),
            ("urn:uuid:1234", "urn:uuid:1234"),
            ("sqlite:///mlruns.db", "sqlite:///mlruns.db"),
            ("file:///var/mlruns", "file:///var/mlruns"),
            ("http://localhost:5000", "http://localhost:5000"),
            # host が無くても userinfo は落とす
            ("http://user:secret@", "http://"),
            ("http://user:secret@/path", "http:///path"),
            ("databricks://a:b@", "databricks://"),
            # host がある形は credential と query / fragment ごと落とす
            (
                "postgresql://user:secret@host:5432/db?sslmode=require",
                "postgresql://host:5432/db",
            ),
            ("https://user:tok@[::1]:8080/x?q=1#f", "https://[::1]:8080/x"),
            ("http://host/p?token=abc#frag", "http://host/p"),
        ],
    )
    def test_uri_is_sanitized_as_pinned(self, uri: str, expected: str):
        assert sanitize_persisted_uri(uri) == expected

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

    def test_credentials_without_a_host_are_removed_in_text(self):
        """Host を持たない URI の userinfo も自由文から消える."""

        text = "tracking は http://user:secret@/path です"

        sanitized = sanitize_persisted_text(text)

        assert "secret" not in sanitized
        assert "http:///path" in sanitized

    def test_text_without_uri_is_unchanged(self):
        text = "学習が 2 epoch で終了しました"

        assert sanitize_persisted_text(text) == text
