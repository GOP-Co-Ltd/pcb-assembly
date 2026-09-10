"""`web.selfupdate.steps` と `git_env()` の argv 契約.

計画書「1. 共通モジュール」の `steps.py`（argv を組み立てるだけの純関数）、
「設計上の要 4.」（`uv sync` の落とし穴 2 つ）、「6.」（git のハング対策）、
そして「4. sudoers」（許可する固定 argv と **順序も契約**）が根拠。

`restart_command` の出力は `/etc/sudoers.d/pcbasm-update` が許す argv と 1 文字でも
違えば実機で `sudo: a password is required` になって再起動できない。ここだけは
substring ではなくフル argv で固定する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from web.selfupdate.repo import git_env
from web.selfupdate.service import CANONICAL_ORDER, UNIT_NAMES
from web.selfupdate.settings import UpdateSettings
from web.selfupdate.steps import restart_command, smoke_command, sync_command


@pytest.fixture
def settings(tmp_path: Path) -> UpdateSettings:
    """既定値のままの設定（argv の既定を固定するため上書きしない）."""
    return UpdateSettings(repo_root=tmp_path, state_dir=tmp_path / "state")


class TestSyncCommand:
    """`uv sync` の引数（既定は `--locked --inexact`）."""

    def test_uses_the_configured_uv_binary_and_the_sync_subcommand(
        self, settings: UpdateSettings
    ):
        command = sync_command(settings)

        assert command[0] == settings.uv_bin
        assert command[1] == "sync"

    def test_keeps_the_lockfile_immutable(self, settings: UpdateSettings):
        """`--locked` が無いと pyproject とのズレで `uv.lock` が書き換わり、tree が dirty
        になって以後の更新が全部止まる（計画書「設計上の要 4.(b)」）."""
        assert "--locked" in sync_command(settings)

    def test_keeps_extra_dependency_groups_installed(self, settings: UpdateSettings):
        """`--inexact` が無いと指定しなかった group を削除する。ml-runtime を入れた Pi が torch
        を失う（計画書「設計上の要 4.(a)」）."""
        assert "--inexact" in sync_command(settings)

    @pytest.mark.parametrize("forbidden", ("--all-extras", "--frozen"))
    def test_does_not_widen_or_freeze_the_install(
        self, forbidden: str, settings: UpdateSettings
    ):
        assert forbidden not in sync_command(settings)

    def test_reflects_the_per_machine_override(self, tmp_path: Path):
        """機体ごとに env で上書きできる（確定要件の `uv sync` フラグ）."""
        settings = UpdateSettings(
            repo_root=tmp_path,
            state_dir=tmp_path / "state",
            uv_sync_args=("--locked", "--inexact", "--group", "ml-runtime"),
        )

        assert sync_command(settings)[-2:] == ("--group", "ml-runtime")


class TestSmokeCommand:
    """更新後の import smoke（起動失敗で WebUI ごと到達不能になるのを防ぐ）."""

    def test_runs_python_without_resyncing(self, settings: UpdateSettings):
        """`--no-sync` が無いと smoke 自体が依存を書き換えてしまう."""
        command = smoke_command(settings)

        assert command[0] == settings.uv_bin
        assert command[1:4] == ("run", "--no-sync", "python")
        assert command[4] == "-c"

    def test_imports_both_web_applications(self, settings: UpdateSettings):
        """同居機・専用機のどちらでも壊れていないことを 1 回で確かめる."""
        assert "import web.api.app, web.ui.app" in smoke_command(settings)[5]


class TestRestartCommand:
    """Sudoers 側で許可した固定 argv と完全一致すること."""

    @pytest.mark.parametrize(
        "units",
        (
            ("pcbasm-api.service",),
            ("pcbasm-ui.service",),
            ("pcbasm-api.service", "pcbasm-ui.service"),
        ),
    )
    def test_matches_the_permitted_invocation(
        self, units: tuple[str, ...], settings: UpdateSettings
    ):
        assert restart_command(settings, units) == (
            settings.sudo_bin,
            "-n",
            settings.systemctl_bin,
            "restart",
            "--no-block",
            *units,
        )

    def test_unit_order_follows_the_canonical_order(self, settings: UpdateSettings):
        """同居機の argv は api → ui の 1 変種しか許可されていない（順序も契約）."""
        units = tuple(UNIT_NAMES[key] for key in CANONICAL_ORDER)

        assert restart_command(settings, units)[-2:] == (
            "pcbasm-api.service",
            "pcbasm-ui.service",
        )

    def test_never_asks_for_a_password(self, settings: UpdateSettings):
        """`-n` が無いと非対話プロセスがパスワード入力待ちでハングする."""
        assert restart_command(settings, ("pcbasm-api.service",))[1] == "-n"


class TestGitEnv:
    """Git がハングしないための環境変数（計画書「設計上の要 6.」）."""

    def test_terminal_prompt_is_disabled(self):
        assert git_env()["GIT_TERMINAL_PROMPT"] == "0"

    @pytest.mark.parametrize(
        "option", ("BatchMode=yes", "ConnectTimeout=10", "StrictHostKeyChecking=yes")
    )
    def test_ssh_never_becomes_interactive(self, option: str):
        """鍵やホスト鍵の確認を求められると、ssh-agent が無い環境では無限に待つ."""
        assert option in git_env()["GIT_SSH_COMMAND"]
