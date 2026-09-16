"""`web.selfupdate.repo` の契約テスト（実 git に対して走らせる）.

計画書「1. 共通モジュール `src/web/selfupdate/`」の `repo.py` と、その直後の

- 「`git pull` は分解する」（`fetch` → 判定 → `merge --ff-only`）
- 「dirty 判定は `git status --porcelain --untracked-files=no`。untracked は report に
  記録するだけで中断しない」
- 確定要件「ff 不可（dirty / ローカル commit / 分岐）は **何もせず中断**」

が契約。`fast_forward_blocker` は「例外でなく `str | None`」規約の適用点なので、
中断すべき状況を 1 つ残らず列挙して固定する。

モックは使わない。remote は `tmp_path` 上の bare リポジトリなのでネットワークにも出ない。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.web.selfupdate.conftest import git, head, local_commit, push_commit
from web.selfupdate.repo import (
    RepoState,
    capture_state,
    fast_forward_blocker,
    fetch,
    merge_fast_forward,
)
from web.selfupdate.settings import UpdateSettings


@pytest.fixture
def settings(clone: Path, tmp_path: Path) -> UpdateSettings:
    """被験体の clone を指す設定（外部バイナリは触らない項目のみ使う）."""
    return UpdateSettings(repo_root=clone, state_dir=tmp_path / "state")


def state_of(settings: UpdateSettings) -> RepoState:
    """`capture_state` の成功を強制して `RepoState` を取り出す."""
    state, reason = capture_state(settings)

    assert reason is None, reason
    assert state is not None
    return state


def fetched_state(settings: UpdateSettings) -> RepoState:
    """Fetch を通してから状態を取る（ahead / behind を最新にする）."""
    assert fetch(settings) is None
    return state_of(settings)


class TestCaptureState:
    """`capture_state` が返す観測値."""

    def test_clean_clone_tracks_origin_main(self, settings: UpdateSettings):
        state = state_of(settings)

        assert state.branch == "main"
        assert state.upstream == "origin/main"
        assert state.head == head(settings.repo_root)
        assert state.head_subject == "initial"
        assert state.dirty_paths == ()
        assert state.untracked_paths == ()

    def test_behind_counts_the_commits_waiting_on_origin(
        self, settings: UpdateSettings, publisher: Path
    ):
        push_commit(publisher, body="second\n")

        state = fetched_state(settings)

        assert state.behind == 1
        assert state.ahead == 0
        assert state.up_to_date is False
        assert state.upstream_head is not None
        assert state.upstream_head != state.head

    def test_modified_tracked_file_is_reported_as_dirty(self, settings: UpdateSettings):
        (settings.repo_root / "tracked.txt").write_text("edited\n", encoding="utf-8")

        state = state_of(settings)

        assert state.dirty_paths == ("tracked.txt",)

    def test_observation_does_not_refresh_the_git_index(self, settings: UpdateSettings):
        """更新中のポーリングが index.lock を取って merge と競合しない。"""
        tracked = settings.repo_root / "tracked.txt"
        index = settings.repo_root / ".git" / "index"
        before = index.read_bytes()
        stamp = tracked.stat()
        # 内容はそのまま、stat キャッシュだけが古くなる状態を実ファイルで作る。
        os.utime(tracked, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000_000))

        state = state_of(settings)

        assert state.dirty_paths == ()
        assert index.read_bytes() == before

    def test_untracked_file_is_recorded_separately_from_dirty(
        self, settings: UpdateSettings
    ):
        """Untracked は「記録するだけ」。dirty に混ぜると更新が止まってしまう."""
        (settings.repo_root / "stray.txt").write_text("stray\n", encoding="utf-8")

        state = state_of(settings)

        assert state.dirty_paths == ()
        assert state.untracked_paths == ("stray.txt",)

    def test_detached_head_has_no_branch(self, settings: UpdateSettings):
        git(settings.repo_root, "checkout", "--detach", "HEAD")

        state = state_of(settings)

        assert state.branch is None

    def test_missing_upstream_is_reported_as_none(self, settings: UpdateSettings):
        git(settings.repo_root, "branch", "--unset-upstream")

        state = state_of(settings)

        assert state.upstream is None

    def test_non_repository_returns_a_reason(self, tmp_path: Path):
        """`.git` の無いディレクトリでも例外を投げず理由を返す（GET status は常に 200）."""
        plain = tmp_path / "plain"
        plain.mkdir()

        state, reason = capture_state(
            UpdateSettings(repo_root=plain, state_dir=tmp_path / "state")
        )

        assert state is None
        assert reason


class TestFastForwardBlocker:
    """中断すべき状況の列挙（`None` なら fast-forward してよい）."""

    def test_clean_and_behind_is_allowed(
        self, settings: UpdateSettings, publisher: Path
    ):
        push_commit(publisher, body="second\n")

        assert fast_forward_blocker(fetched_state(settings)) is None

    def test_already_up_to_date_is_allowed(self, settings: UpdateSettings):
        state = fetched_state(settings)

        assert fast_forward_blocker(state) is None
        assert state.behind == 0
        assert state.up_to_date is True

    def test_untracked_files_alone_do_not_block(
        self, settings: UpdateSettings, publisher: Path
    ):
        """Ignore 漏れのファイル 1 つで全機体の更新が止まるのは脆い（計画書「1.」）.

        実際に pull と衝突する場合は `merge --ff-only` が非 0 で落ちて fail-closed になる。
        """
        push_commit(publisher, body="second\n")
        (settings.repo_root / "stray.txt").write_text("stray\n", encoding="utf-8")

        state = fetched_state(settings)

        assert fast_forward_blocker(state) is None
        assert state.untracked_paths == ("stray.txt",)

    def test_uncommitted_change_blocks(self, settings: UpdateSettings, publisher: Path):
        push_commit(publisher, body="second\n")
        (settings.repo_root / "tracked.txt").write_text("edited\n", encoding="utf-8")

        blocker = fast_forward_blocker(fetched_state(settings))

        assert blocker is not None
        assert "未コミット" in blocker

    def test_local_commit_blocks(self, settings: UpdateSettings):
        local_commit(settings.repo_root)

        state = fetched_state(settings)

        assert state.ahead == 1
        assert fast_forward_blocker(state) is not None

    def test_diverged_history_blocks(self, settings: UpdateSettings, publisher: Path):
        push_commit(publisher, body="second\n")
        local_commit(settings.repo_root)

        state = fetched_state(settings)

        assert (state.ahead, state.behind) == (1, 1)
        assert fast_forward_blocker(state) is not None

    def test_detached_head_blocks(self, settings: UpdateSettings):
        """Detached では追従先が無く、merge しても次の起動で迷子になる."""
        git(settings.repo_root, "checkout", "--detach", "HEAD")

        assert fast_forward_blocker(state_of(settings)) is not None

    def test_missing_upstream_blocks(self, settings: UpdateSettings):
        git(settings.repo_root, "branch", "--unset-upstream")

        assert fast_forward_blocker(state_of(settings)) is not None


class TestFetch:
    """`fetch` はネットワーク I/O を伴う唯一の経路（失敗しても例外にしない）."""

    def test_fetch_makes_the_new_origin_commit_visible(
        self, settings: UpdateSettings, publisher: Path
    ):
        pushed = push_commit(publisher, body="second\n")

        assert fetch(settings) is None
        assert state_of(settings).upstream_head == pushed

    def test_unreachable_remote_returns_a_reason(self, settings: UpdateSettings):
        """認証やネットワーク障害でハングせず理由を返す（ネットワークは使わない）."""
        git(settings.repo_root, "remote", "set-url", "origin", "/nonexistent.git")

        assert fetch(settings) is not None


class TestMergeFastForward:
    """`merge --ff-only`。成功しない限り作業ツリーを 1 バイトも動かさない."""

    def test_advances_head_to_the_upstream_commit(
        self, settings: UpdateSettings, publisher: Path
    ):
        pushed = push_commit(publisher, body="second\n")
        assert fetch(settings) is None

        assert merge_fast_forward(settings) is None
        assert head(settings.repo_root) == pushed
        assert (settings.repo_root / "tracked.txt").read_text() == "second\n"

    def test_diverged_history_leaves_head_and_worktree_untouched(
        self, settings: UpdateSettings, publisher: Path
    ):
        """Fast-forward できないなら「何もせず中断」（確定要件）."""
        push_commit(publisher, body="second\n")
        local_commit(settings.repo_root)
        assert fetch(settings) is None
        before = head(settings.repo_root)

        reason = merge_fast_forward(settings)

        assert reason is not None
        assert head(settings.repo_root) == before
        assert (settings.repo_root / "tracked.txt").read_text() == "initial\n"

    def test_uncommitted_change_to_an_incoming_file_leaves_the_worktree_untouched(
        self, settings: UpdateSettings, publisher: Path
    ):
        push_commit(publisher, body="second\n")
        (settings.repo_root / "tracked.txt").write_text("edited\n", encoding="utf-8")
        assert fetch(settings) is None
        before = head(settings.repo_root)

        reason = merge_fast_forward(settings)

        assert reason is not None
        assert head(settings.repo_root) == before
        assert (settings.repo_root / "tracked.txt").read_text() == "edited\n"

    def test_untracked_file_colliding_with_an_incoming_file_aborts(
        self, settings: UpdateSettings, publisher: Path
    ):
        """Untracked は中断理由にしないが、実際に衝突すれば git が止める（fail-closed）."""
        push_commit(publisher, name="added.txt", body="from origin\n")
        (settings.repo_root / "added.txt").write_text("local stray\n", encoding="utf-8")
        assert fetch(settings) is None
        before = head(settings.repo_root)

        reason = merge_fast_forward(settings)

        assert reason is not None
        assert head(settings.repo_root) == before
        assert (settings.repo_root / "added.txt").read_text() == "local stray\n"
