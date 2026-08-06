"""ソフトウェア更新 core の公開契約テスト。"""

from __future__ import annotations

import json
import subprocess
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from pcbasm.software_update import (
    UpdateConfig,
    UpdateManager,
    UpdatePhase,
    UpdatePlatform,
    UpdateStatus,
    UpdateStatusStore,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


class GitRemote:
    """tmp_path 上だけで完結する実 bare remote と管理 checkout。"""

    def __init__(self, root: Path) -> None:
        self.remote = root / "origin.git"
        self.author = root / "author"
        self.checkout = root / "management"
        subprocess.run(("git", "init", "--bare", str(self.remote)), check=True)
        subprocess.run(
            ("git", "init", "--initial-branch=main", str(self.author)), check=True
        )
        _git(self.author, "config", "user.email", "test@example.invalid")
        _git(self.author, "config", "user.name", "Update Test")
        _git(self.author, "remote", "add", "origin", str(self.remote))
        config = self.author / "config"
        config.mkdir()
        (config / "machine.toml").write_text('machine_id = "test"\n')
        self.commit("initial", packages=("git",), schema="1")
        _git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        subprocess.run(
            ("git", "clone", str(self.remote), str(self.checkout)), check=True
        )

    def commit(
        self,
        message: str,
        *,
        packages: tuple[str, ...] = ("git",),
        schema: str = "1",
        push: bool = True,
    ) -> str:
        deploy = self.author / "deploy"
        deploy.mkdir(exist_ok=True)
        (deploy / "os-packages.txt").write_text("\n".join(packages) + "\n")
        (deploy / "schema-version").write_text(schema + "\n")
        (self.author / "revision.txt").write_text(message + "\n")
        _git(self.author, "add", ".")
        _git(self.author, "commit", "-m", message)
        if push:
            _git(self.author, "push", "origin", "HEAD")
        return _git(self.author, "rev-parse", "HEAD")

    def branch(self, name: str) -> str:
        _git(self.author, "switch", "-c", name)
        revision = self.commit(f"branch {name}")
        _git(self.checkout, "fetch", "origin")
        _git(self.checkout, "switch", "-c", name, f"origin/{name}")
        return revision


class RecordingPlatform(UpdatePlatform):
    """OS/systemd/uv 境界だけを置き換える test platform。"""

    def __init__(self) -> None:
        self.missing: tuple[str, ...] = ()
        self.unhealthy_targets: set[tuple[str, str]] = set()
        self.fail_at: str | None = None
        self.operations: list[tuple[str, str]] = []

    def missing_os_packages(self, packages: tuple[str, ...]) -> tuple[str, ...]:
        self.operations.append(("packages", " ".join(packages)))
        return tuple(package for package in packages if package in self.missing)

    def pull_lfs(self, release: Path) -> None:
        self._record("lfs", release.name)

    def create_venv(self, release: Path) -> None:
        self._record("venv", release.name)

    def sync_environment(self, release: Path) -> None:
        self._record("sync", release.name)

    def verify_entrypoint(self, release: Path, role: str) -> None:
        self._record("verify", role)

    def restart(self, role: str) -> None:
        self._record("restart", role)

    def wait_healthy(self, role: str, revision: str, timeout_seconds: float) -> bool:
        self.operations.append(("health", f"{role}:{revision}:{timeout_seconds:g}"))
        return (role, revision) not in self.unhealthy_targets

    def _record(self, operation: str, value: str) -> None:
        self.operations.append((operation, value))
        if self.fail_at == operation:
            raise RuntimeError(f"{operation} failed")


def _manager(git_remote: GitRemote, root: Path, platform: RecordingPlatform):
    config = UpdateConfig(
        management_repo=git_remote.checkout,
        state_dir=root / "state",
        releases_dir=root / "releases",
        installed_schema_version="1",
    )
    return UpdateManager(config, platform), config


class TestUpdateStatusStore:
    def test_phase_values_are_the_wire_contract(self):
        assert {phase.value for phase in UpdatePhase} == {
            "disabled",
            "idle",
            "checking",
            "available",
            "queued",
            "preparing",
            "restarting",
            "rolling_back",
            "succeeded",
            "rolled_back",
            "blocked",
            "failed",
        }

    def test_status_round_trips_as_persistent_json(self, tmp_path: Path):
        store = UpdateStatusStore(tmp_path, "api")
        expected = UpdateStatus(
            role="api",
            enabled=True,
            phase=UpdatePhase.AVAILABLE,
            available="b" * 40,
            previous="a" * 40,
            branch="main",
            can_apply=True,
            message="更新できます",
        )

        store.save(expected)

        assert store.load() == expected

    def test_broken_status_fails_closed(self, tmp_path: Path):
        (tmp_path / "status-api.json").write_text("{broken")

        status = UpdateStatusStore(tmp_path, "api").load()

        assert status.phase is UpdatePhase.FAILED
        assert status.can_apply is False
        assert status.blockers
        assert "status" in status.message.lower()

    def test_status_with_invalid_field_type_fails_closed(self, tmp_path: Path):
        status = UpdateStatus(role="api", enabled=True).to_dict()
        status["can_apply"] = "yes"
        (tmp_path / "status-api.json").write_text(json.dumps(status))

        loaded = UpdateStatusStore(tmp_path, "api").load()

        assert loaded.phase is UpdatePhase.FAILED
        assert loaded.can_apply is False

    def test_api_and_ui_share_one_nonblocking_flock(self, tmp_path: Path):
        api_store = UpdateStatusStore(tmp_path, "api")
        ui_store = UpdateStatusStore(tmp_path, "ui")

        first: AbstractContextManager[None] = api_store.lock()
        with first:
            with pytest.raises(BlockingIOError):
                with ui_store.lock():
                    pass


class TestUpdateCheck:
    def test_fast_forward_remote_commit_is_available(self, tmp_path: Path):
        remote = GitRemote(tmp_path)
        target = remote.commit("next")
        platform = RecordingPlatform()
        manager, config = _manager(remote, tmp_path, platform)

        status = manager.check("api")

        assert status.phase is UpdatePhase.AVAILABLE
        assert status.available == target
        assert status.branch == "main"
        assert status.branch_change is False
        assert status.can_apply is True

    def test_target_manifest_blocks_before_release_creation(self, tmp_path: Path):
        remote = GitRemote(tmp_path)
        remote.commit("needs package", packages=("git", "new-runtime"))
        platform = RecordingPlatform()
        platform.missing = ("new-runtime",)
        manager, config = _manager(remote, tmp_path, platform)

        status = manager.check("ui")

        assert status.phase is UpdatePhase.BLOCKED
        assert status.can_apply is False
        assert status.missing_os_packages == ("new-runtime",)
        assert "./install-os-packages.sh --ref origin/main" in status.message
        assert not config.releases_dir.exists()

    def test_schema_change_is_blocked(self, tmp_path: Path):
        remote = GitRemote(tmp_path)
        target = remote.commit("schema two", schema="2")
        manager, _ = _manager(remote, tmp_path, RecordingPlatform())

        status = manager.check("api")

        assert status.phase is UpdatePhase.BLOCKED
        assert status.available == target
        assert any("schema" in blocker.lower() for blocker in status.blockers)
        assert "web-service.sh install" in status.message

    def test_management_checkout_branch_change_is_reported(self, tmp_path: Path):
        remote = GitRemote(tmp_path)
        manager, _ = _manager(remote, tmp_path, RecordingPlatform())
        manager.check("ui")
        target = remote.branch("release")

        status = manager.check("ui")

        assert status.available == target
        assert status.branch == "release"
        assert status.branch_change is True
        assert status.can_apply is False

    def test_same_branch_force_push_is_blocked(self, tmp_path: Path):
        remote = GitRemote(tmp_path)
        revision_a = _git(remote.checkout, "rev-parse", "HEAD")
        revision_b = remote.commit("next")
        platform = RecordingPlatform()
        manager, config = _manager(remote, tmp_path, platform)
        release_b = config.releases_dir / revision_b
        release_b.mkdir(parents=True)
        config.current_link("api").symlink_to(release_b)
        _git(remote.author, "reset", "--hard", revision_a)
        remote.commit("replacement", push=False)
        _git(remote.author, "push", "--force", "origin", "HEAD")

        status = manager.check("api")

        assert status.phase is UpdatePhase.BLOCKED
        assert any("non-fast-forward" in blocker for blocker in status.blockers)
        assert config.current_link("api").resolve() == release_b

    def test_missing_remote_fails_closed(self, tmp_path: Path):
        remote = GitRemote(tmp_path)
        _git(remote.checkout, "remote", "remove", "origin")
        manager, _ = _manager(remote, tmp_path, RecordingPlatform())

        status = manager.check("api")

        assert status.phase is UpdatePhase.FAILED
        assert status.can_apply is False

    def test_invalid_manifest_fails_closed_before_release_creation(
        self, tmp_path: Path
    ):
        remote = GitRemote(tmp_path)
        remote.commit("invalid package", packages=("git", "bad package"))
        manager, config = _manager(remote, tmp_path, RecordingPlatform())

        status = manager.check("api")

        assert status.phase is UpdatePhase.FAILED
        assert status.can_apply is False
        assert not config.releases_dir.exists()


class TestUpdateApply:
    def test_branch_change_requires_exact_branch_confirmation(self, tmp_path: Path):
        remote = GitRemote(tmp_path)
        platform = RecordingPlatform()
        manager, config = _manager(remote, tmp_path, platform)
        manager.check("api")
        target = remote.branch("release")
        manager.check("api")

        with pytest.raises(ValueError, match="branch"):
            manager.apply(
                "api",
                expected_branch="release",
                expected_revision=target,
                confirmed=True,
                branch_confirmation="Release",
            )

        persisted = UpdateStatusStore(config.state_dir, "api").load()
        assert persisted.phase is UpdatePhase.BLOCKED
        assert persisted.running is False

    @pytest.mark.parametrize("failure", ("lfs", "venv", "sync", "verify"))
    def test_failed_preparation_never_switches_current_release(
        self, tmp_path: Path, failure: str
    ):
        remote = GitRemote(tmp_path)
        previous_revision = _git(remote.checkout, "rev-parse", "HEAD")
        target = remote.commit("next")
        platform = RecordingPlatform()
        platform.fail_at = failure
        manager, config = _manager(remote, tmp_path, platform)
        previous = config.releases_dir / previous_revision
        previous.mkdir(parents=True)
        config.current_link("api").symlink_to(previous)
        manager.check("api")

        status = manager.apply(
            "api",
            expected_branch="main",
            expected_revision=target,
            confirmed=True,
        )

        assert status.phase is UpdatePhase.FAILED
        assert config.current_link("api").resolve() == previous
        assert "restart" not in [operation for operation, _ in platform.operations]

    def test_ui_failure_rolls_back_only_ui_after_api_succeeds(self, tmp_path: Path):
        remote = GitRemote(tmp_path)
        revision_a = _git(remote.checkout, "rev-parse", "HEAD")
        revision_b = remote.commit("next")
        platform = RecordingPlatform()
        platform.unhealthy_targets.add(("ui", revision_b))
        manager, config = _manager(remote, tmp_path, platform)
        release_a = config.releases_dir / revision_a
        release_a.mkdir(parents=True)
        config.current_link("api").symlink_to(release_a)
        config.current_link("ui").symlink_to(release_a)

        manager.check("api")
        api_status = manager.apply(
            "api",
            expected_branch="main",
            expected_revision=revision_b,
            confirmed=True,
        )
        manager.check("ui")
        ui_status = manager.apply(
            "ui",
            expected_branch="main",
            expected_revision=revision_b,
            confirmed=True,
        )

        assert api_status.phase is UpdatePhase.SUCCEEDED
        assert ui_status.phase is UpdatePhase.ROLLED_BACK
        assert config.current_link("api").resolve().name == revision_b
        assert config.current_link("ui").resolve().name == revision_a
        release_b = config.releases_dir / revision_b
        for relative in (Path("config"), Path("uploads"), Path("data/webui")):
            assert (release_b / relative).resolve() == (
                remote.checkout / relative
            ).resolve()

    def test_failed_rollback_health_is_persisted_as_failed(self, tmp_path: Path):
        remote = GitRemote(tmp_path)
        revision_a = _git(remote.checkout, "rev-parse", "HEAD")
        revision_b = remote.commit("next")
        platform = RecordingPlatform()
        platform.unhealthy_targets.update({("api", revision_b), ("api", revision_a)})
        manager, config = _manager(remote, tmp_path, platform)
        release_a = config.releases_dir / revision_a
        release_a.mkdir(parents=True)
        config.current_link("api").symlink_to(release_a)
        manager.check("api")

        status = manager.apply(
            "api",
            expected_branch="main",
            expected_revision=revision_b,
            confirmed=True,
        )

        assert status.phase is UpdatePhase.FAILED
        assert config.current_link("api").resolve() == release_a
        assert "復旧" in status.message
