"""Host 上で release worktree を安全に切り替えるソフトウェア更新基盤."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Protocol


class UpdatePhase(str, Enum):
    """永続 status と HTTP で共有する phase 契約."""

    DISABLED = "disabled"
    IDLE = "idle"
    CHECKING = "checking"
    AVAILABLE = "available"
    QUEUED = "queued"
    PREPARING = "preparing"
    RESTARTING = "restarting"
    ROLLING_BACK = "rolling_back"
    SUCCEEDED = "succeeded"
    ROLLED_BACK = "rolled_back"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class UpdateStatus:
    """Role ごとの、表示に必要な解決済み更新状態."""

    role: str
    enabled: bool = False
    phase: UpdatePhase = UpdatePhase.DISABLED
    running: bool = False
    available: str | None = None
    previous: str | None = None
    branch: str | None = None
    branch_change: bool = False
    can_apply: bool = False
    blockers: tuple[str, ...] = ()
    missing_os_packages: tuple[str, ...] = ()
    request_id: str | None = None
    checked_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    message: str = "ソフトウェア更新は導入されていません"

    def to_dict(self) -> dict[str, Any]:
        """JSON 化可能な wire 表現を返す."""
        data = asdict(self)
        data["phase"] = self.phase.value
        return data

    @classmethod
    def from_dict(cls, data: object, *, expected_role: str) -> UpdateStatus:
        """永続 JSON を厳格に読み、壊れていれば例外にする."""
        if not isinstance(data, dict):
            raise ValueError("status must be an object")
        values = dict(data)
        allowed = {field.name for field in fields(cls)}
        if set(values) != allowed:
            raise ValueError("status fields do not match")
        if values.get("role") != expected_role:
            raise ValueError("status role does not match")
        values["phase"] = UpdatePhase(values["phase"])
        for key in ("enabled", "running", "branch_change", "can_apply"):
            if not isinstance(values.get(key), bool):
                raise ValueError(f"invalid {key}")
        for key in (
            "available",
            "previous",
            "branch",
            "request_id",
            "checked_at",
            "started_at",
            "finished_at",
        ):
            value = values.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"invalid {key}")
        if not isinstance(values.get("message"), str):
            raise ValueError("invalid message")
        for key in ("blockers", "missing_os_packages"):
            value = values.get(key, ())
            if not isinstance(value, list | tuple) or not all(
                isinstance(item, str) for item in value
            ):
                raise ValueError(f"invalid {key}")
            values[key] = tuple(value)
        return cls(**values)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json_atomic(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


class UpdateStatusStore:
    """Role status と全 role 共通 nonblocking lock の永続境界."""

    def __init__(self, state_dir: Path, role: str) -> None:
        _validate_role(role)
        self._state_dir = state_dir
        self._role = role
        self._path = state_dir / f"status-{role}.json"
        self._lock_path = state_dir / "update.lock"

    def load(self) -> UpdateStatus:
        if not self._path.exists():
            return UpdateStatus(role=self._role)
        try:
            return UpdateStatus.from_dict(
                json.loads(self._path.read_text()), expected_role=self._role
            )
        except (OSError, ValueError, KeyError, TypeError):
            return UpdateStatus(
                role=self._role,
                enabled=True,
                phase=UpdatePhase.FAILED,
                blockers=("永続 status を読み取れません",),
                message="Software update status is broken; apply is disabled",
            )

    def save(self, status: UpdateStatus) -> None:
        if status.role != self._role:
            raise ValueError("status role does not match store")
        _write_json_atomic(self._path, status.to_dict())

    @contextmanager
    def lock(self) -> Iterator[None]:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class UpdateConfig:
    """管理 checkout と release/state 配置の固定設定."""

    management_repo: Path
    state_dir: Path = Path("/var/lib/pcbasm/state")
    releases_dir: Path = Path("/var/lib/pcbasm/releases")
    installed_schema_version: str = "1"
    health_timeout_seconds: float = 60.0

    def current_link(self, role: str) -> Path:
        _validate_role(role)
        return self.releases_dir.parent / f"current-{role}"

    def previous_link(self, role: str) -> Path:
        _validate_role(role)
        return self.releases_dir.parent / f"previous-{role}"

    @classmethod
    def from_env(cls) -> UpdateConfig:
        repo = Path(os.environ.get("PCBASM_MANAGEMENT_REPO", Path.cwd()))
        state = Path(os.environ.get("PCBASM_STATE_DIR", "/var/lib/pcbasm/state"))
        releases = Path(
            os.environ.get("PCBASM_RELEASES_DIR", "/var/lib/pcbasm/releases")
        )
        schema = os.environ.get("PCBASM_INSTALLED_SCHEMA_VERSION", "1")
        return cls(repo, state, releases, schema)


class UpdatePlatform:
    """テスト用 platform を差し替えるための公開 marker."""


class _UpdatePlatformContract(Protocol):
    def missing_os_packages(self, packages: tuple[str, ...]) -> tuple[str, ...]: ...

    def pull_lfs(self, release: Path) -> None: ...

    def create_venv(self, release: Path) -> None: ...

    def sync_environment(self, release: Path) -> None: ...

    def verify_entrypoint(self, release: Path, role: str) -> None: ...

    def restart(self, role: str) -> None: ...

    def wait_healthy(
        self, role: str, revision: str, timeout_seconds: float
    ) -> bool: ...


class SystemUpdatePlatform(UpdatePlatform):
    """APT/systemd/uv/health の実 OS 境界（role と command は固定）."""

    _HEALTH_URLS: ClassVar[dict[str, str]] = {
        "api": "http://127.0.0.1:8081/api/health",
        "ui": "http://127.0.0.1:8080/api/health",
    }

    def missing_os_packages(self, packages: tuple[str, ...]) -> tuple[str, ...]:
        missing: list[str] = []
        for package in packages:
            completed = subprocess.run(
                ("dpkg-query", "-W", "-f=${db:Status-Status}", package),
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0 or completed.stdout.strip() != "installed":
                missing.append(package)
        return tuple(missing)

    def pull_lfs(self, release: Path) -> None:
        self._run(("git", "-C", str(release), "lfs", "pull"))

    def create_venv(self, release: Path) -> None:
        self._run(("uv", "venv", "--system-site-packages"), cwd=release)

    def sync_environment(self, release: Path) -> None:
        self._run(("uv", "sync", "--locked", "--all-extras"), cwd=release)

    def verify_entrypoint(self, release: Path, role: str) -> None:
        _validate_role(role)
        module = "web.api.app" if role == "api" else "web.ui.app"
        self._run(
            (str(release / ".venv" / "bin" / "python"), "-c", f"import {module}"),
            cwd=release,
        )

    def restart(self, role: str) -> None:
        _validate_role(role)
        self._run(("sudo", "systemctl", "restart", f"pcbasm-{role}.service"))

    def wait_healthy(self, role: str, revision: str, timeout_seconds: float) -> bool:
        _validate_role(role)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    self._HEALTH_URLS[role], timeout=min(2.0, timeout_seconds)
                ) as response:
                    payload = json.loads(response.read())
                if not isinstance(payload, dict):
                    continue
                if (
                    payload.get("status") == "ok"
                    and payload.get("service") == role
                    and payload.get("revision") == revision
                ):
                    return True
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(1.0)
        return False

    @staticmethod
    def _run(command: tuple[str, ...], *, cwd: Path | None = None) -> None:
        subprocess.run(command, cwd=cwd, check=True)


def _validate_role(role: str) -> None:
    if role not in {"api", "ui"}:
        raise ValueError(f"unknown software update role: {role}")


class UpdateManager:
    """Git target を検査し、role 単位で release を切り替える."""

    def __init__(self, config: UpdateConfig, platform: _UpdatePlatformContract) -> None:
        self._config = config
        self._platform = platform

    def check(self, role: str) -> UpdateStatus:
        store = UpdateStatusStore(self._config.state_dir, role)
        try:
            with store.lock():
                return self._check_locked(role, store)
        except BlockingIOError:
            current = store.load()
            return UpdateStatus(
                **{
                    **current.to_dict(),
                    "phase": UpdatePhase.CHECKING,
                    "running": True,
                    "can_apply": False,
                    "message": "別の更新処理が実行中です",
                }
            )

    def apply(
        self,
        role: str,
        *,
        expected_branch: str,
        expected_revision: str,
        confirmed: bool,
        branch_confirmation: str | None = None,
        request_id: str | None = None,
    ) -> UpdateStatus:
        store = UpdateStatusStore(self._config.state_dir, role)
        try:
            with store.lock():
                checked = self._check_locked(role, store)
                self._validate_apply(
                    checked,
                    expected_branch=expected_branch,
                    expected_revision=expected_revision,
                    confirmed=confirmed,
                    branch_confirmation=branch_confirmation,
                )
                return self._apply_locked(role, checked, store, request_id=request_id)
        except BlockingIOError:
            current = store.load()
            status = replace(
                current,
                phase=UpdatePhase.BLOCKED,
                running=False,
                can_apply=False,
                blockers=("別の更新処理が実行中です",),
                message="別の更新処理が実行中です",
            )
            store.save(status)
            return status
        except ValueError as exc:
            current = store.load()
            store.save(
                replace(
                    current,
                    enabled=True,
                    phase=UpdatePhase.BLOCKED,
                    running=False,
                    can_apply=False,
                    blockers=(*current.blockers, str(exc)),
                    finished_at=_now(),
                    message=str(exc),
                )
            )
            raise
        except Exception as exc:
            current = store.load()
            status = replace(
                current,
                enabled=True,
                phase=UpdatePhase.FAILED,
                running=False,
                can_apply=False,
                blockers=(str(exc),),
                finished_at=_now(),
                message=f"更新準備に失敗しました: {exc}",
            )
            store.save(status)
            return status

    def _check_locked(self, role: str, store: UpdateStatusStore) -> UpdateStatus:
        previous_status = store.load()
        checking = replace(
            previous_status,
            role=role,
            enabled=True,
            phase=UpdatePhase.CHECKING,
            running=True,
            can_apply=False,
            blockers=(),
            missing_os_packages=(),
            message="更新を確認しています",
        )
        store.save(checking)
        try:
            branch = self._git("branch", "--show-current")
            if not branch:
                raise RuntimeError("管理 checkout が detached HEAD です")
            self._git("fetch", "origin", branch)
            target = self._git("rev-parse", f"origin/{branch}")
            installed = self._installed_revision(role)
            # check 済み branch change は apply の再確認でも保持する。管理 checkout を
            # switch した直後の 1 回目だけで失うと、完全一致確認を迂回できてしまう。
            branch_change = previous_status.branch_change or bool(
                previous_status.branch and previous_status.branch != branch
            )
            blockers: list[str] = []
            if (
                not branch_change
                and installed
                and not self._is_ancestor(installed, target)
            ):
                blockers.append("同一 branch の non-fast-forward 更新は適用できません")

            packages = self._manifest_lines(target, "deploy/os-packages.txt")
            missing = self._platform.missing_os_packages(packages)
            if missing:
                blockers.append("不足している OS package があります")

            schema = self._git("show", f"{target}:deploy/schema-version").strip()
            if schema != self._config.installed_schema_version:
                blockers.append("schema-version が installed version と一致しません")

            if blockers:
                message = self._blocked_message(branch, missing, schema)
                phase = UpdatePhase.BLOCKED
                can_apply = False
            elif target == installed and not branch_change:
                message = "ソフトウェアは最新です"
                phase = UpdatePhase.IDLE
                can_apply = False
            elif branch_change:
                message = f"branch '{branch}' の完全入力による確認が必要です"
                phase = UpdatePhase.AVAILABLE
                can_apply = False
            else:
                message = "更新できます"
                phase = UpdatePhase.AVAILABLE
                can_apply = True
            status = UpdateStatus(
                role=role,
                enabled=True,
                phase=phase,
                available=target,
                previous=installed,
                branch=branch,
                branch_change=branch_change,
                can_apply=can_apply,
                blockers=tuple(blockers),
                missing_os_packages=missing,
                request_id=previous_status.request_id,
                checked_at=_now(),
                message=message,
            )
        except Exception as exc:
            status = UpdateStatus(
                role=role,
                enabled=True,
                phase=UpdatePhase.FAILED,
                blockers=(str(exc),),
                checked_at=_now(),
                message=f"更新確認に失敗しました: {exc}",
            )
        store.save(status)
        return status

    def _apply_locked(
        self,
        role: str,
        checked: UpdateStatus,
        store: UpdateStatusStore,
        *,
        request_id: str | None,
    ) -> UpdateStatus:
        assert checked.available is not None
        target = checked.available
        release = self._config.releases_dir / target
        old_release = self._link_target(self._config.current_link(role))
        preparing = replace(
            checked,
            phase=UpdatePhase.PREPARING,
            running=True,
            can_apply=False,
            request_id=request_id or checked.request_id,
            started_at=_now(),
            finished_at=None,
            message="release を準備しています",
        )
        store.save(preparing)
        try:
            self._prepare_release(release, target, role)
        except Exception as exc:
            self._discard_incomplete_release(release)
            failed = replace(
                preparing,
                phase=UpdatePhase.FAILED,
                running=False,
                blockers=(str(exc),),
                finished_at=_now(),
                message=f"更新準備に失敗しました: {exc}",
            )
            store.save(failed)
            return failed

        if old_release is not None:
            self._switch_link(self._config.previous_link(role), old_release)
        self._switch_link(self._config.current_link(role), release)
        restarting = replace(
            preparing,
            phase=UpdatePhase.RESTARTING,
            message=f"{role} を再起動しています",
        )
        store.save(restarting)
        try:
            self._platform.restart(role)
            healthy = self._platform.wait_healthy(
                role, target, self._config.health_timeout_seconds
            )
        except Exception:
            healthy = False
        if healthy:
            succeeded = replace(
                restarting,
                phase=UpdatePhase.SUCCEEDED,
                running=False,
                previous=target,
                branch_change=False,
                finished_at=_now(),
                message=f"{role} を {target[:12]} へ更新しました",
            )
            store.save(succeeded)
            self._cleanup_releases()
            return succeeded
        return self._rollback(role, old_release, restarting, store)

    def _rollback(
        self,
        role: str,
        old_release: Path | None,
        restarting: UpdateStatus,
        store: UpdateStatusStore,
    ) -> UpdateStatus:
        rolling = replace(
            restarting,
            phase=UpdatePhase.ROLLING_BACK,
            message=f"{role} を直前 release へ戻しています",
        )
        store.save(rolling)
        if old_release is None:
            failed = replace(
                rolling,
                phase=UpdatePhase.FAILED,
                running=False,
                blockers=("直前 release がありません",),
                finished_at=_now(),
                message="起動確認に失敗し、rollback 先もありません",
            )
            store.save(failed)
            return failed
        try:
            self._switch_link(self._config.current_link(role), old_release)
            self._platform.restart(role)
            recovered = self._platform.wait_healthy(
                role, old_release.name, self._config.health_timeout_seconds
            )
        except Exception as exc:
            failed = replace(
                rolling,
                phase=UpdatePhase.FAILED,
                running=False,
                blockers=(str(exc),),
                finished_at=_now(),
                message=f"rollback に失敗しました: {exc}",
            )
            store.save(failed)
            return failed
        if not recovered:
            failed = replace(
                rolling,
                phase=UpdatePhase.FAILED,
                running=False,
                blockers=("rollback 後の health 確認にも失敗しました",),
                finished_at=_now(),
                message="旧 release へ戻しましたが、サービスは復旧していません",
            )
            store.save(failed)
            return failed
        rolled_back = replace(
            rolling,
            phase=UpdatePhase.ROLLED_BACK,
            running=False,
            previous=old_release.name,
            finished_at=_now(),
            message=f"起動確認に失敗したため {old_release.name[:12]} へ戻しました",
        )
        store.save(rolled_back)
        self._cleanup_releases()
        return rolled_back

    def _validate_apply(
        self,
        status: UpdateStatus,
        *,
        expected_branch: str,
        expected_revision: str,
        confirmed: bool,
        branch_confirmation: str | None,
    ) -> None:
        if not confirmed:
            raise ValueError("confirmed=true が必要です")
        if status.branch != expected_branch or status.available != expected_revision:
            raise ValueError("branch または revision が確認時点から変わりました")
        if status.blockers:
            raise ValueError("更新 blocker があるため適用できません")
        if status.branch_change and branch_confirmation != status.branch:
            raise ValueError("branch 名の完全一致確認が必要です")
        if not status.can_apply and not status.branch_change:
            raise ValueError("適用可能な更新がありません")

    def _prepare_release(self, release: Path, target: str, role: str) -> None:
        marker = release / ".pcbasm-prepared"
        if marker.is_file():
            self._platform.verify_entrypoint(release, role)
            return
        self._config.releases_dir.mkdir(parents=True, exist_ok=True)
        if release.exists():
            self._discard_incomplete_release(release)
        self._git("worktree", "add", "--detach", str(release), target)
        self._link_persistent_paths(release)
        self._platform.pull_lfs(release)
        self._platform.create_venv(release)
        self._platform.sync_environment(release)
        self._platform.verify_entrypoint(release, role)
        marker.write_text(target + "\n")

    def _link_persistent_paths(self, release: Path) -> None:
        """Release内の従来パスを管理checkout側の永続データへ接続する."""
        for relative in (Path("config"), Path("uploads"), Path("data/webui")):
            source = self._config.management_repo / relative
            destination = release / relative
            source.mkdir(parents=True, exist_ok=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink() and destination.resolve() == source.resolve():
                continue
            # config/ は配布元でも tracked directory になり得る。release は
            # disposable worktree なので、その snapshot だけを除き、管理 checkout
            # 側の永続 directory へ接続する。
            if destination.is_symlink() or destination.is_file():
                destination.unlink()
            elif destination.is_dir():
                shutil.rmtree(destination)
            destination.symlink_to(source, target_is_directory=True)

    def _discard_incomplete_release(self, release: Path) -> None:
        if not release.exists():
            return
        subprocess.run(
            (
                "git",
                "-C",
                str(self._config.management_repo),
                "worktree",
                "remove",
                "--force",
                str(release),
            ),
            check=False,
            capture_output=True,
        )
        if release.exists():
            shutil.rmtree(release)

    def _cleanup_releases(self) -> None:
        if not self._config.releases_dir.is_dir():
            return
        keep = {
            target.resolve()
            for role in ("api", "ui")
            for target in (
                self._link_target(self._config.current_link(role)),
                self._link_target(self._config.previous_link(role)),
            )
            if target is not None
        }
        for release in self._config.releases_dir.iterdir():
            if release.is_dir() and release.resolve() not in keep:
                self._discard_incomplete_release(release)

    def _installed_revision(self, role: str) -> str:
        linked = self._link_target(self._config.current_link(role))
        if linked is not None:
            return linked.name
        return self._git("rev-parse", "HEAD")

    def _manifest_lines(self, target: str, path: str) -> tuple[str, ...]:
        text = self._git("show", f"{target}:{path}")
        packages: dict[str, None] = {}
        for raw_line in text.splitlines():
            package = raw_line.partition("#")[0].strip()
            if not package:
                continue
            if re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", package) is None:
                raise ValueError(f"不正な OS package 名です: {package}")
            packages[package] = None
        if not packages:
            raise ValueError("OS package manifest が空です")
        return tuple(packages)

    def _is_ancestor(self, older: str, newer: str) -> bool:
        completed = subprocess.run(
            (
                "git",
                "-C",
                str(self._config.management_repo),
                "merge-base",
                "--is-ancestor",
                older,
                newer,
            ),
            check=False,
        )
        return completed.returncode == 0

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ("git", "-C", str(self._config.management_repo), *args),
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def _blocked_message(
        self, branch: str, missing: tuple[str, ...], schema: str
    ) -> str:
        messages: list[str] = []
        if missing:
            messages.append(
                f"不足 package を ./install-os-packages.sh --ref origin/{branch} "
                "で導入してください"
            )
        if schema != self._config.installed_schema_version:
            messages.append(
                "手動更新後に ./web-service.sh install <target> を実行してください"
            )
        return "。".join(messages) or "更新を安全に適用できません"

    @staticmethod
    def _link_target(link: Path) -> Path | None:
        if not link.is_symlink():
            return None
        return link.resolve(strict=False)

    @staticmethod
    def _switch_link(link: Path, target: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
        temporary.symlink_to(target)
        os.replace(temporary, link)


class UpdateCoordinator:
    """テスト用 coordinator を差し替えるための公開 marker."""


class UpdateCoordinatorContract(Protocol):
    """Web process から固定 oneshot を要求する structural contract."""

    def status(self) -> UpdateStatus: ...

    def request_check(self) -> str: ...

    def request_apply(
        self,
        *,
        expected_branch: str,
        expected_revision: str,
        confirmed: bool,
        branch_confirmation: str | None,
    ) -> str: ...


class SystemdUpdateCoordinator(UpdateCoordinator):
    """Request JSON を保存し sudoers で許可した unit だけを起動する."""

    def __init__(self, state_dir: Path, role: str) -> None:
        _validate_role(role)
        self._store = UpdateStatusStore(state_dir, role)
        self._state_dir = state_dir
        self._role = role

    def status(self) -> UpdateStatus:
        return self._store.load()

    def request_check(self) -> str:
        request_id = uuid.uuid4().hex
        self._start(f"pcbasm-update-check@{self._role}.service")
        return request_id

    def request_apply(
        self,
        *,
        expected_branch: str,
        expected_revision: str,
        confirmed: bool,
        branch_confirmation: str | None,
    ) -> str:
        request_id = uuid.uuid4().hex
        request_path = self._state_dir / f"request-{self._role}.json"
        previous = self._store.load()
        try:
            _write_json_atomic(
                request_path,
                {
                    "request_id": request_id,
                    "expected_branch": expected_branch,
                    "expected_revision": expected_revision,
                    "confirmed": confirmed,
                    "branch_confirmation": branch_confirmation,
                },
            )
            self._store.save(
                replace(
                    previous,
                    enabled=True,
                    phase=UpdatePhase.QUEUED,
                    running=True,
                    can_apply=False,
                    request_id=request_id,
                    message="更新を予約しました",
                )
            )
            self._start(f"pcbasm-update@{self._role}.service")
        except Exception:
            self._store.save(previous)
            request_path.unlink(missing_ok=True)
            raise
        return request_id

    @staticmethod
    def from_env(role: str) -> SystemdUpdateCoordinator | None:
        state_value = os.environ.get("PCBASM_STATE_DIR")
        if not state_value:
            return None
        return SystemdUpdateCoordinator(Path(state_value), role)

    @staticmethod
    def _start(unit: str) -> None:
        subprocess.run(("sudo", "systemctl", "start", unit, "--no-block"), check=True)


def _read_request(config: UpdateConfig, role: str) -> dict[str, Any]:
    path = config.state_dir / f"request-{role}.json"
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("update request must be an object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply"))
    parser.add_argument("--role", required=True, choices=("api", "ui"))
    args = parser.parse_args(argv)
    config = UpdateConfig.from_env()
    manager = UpdateManager(config, SystemUpdatePlatform())
    if args.action == "check":
        status = manager.check(args.role)
    else:
        request = _read_request(config, args.role)
        status = manager.apply(
            args.role,
            expected_branch=str(request["expected_branch"]),
            expected_revision=str(request["expected_revision"]),
            confirmed=request.get("confirmed") is True,
            branch_confirmation=(
                str(request["branch_confirmation"])
                if request.get("branch_confirmation") is not None
                else None
            ),
            request_id=str(request.get("request_id") or ""),
        )
    return 0 if status.phase not in {UpdatePhase.FAILED} else 1


if __name__ == "__main__":
    raise SystemExit(main())
