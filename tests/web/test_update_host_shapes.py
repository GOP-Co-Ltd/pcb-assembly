"""ホスト構成ごとの自己更新の振る舞い（実機でしか出ない差分の回帰テスト）.

`tests/web/selfupdate/` の契約テストは「同居機・sudoers 設置済み・sudo の listing が
折り返さない」という 1 つの形しか見ていない。ここはそれ以外の形を固定する:

- **frontend 専用機**（`pcbasm-ui.service` だけが active。picamera2 / pcbnew が無い）
- **機体**（`pcbasm-api.service` だけが active）
- **systemd で動かしていない開発機**（active な unit が 0）
- 実 `sudo -l` と同じく **80 桁で折り返した listing**
- preflight を通ったあとに **sudo が実際に再起動を拒否**する場合

スタブが実物より甘いと「実装の仮定をミラーしたテスト」になるので、
`tests/web/update_support.py` 側でこれらの実挙動を再現している。

**配置がミラーレイアウト外なのは意図的**: 対応する `tests/web/selfupdate/` は
spec-test-author が所有する契約テストのディレクトリで、そこへ後から足すと
「どちらが契約か」が混ざる。`tests/web/update_support.py`（router / frontend / E2E が
共有する sandbox）と同じ階層に置き、契約の**外側**の回帰であることを位置で示す。
"""

from __future__ import annotations

from pathlib import Path

import attrs
import pytest

from tests.helpers import before_deadline
from tests.web.update_support import make_update_sandbox
from web.selfupdate.report import UpdateState, UpdateStep, status_payload
from web.selfupdate.runner import UpdateRunner
from web.selfupdate.service import UNIT_NAMES
from web.selfupdate.steps import smoke_modules

API_UNIT = UNIT_NAMES["api"]
UI_UNIT = UNIT_NAMES["ui"]


def restart_notice_of(runner: UpdateRunner) -> str:
    """サーバが組んだ再起動範囲の文言（表示文字列はサーバ側で作る）."""
    return status_payload(runner.plan(), runner.status(), hostname="h").restart_notice


def run_to_completion(runner: UpdateRunner) -> None:
    """`start()` の成功を強制し、終わるまで待つ（ハングはテスト失敗にする）."""
    run_id, reason = runner.start()

    assert reason is None, reason
    assert run_id is not None
    assert before_deadline(lambda: runner.wait(30.0), what="更新の完了") is True


class TestSmokeTargetsFollowTheHost:
    """Import smoke は **そのホストが実際に動かすアプリだけ**を対象にする.

    `web.api.app` は picamera2 / pcbnew / cv2 を eager import する。frontend 専用機で
    これを読むと必ず `ModuleNotFoundError` になり、`uv sync` まで成功して
    「ソースだけ新しくして再起動しない」状態で詰む（何度押しても同じ）。
    """

    @pytest.mark.parametrize(
        ("units", "expected"),
        [
            ((UI_UNIT,), ("web.ui.app",)),
            ((API_UNIT,), ("web.api.app",)),
            ((API_UNIT, UI_UNIT), ("web.api.app", "web.ui.app")),
            ((), ()),
        ],
    )
    def test_modules_are_derived_from_the_active_units(
        self, units: tuple[str, ...], expected: tuple[str, ...]
    ):
        assert smoke_modules(units) == expected

    def test_frontend_only_host_never_imports_the_device_application(
        self, tmp_path: Path
    ):
        """専用機の smoke に `web.api.app` が混ざらないこと（M1 の回帰）."""
        sandbox = make_update_sandbox(tmp_path / "ui-only", active=(UI_UNIT,))
        sandbox.push()
        runner = UpdateRunner(sandbox.settings)

        run_to_completion(runner)

        report = runner.status()
        assert report.state is UpdateState.RESTARTING
        assert report.restart_units == (UI_UNIT,)
        smoke = [line for line in sandbox.calls() if "--no-sync" in line]
        assert len(smoke) == 1, sandbox.calls()
        assert "import web.ui.app" in smoke[0]
        assert "web.api.app" not in smoke[0]


