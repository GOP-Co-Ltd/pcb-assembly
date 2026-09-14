"""`web.selfupdate.runner.UpdateRunner` の契約テスト.

計画書「1. 共通モジュール」の実行順序表

```
0 単一実行ロック（flock: state_dir/update.lock）
1 restart_permitted()
2 fetch → fast_forward_blocker → 中断 or 継続
3 merge --ff-only          （失敗 → 中断）
4 uv sync --locked --inexact（失敗 → 再起動しない）
5 import smoke              （失敗 → 再起動しない）
6 report を JSON へ永続化
7 schedule_restart(active_units(), delay=1.0)
```

と、確定要件「`uv sync` 失敗 → **再起動しない**」「ff 不可 → **何もせず中断**」、
安全弁「`expected_head` 必須の 2 段階（楽観ロック）」「単一実行ロック」「`enabled` 設定」
が契約。

モックは 1 つも使わない。git は実物、`uv` / `sudo` / `systemctl` は `UpdateSettings` の
絶対パス seam に差し込んだ **実スタブ実行ファイル**で、呼び出しの有無と順序は
スタブが追記する共有ログで観測する。ハングし得る待ちは `tests.helpers.before_deadline`
で包む。
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers import before_deadline, wait_until
from tests.web.selfupdate.conftest import (
    call_log,
    git,
    head,
    push_commit,
    restart_calls,
    write_stub,
    write_sudo_stub,
    write_systemctl_stub,
)
from web.selfupdate.report import UpdateState, UpdateStep
from web.selfupdate.runner import UpdateRunner
from web.selfupdate.settings import UpdateSettings

API_UNIT = "pcbasm-api.service"
UI_UNIT = "pcbasm-ui.service"

# 子プロセスから同じ設定の runner を組み立てる前置き（report / flock がプロセスを
# 跨ぐことを確かめるために使う）。
CHILD_PREAMBLE = """
import sys
from pathlib import Path

from web.selfupdate.runner import UpdateRunner
from web.selfupdate.settings import UpdateSettings

