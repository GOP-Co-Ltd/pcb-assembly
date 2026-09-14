"""`web.selfupdate.service` の再起動予約と表示文言の契約テスト.

実機で観測された不具合の回帰が主目的:

- `sudo -n systemctl restart --no-block <units>` は job を enqueue した時点で exit するが、
  その job が**自分の cgroup**（`pcbasm-api.service`）を止めるほうが先行しうる。すると
  `sudo` が SIGTERM で死んで終了コードが `-15` になり、`schedule_restart` が
  「再起動コマンドが失敗しました（終了コード -15）」と嘘の失敗を report に残していた

モックは使わない。`sudo` の seam には **実スタブ実行ファイル**を差す
（`tests/web/selfupdate/conftest.py` と同じ方式）。
"""

from __future__ import annotations

from pathlib import Path

import attrs

from tests.web.selfupdate.conftest import call_log, write_systemctl_stub
from web.selfupdate.service import (
    restart_scheduled_notice,
    schedule_restart,
    unit_summary,
)
from web.selfupdate.settings import UpdateSettings

API_UNIT = "pcbasm-api.service"
UI_UNIT = "pcbasm-ui.service"


def _write_sudo(path: Path, *, log: Path, body: str) -> str:
    """引数をログへ追記してから `body` を実行する `sudo` スタブを書く."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/bash\n" f'printf "%s %s\\n" sudo "$*" >> "{log}"\n' f"{body}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def _settings(tmp_path: Path, *, sudo_body: str) -> UpdateSettings:
    log = tmp_path / "calls.log"
    return UpdateSettings(
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        systemctl_bin=write_systemctl_stub(tmp_path / "bin" / "systemctl", log=log),
        sudo_bin=_write_sudo(tmp_path / "bin" / "sudo", log=log, body=sudo_body),
        restart_delay=0.0,
    )


class TestScheduleRestart:
    """`schedule_restart()` が「失敗」と呼ぶべき事象の範囲."""

    def test_self_termination_by_the_requested_restart_is_not_a_failure(
        self, tmp_path: Path
    ):
        """自分が要求した restart が cgroup ごと止めた場合（-15）は成功扱い."""
        settings = _settings(tmp_path, sudo_body="kill -TERM $$\nsleep 5")

        reason = schedule_restart(settings, (API_UNIT, UI_UNIT))

        assert reason is None
        assert call_log(tmp_path / "calls.log") != []

    def test_refused_command_is_reported_with_its_exit_code(self, tmp_path: Path):
        """Sudo が拒否した（非 0 で生き残った）ときは従来どおり理由を返す."""
        settings = _settings(
            tmp_path, sudo_body='echo "a password is required"\nexit 1'
        )

        reason = schedule_restart(settings, (API_UNIT,))

        assert reason is not None
        assert "終了コード 1" in reason

    def test_missing_executable_is_still_a_failure(self, tmp_path: Path):
        """起動できないコマンドを「シグナルで死んだ」と取り違えない.

        `run_command` は実行ファイル不在も `returncode=-1`（= `-SIGHUP`）で返す。
        負の終了コードを一律で成功にすると、再起動されないまま誰も気付けない。
        """
        settings = attrs.evolve(
            _settings(tmp_path, sudo_body="exit 0"),
            sudo_bin=str(tmp_path / "bin" / "absent-sudo"),
        )

        reason = schedule_restart(settings, (API_UNIT,))

        assert reason is not None
        assert "-1" in reason

    def test_no_units_runs_no_command(self, tmp_path: Path):
        settings = _settings(tmp_path, sudo_body="exit 0")

        assert schedule_restart(settings, ()) is None
        assert call_log(tmp_path / "calls.log") == []


class TestUnitSummary:
    """表示文字列はサーバが組む（JS・テンプレートに複製しない）."""

    def test_lists_labels_and_full_unit_names(self):
        summary = unit_summary((API_UNIT, UI_UNIT))

        assert "backend WebAPI" in summary
        assert "UI frontend" in summary
        assert f"{API_UNIT} {UI_UNIT}" in summary


class TestRestartScheduledNotice:
    """再起動を予約したことを伝える 1 文（ファームウェア再起動から使う）."""

    def test_warns_that_the_screen_drops_when_the_ui_is_included(self):
        notice = restart_scheduled_notice((API_UNIT, UI_UNIT))

        assert UI_UNIT in notice
        assert "接続が切れます" in notice

    def test_backend_only_restart_does_not_warn_about_the_screen(self):
        notice = restart_scheduled_notice((API_UNIT,))

        assert API_UNIT in notice
        assert "接続が切れます" not in notice

    def test_without_units_says_nothing_is_restarted(self):
        assert "行いません" in restart_scheduled_notice(())