class TestHostWithoutSystemdUnits:
    """Active な pcbasm unit が 0 のホスト（手元で起動している開発機）."""

    def test_update_succeeds_without_sudo_and_without_restarting(self, tmp_path: Path):
        """再起動しないのだから sudoers 未設置でも中断しない（S2 の回帰）."""
        sandbox = make_update_sandbox(tmp_path / "bare", active=(), permitted=False)
        target = sandbox.push()
        runner = UpdateRunner(sandbox.settings)

        run_to_completion(runner)

        report = runner.status()
        assert report.state is UpdateState.SUCCEEDED
        assert report.restart_units == ()
        assert sandbox.head() == target
        assert sandbox.restarts() == []
        assert not [line for line in sandbox.calls() if line.startswith("sudo")]

    def test_the_notice_says_nothing_will_be_restarted(self, tmp_path: Path):
        sandbox = make_update_sandbox(tmp_path / "bare", active=())
        runner = UpdateRunner(sandbox.settings)

        assert "再起動は行いません" in restart_notice_of(runner)

    def test_smoke_is_skipped_but_recorded(self, tmp_path: Path):
        """手順の並びは変えない（画面が 5 段のまま読める）."""
        sandbox = make_update_sandbox(tmp_path / "bare", active=())
        sandbox.push()
        runner = UpdateRunner(sandbox.settings)

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
        assert not [line for line in sandbox.calls() if "--no-sync" in line]


class TestWrappedSudoListing:
    """実 `sudo -l` は tty が無いと 80 桁で折り返す（許可行は 84〜102 文字）."""

    def test_preflight_passes_when_the_listing_is_wrapped(self, tmp_path: Path):
        """折り返しで preflight が落ちると、正しく設置した機体で 1 度も更新できない."""
        sandbox = make_update_sandbox(tmp_path / "wrapped", wrap_sudo_listing=True)
        target = sandbox.push()
        runner = UpdateRunner(sandbox.settings)

        run_to_completion(runner)

        report = runner.status()
        assert report.state is UpdateState.RESTARTING, report.error
        assert sandbox.head() == target

    def test_missing_permission_is_still_detected_when_wrapped(self, tmp_path: Path):
        """正規化しても「許可が無い」は落とさない（fail-closed を緩めない）."""
        sandbox = make_update_sandbox(
            tmp_path / "denied", wrap_sudo_listing=True, permitted=False
        )
        sandbox.push()
        before = sandbox.head()
        runner = UpdateRunner(sandbox.settings)

        run_to_completion(runner)

        report = runner.status()
        assert report.state is UpdateState.FAILED
        assert report.step is UpdateStep.PREFLIGHT
        assert sandbox.head() == before


class TestRestartRefusedBySudo:
    """Preflight を通ったのに sudo が実際の再起動を拒否した場合（S5 の回帰）."""

    def test_failure_is_written_back_to_the_report(self, tmp_path: Path):
        """このプロセスは死んでいないので、握り潰さず report に残せる."""
        sandbox = make_update_sandbox(tmp_path / "refused", restart_exit_code=1)
        target = sandbox.push()
        runner = UpdateRunner(sandbox.settings)

        run_to_completion(runner)

        report = runner.status()
        assert report.state is UpdateState.FAILED
        assert "再起動" in report.error
        # 更新自体は完了している（git は進んでいる）
        assert report.to_head == target
        assert sandbox.head() == target


class TestRestartableStates:
    """`systemctl is-active` の答えのうち、どれを再起動対象にするか."""

    @pytest.mark.parametrize("state", ["active", "activating", "reloading", "failed"])
    def test_units_worth_restarting_are_picked_up(self, state: str, tmp_path: Path):
        """`failed` を落とすと、起動に失敗したリビジョンの修正が永久に届かない."""
        sandbox = make_update_sandbox(
            tmp_path / state, active=(API_UNIT,), active_state=state
        )

        assert UpdateRunner(sandbox.settings).plan().restart_units == (API_UNIT,)

    @pytest.mark.parametrize("state", ["inactive", "deactivating"])
    def test_deliberately_stopped_units_are_left_alone(
        self, state: str, tmp_path: Path
    ):
        """止めてある unit を更新のついでに起こさない."""
        sandbox = make_update_sandbox(
            tmp_path / state, active=(API_UNIT,), active_state=state
        )

        assert UpdateRunner(sandbox.settings).plan().restart_units == ()