settings = UpdateSettings(
    repo_root=Path(sys.argv[1]),
    state_dir=Path(sys.argv[2]),
    uv_bin=sys.argv[3],
    systemctl_bin=sys.argv[4],
    sudo_bin=sys.argv[5],
    restart_delay=0.05,
)
runner = UpdateRunner(settings)
"""


def child_arguments(settings: UpdateSettings) -> list[str]:
    """`CHILD_PREAMBLE` に渡す argv（親と同じ設定を再現する）."""
    return [
        str(settings.repo_root),
        str(settings.state_dir),
        settings.uv_bin,
        settings.systemctl_bin,
        settings.sudo_bin,
    ]


def build_settings(
    clone: Path,
    tmp_path: Path,
    *,
    uv_exit_code: int = 0,
    uv_fail_match: str | None = None,
    uv_sleep: float = 0.0,
    pid_file: Path | None = None,
    permitted: bool = True,
    enabled: bool = True,
    sync_timeout: float = 60.0,
) -> UpdateSettings:
    """スタブ実行ファイルを置いた `UpdateSettings` を組む.

    Args:
        clone: 被験体の作業リポジトリ（`clone` fixture）
        tmp_path: テストの一時ディレクトリ（ログとスタブの置き場所）
        uv_exit_code: `uv` スタブの終了コード
        uv_fail_match: この引数が渡されたときだけ `uv` を失敗させる（`sync` / `run`）
        uv_sleep: `uv` スタブが終了前に待つ秒数（タイムアウト検証用）
        pid_file: `uv` スタブが自身と子の PID を書き出す先
        permitted: `sudo -n -l` の下見を成功させるか（sudoers 未設置の再現）
        enabled: 自己更新機能の有効・無効
        sync_timeout: `uv sync` のタイムアウト

    Returns:
        組み立てた設定
    """
    bin_dir = tmp_path / "bin"
    log = tmp_path / "calls.log"
    systemctl = write_systemctl_stub(bin_dir / "systemctl", log=log)
    return UpdateSettings(
        repo_root=clone,
        state_dir=tmp_path / "state",
        uv_bin=write_stub(
            bin_dir / "uv",
            exit_code=uv_exit_code,
            log=log,
            sleep=uv_sleep,
            fail_match=uv_fail_match,
            pid_file=pid_file,
        ),
        systemctl_bin=systemctl,
        sudo_bin=write_sudo_stub(
            bin_dir / "sudo", systemctl_bin=systemctl, log=log, permitted=permitted
        ),
        enabled=enabled,
        restart_delay=0.05,
        sync_timeout=sync_timeout,
    )


def log_path(settings: UpdateSettings) -> Path:
    """スタブが追記する共有ログのパス（`state_dir` の隣）."""
    return settings.state_dir.parent / "calls.log"


def run_to_completion(runner: UpdateRunner, *, expected_head: str | None = None) -> str:
    """`start()` の成功を強制し、終わるまで待つ（ハングはテスト失敗にする）."""
    run_id, reason = runner.start(expected_head=expected_head)

    assert reason is None, reason
    assert run_id is not None
    assert before_deadline(lambda: runner.wait(30.0), what="更新の完了") is True
    return run_id


def process_alive(pid: int) -> bool:
    """PID のプロセスがまだ生きているか（シグナル 0 の到達性で判定）."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class TestSuccessfulRun:
    """更新が通ったときの手順と結果."""

    def test_steps_run_in_the_planned_order(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        push_commit(publisher, body="second\n")
        settings = build_settings(clone, tmp_path)
        runner = UpdateRunner(settings)

        run_to_completion(runner)

        report = runner.status()
        assert [record.step for record in report.steps] == [
            UpdateStep.PREFLIGHT,
            UpdateStep.FETCH,
            UpdateStep.MERGE,
            UpdateStep.SYNC,
            UpdateStep.SMOKE,
        ]
        assert all(record.ok for record in report.steps)

    def test_external_commands_are_invoked_in_order(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        """`uv sync` → import smoke → 再起動。ログの追記順で観測する."""
        push_commit(publisher, body="second\n")
        settings = build_settings(clone, tmp_path)

        run_to_completion(UpdateRunner(settings))

        lines = call_log(log_path(settings))
        sync = next(index for index, line in enumerate(lines) if " sync " in line)
        smoke = next(index for index, line in enumerate(lines) if "--no-sync" in line)
        restart = next(index for index, line in enumerate(lines) if "restart" in line)
        assert sync < smoke < restart, lines

    def test_working_tree_is_fast_forwarded(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        pushed = push_commit(publisher, body="second\n")
        settings = build_settings(clone, tmp_path)
        runner = UpdateRunner(settings)
        before = head(clone)

        run_to_completion(runner)

        report = runner.status()
        assert head(clone) == pushed
        assert report.from_head == before
        assert report.to_head == pushed

    def test_restart_is_scheduled_for_every_active_unit_in_canonical_order(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        """同居機は api → ui の 1 argv で再起動する（sudoers が許す唯一の並び）."""
        push_commit(publisher, body="second\n")
        settings = build_settings(clone, tmp_path)
        runner = UpdateRunner(settings)

        run_to_completion(runner)

        report = runner.status()
        assert report.state is UpdateState.RESTARTING
        assert report.restart_units == (API_UNIT, UI_UNIT)
        wait_until(lambda: restart_calls(log_path(settings)) != [])
        assert any(
            f"restart --no-block {API_UNIT} {UI_UNIT}" in line
            for line in restart_calls(log_path(settings))
        ), call_log(log_path(settings))


class TestRestartIsWithheldOnFailure:
    """「失敗したら再起動しない」（確定要件）の直接検証."""

    def test_failing_uv_sync_skips_both_the_smoke_and_the_restart(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        push_commit(publisher, body="second\n")
        settings = build_settings(clone, tmp_path, uv_fail_match="sync")
        runner = UpdateRunner(settings)

        run_id, reason = runner.start()
        assert reason is None, reason
        assert run_id is not None
        before_deadline(lambda: runner.wait(30.0), what="更新の完了")

        report = runner.status()
        assert report.state is UpdateState.FAILED
        assert report.step is UpdateStep.SYNC
        assert report.error
        lines = call_log(log_path(settings))
        assert restart_calls(log_path(settings)) == [], lines
        assert not [line for line in lines if "--no-sync" in line], lines

    def test_failing_import_smoke_skips_the_restart(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        """新リビジョンが起動しないまま再起動すると WebUI ごと到達不能になる."""
        push_commit(publisher, body="second\n")
        settings = build_settings(clone, tmp_path, uv_fail_match="run")
        runner = UpdateRunner(settings)

        run_to_completion(runner)

        report = runner.status()
        assert report.state is UpdateState.FAILED
        assert report.step is UpdateStep.SMOKE
        assert restart_calls(log_path(settings)) == []


class TestStartIsRefused:
    """`start()` が理由を返して何もしないケース（`str | None` 規約）."""

    def test_uncommitted_change_blocks_before_anything_runs(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        """Fast-forward できないなら git も uv も動かさない（確定要件「何もせず中断」）."""
        push_commit(publisher, body="second\n")
        (clone / "tracked.txt").write_text("edited\n", encoding="utf-8")
        settings = build_settings(clone, tmp_path)
        before = head(clone)

        run_id, reason = UpdateRunner(settings).start()

        assert run_id is None
        assert reason
        assert head(clone) == before
        assert (clone / "tracked.txt").read_text() == "edited\n"
        assert not [line for line in call_log(log_path(settings)) if " sync " in line]
        assert restart_calls(log_path(settings)) == []

    def test_stale_expected_head_is_rejected(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        """開きっぱなしの古いタブや `curl` 一発を弾く楽観ロック（安全弁 1）."""
        push_commit(publisher, body="second\n")
        settings = build_settings(clone, tmp_path)
        before = head(clone)

        run_id, reason = UpdateRunner(settings).start(expected_head="0000000")

        assert run_id is None
        assert reason
        assert head(clone) == before
        assert restart_calls(log_path(settings)) == []

    def test_matching_expected_head_is_accepted(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        push_commit(publisher, body="second\n")
        settings = build_settings(clone, tmp_path)
        runner = UpdateRunner(settings)

        run_to_completion(runner, expected_head=head(clone))

        assert runner.status().state is UpdateState.RESTARTING

    def test_disabled_installation_refuses_to_update(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        """設定で無効化した機体（安全弁 3）."""
        push_commit(publisher, body="second\n")
        settings = build_settings(clone, tmp_path, enabled=False)

        run_id, reason = UpdateRunner(settings).start()

        assert run_id is None
        assert reason
        assert call_log(log_path(settings)) == []

    def test_missing_sudoers_aborts_before_touching_the_repository(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        """Sudoers 未設置なら pull する前に中断する（実行順序 1）."""
        push_commit(publisher, body="second\n")
        settings = build_settings(clone, tmp_path, permitted=False)
        before = head(clone)
        runner = UpdateRunner(settings)

        run_id, reason = runner.start()
        if run_id is not None:
            before_deadline(lambda: runner.wait(30.0), what="更新の完了")
            assert runner.status().step is UpdateStep.PREFLIGHT

        assert head(clone) == before
        assert not [line for line in call_log(log_path(settings)) if " sync " in line]
        assert restart_calls(log_path(settings)) == []


class TestSingleFlight:
    """単一実行ロック（安全弁 2）。同居機の api / ui が同時に走るのも止める."""

    def test_second_start_in_the_same_process_is_refused(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        push_commit(publisher, body="second\n")
        settings = build_settings(clone, tmp_path, uv_sleep=3.0)
        runner = UpdateRunner(settings)

        first_id, first_reason = runner.start()
        wait_until(lambda: call_log(log_path(settings)) != [])
        second_id, second_reason = runner.start()

        assert first_reason is None, first_reason
        assert first_id is not None
        assert second_id is None
        assert second_reason
        before_deadline(lambda: runner.wait(30.0), what="更新の完了")

    def test_another_process_holding_the_lock_is_refused(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        """Flock なのでプロセスを跨いで効く（`state_dir/update.lock`）."""
        push_commit(publisher, body="second\n")
        settings = build_settings(clone, tmp_path, uv_sleep=5.0)
        source = CHILD_PREAMBLE + (
            "run_id, reason = runner.start()\n"
            'print(f"{run_id}|{reason}", flush=True)\n'
            "runner.wait(30.0)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", source, *child_arguments(settings)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None
            first = before_deadline(child.stdout.readline, what="子プロセスの start")
            assert first.strip().endswith("|None"), first

            run_id, reason = UpdateRunner(settings).start()

            assert run_id is None
            assert reason
        finally:
            child.kill()
            child.wait(timeout=10.0)


class TestTimeout:
    """ハングした外部コマンドを打ち切り、プロセスグループごと後始末する."""

    def test_slow_sync_is_aborted_and_its_process_group_is_killed(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        push_commit(publisher, body="second\n")
        pid_file = tmp_path / "uv.pids"
        settings = build_settings(
            clone, tmp_path, uv_sleep=30.0, pid_file=pid_file, sync_timeout=0.5
        )
        runner = UpdateRunner(settings)

        run_to_completion(runner)

        report = runner.status()
        assert report.state is UpdateState.FAILED
        assert report.step is UpdateStep.SYNC
        assert restart_calls(log_path(settings)) == []
        pids = [int(line) for line in pid_file.read_text().split()]
        assert pids, "スタブが PID を書いていない"
        wait_until(lambda: not process_alive(pids[-1]), timeout=15.0)


class TestStatusAcrossProcesses:
    """Report は再起動でプロセスが死んでも残る唯一の状態（実行順序 6）."""

    def test_another_process_reads_the_persisted_report(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        push_commit(publisher, body="second\n")
        settings = build_settings(clone, tmp_path)
        runner = UpdateRunner(settings)
        run_id = run_to_completion(runner)

        source = CHILD_PREAMBLE + (
            "report = runner.status()\n"
            'print(f"{report.run_id}|{report.state}|{report.to_head}", flush=True)\n'
        )
        child = subprocess.run(
            [sys.executable, "-c", source, *child_arguments(settings)],
            capture_output=True,
            text=True,
            timeout=60.0,
        )

        assert child.returncode == 0, child.stderr
        assert child.stdout.strip() == (
            f"{run_id}|{UpdateState.RESTARTING.value}|{head(clone)}"
        )


class TestNothingToUpdate:
    """既に最新の機体でも `start()` は成立する（何も pull せずに終わる）."""

    def test_up_to_date_repository_does_not_fail(self, clone: Path, tmp_path: Path):
        settings = build_settings(clone, tmp_path)
        runner = UpdateRunner(settings)
        before = head(clone)

        run_to_completion(runner)

        report = runner.status()
        assert report.state is not UpdateState.FAILED
        assert head(clone) == before
        assert git(clone, "status", "--porcelain") == ""


class TestRestartServices:
    """更新を伴わないサービス再起動（ファームウェア再起動ボタンから使う）.

    再起動対象と argv は更新時とまったく同じ（`active_units()` と `restart_command`）。
    sudoers に許可を追加せずに済ませるため、ここが分岐したら実機で必ず落ちる。
    """

    def test_active_units_are_restarted_in_canonical_order(
        self, clone: Path, tmp_path: Path
    ):
        settings = build_settings(clone, tmp_path)

        assert UpdateRunner(settings).restart_services() is None

        wait_until(lambda: restart_calls(log_path(settings)) != [])
        assert any(
            f"restart --no-block {API_UNIT} {UI_UNIT}" in line
            for line in restart_calls(log_path(settings))
        ), call_log(log_path(settings))

    def test_missing_sudoers_is_refused_without_restarting(
        self, clone: Path, tmp_path: Path
    ):
        settings = build_settings(clone, tmp_path, permitted=False)

        reason = UpdateRunner(settings).restart_services()

        assert reason is not None
        assert restart_calls(log_path(settings)) == []

    def test_running_update_is_not_interrupted(
        self, clone: Path, tmp_path: Path, publisher: Path
    ):
        """`uv sync` の最中に unit を落とすと中途半端な依存で起動不能になる."""
        push_commit(publisher, body="second\n")
        settings = build_settings(clone, tmp_path, uv_sleep=1.0)
        runner = UpdateRunner(settings)
        run_id, reason = runner.start()
        assert reason is None and run_id is not None

        wait_until(lambda: runner.running)
        refusal = runner.restart_services()

        assert refusal is not None
        assert before_deadline(lambda: runner.wait(30.0), what="更新の完了") is True

    def test_another_process_updating_blocks_the_restart(
        self, clone: Path, tmp_path: Path
    ):
        """同居機の相方が更新中なら断る（守れるのは共有 flock だけ）.

        `running` はこのプロセスの更新スレッドしか見ない。backend と frontend は
        同じ `update.lock` を掴むので、相方の `uv sync` の最中に unit を落とすのを
        止められるのはロックだけ。
        """
        settings = build_settings(clone, tmp_path)
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        other = settings.lock_path.open("w", encoding="utf-8")
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            reason = UpdateRunner(settings).restart_services()
        finally:
            fcntl.flock(other.fileno(), fcntl.LOCK_UN)
            other.close()

        assert reason is not None
        assert restart_calls(log_path(settings)) == []

    def test_lock_is_released_after_the_restart_is_issued(
        self, clone: Path, tmp_path: Path
    ):
        """握ったままにしない（以後の更新が全部「実行中」で止まる）."""
        settings = build_settings(clone, tmp_path)
        runner = UpdateRunner(settings)

        assert runner.restart_services() is None

        wait_until(lambda: restart_calls(log_path(settings)) != [])
        wait_until(lambda: runner.restart_services() is None)