class TestStatusSurvivesMissingTools:
    """`GET /api/update/status` の「常に 200」は外部コマンドの不在でも崩れない."""

    def test_absent_git_is_reported_as_a_reason(self, tmp_path: Path):
        """Git の無いホストでも例外にせず理由を返す（500 にしない）."""
        sandbox = make_update_sandbox(tmp_path / "no-git")
        settings = attrs.evolve(
            sandbox.settings, git_bin=str(tmp_path / "absent" / "git")
        )

        plan = UpdateRunner(settings).plan()

        assert plan.repository is None
        assert plan.repository_error
        assert plan.update_available is False

    def test_absent_systemctl_yields_no_restart_targets(self, tmp_path: Path):
        sandbox = make_update_sandbox(tmp_path / "no-systemctl")
        settings = attrs.evolve(
            sandbox.settings, systemctl_bin=str(tmp_path / "absent" / "systemctl")
        )

        assert UpdateRunner(settings).plan().restart_units == ()


class TestUnitDriftWarning:
    """更新後に設置済み unit が古いままなら警告する（計画書「既知のリスク 6」）.

    本 MR 自身が `render_unit` の出力を 2 行変えたので、更新しただけの機体では
    unit が古いまま残る。**読み取りだけを行い、自動 install はしない。**
    """

    def test_outdated_unit_file_is_reported_as_a_warning(self, tmp_path: Path):
        sandbox = make_update_sandbox(tmp_path / "drift", with_service_script=True)
        sandbox.install_unit(API_UNIT, "[Service]\nExecStart=/bin/true\n")
        sandbox.install_unit(UI_UNIT, sandbox.rendered_unit("ui"))
        sandbox.push()
        runner = UpdateRunner(sandbox.settings)

        run_to_completion(runner)

        report = runner.status()
        assert report.state is UpdateState.RESTARTING
        assert len(report.warnings) == 1
        assert API_UNIT in report.warnings[0]
        assert UI_UNIT not in report.warnings[0]
        assert "web-service.sh install" in report.warnings[0]

    def test_current_unit_files_produce_no_warning(self, tmp_path: Path):
        sandbox = make_update_sandbox(tmp_path / "current", with_service_script=True)
        sandbox.install_unit(API_UNIT, sandbox.rendered_unit("api"))
        sandbox.install_unit(UI_UNIT, sandbox.rendered_unit("ui"))
        sandbox.push()
        runner = UpdateRunner(sandbox.settings)

        run_to_completion(runner)

        assert runner.status().warnings == ()

    def test_unreadable_unit_directory_does_not_fail_the_update(self, tmp_path: Path):
        """警告のための読み取りが失敗しても、更新本体と再起動は止めない（R2）."""
        sandbox = make_update_sandbox(tmp_path / "drift-oserror")
        target = sandbox.push()
        # unit_dir も scripts/ も無いホスト（差分を取りようがない）
        runner = UpdateRunner(sandbox.settings)

        run_to_completion(runner)

        report = runner.status()
        assert report.state is UpdateState.RESTARTING
        assert report.warnings == ()
        assert sandbox.head() == target
        assert sandbox.restarts() != []


class TestUnexpectedFailureIsRecorded:
    """更新スレッドの例外が report に残る（S1 の回帰）."""

    def test_missing_executable_fails_the_run_instead_of_hanging(self, tmp_path: Path):
        """`uv` が設定したパスに無い機体。握り潰すと running のまま固まる."""
        sandbox = make_update_sandbox(tmp_path / "no-uv")
        sandbox.push()
        settings = attrs.evolve(
            sandbox.settings, uv_bin=str(tmp_path / "absent" / "uv")
        )
        runner = UpdateRunner(settings)

        run_to_completion(runner)

        report = runner.status()
        assert report.state is UpdateState.FAILED
        assert report.error
        assert sandbox.restarts() == []
