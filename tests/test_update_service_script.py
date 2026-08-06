"""更新用 systemd unit・sudoers・OS package installer の契約。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.helpers import PROJECT_ROOT

SERVICE_SCRIPT = PROJECT_ROOT / "web-service.sh"
OS_INSTALLER = PROJECT_ROOT / "install-os-packages.sh"
SOFTWARE_INSTALLER = PROJECT_ROOT / "install-softwares.sh"
PACKAGE_MANIFEST = PROJECT_ROOT / "deploy" / "os-packages.txt"
SCHEMA_VERSION = PROJECT_ROOT / "deploy" / "schema-version"


def _render(function: str, role: str | None, tmp_path: Path) -> str:
    invocation = f"{function} {role}" if role else function
    completed = subprocess.run(
        ("bash", "-c", f'source "$1"; {invocation}', "bash", str(SERVICE_SCRIPT)),
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            "PCBASM_STATE_DIR": str(tmp_path / "state"),
            "PCBASM_RELEASES_DIR": str(tmp_path / "releases"),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


class TestManagedServiceUnits:
    @pytest.mark.parametrize("role", ("api", "ui"))
    def test_runtime_unit_starts_from_role_specific_current_release(
        self, role: str, tmp_path: Path
    ):
        unit = _render("render_unit", role, tmp_path)

        assert f"/current-{role}" in unit
        assert "/.venv/bin/python" in unit
        assert "ExecStart=" in unit
        assert "make api" not in unit
        assert "make ui" not in unit

    @pytest.mark.parametrize("role", ("api", "ui"))
    def test_apply_unit_is_nonroot_fixed_role_oneshot(self, role: str, tmp_path: Path):
        unit = _render("render_update_apply_unit", role, tmp_path)

        assert "Type=oneshot" in unit
        assert "User=" in unit and "Group=" in unit
        assert "User=root" not in unit
        assert f"apply --role {role}" in unit
        assert "sudo" not in unit
        assert "apt" not in unit

    @pytest.mark.parametrize(
        "renderer",
        ("render_unit", "render_update_apply_unit", "render_update_check_unit"),
    )
    def test_units_pin_the_schema_of_the_active_release(
        self, renderer: str, tmp_path: Path
    ):
        release = tmp_path / "releases" / "revision"
        schema = release / "deploy" / "schema-version"
        schema.parent.mkdir(parents=True)
        schema.write_text("7\n")
        (tmp_path / "current-api").symlink_to(release)

        unit = _render(renderer, "api", tmp_path)

        assert 'Environment="PCBASM_INSTALLED_SCHEMA_VERSION=7"' in unit

    @pytest.mark.parametrize("role", ("api", "ui"))
    def test_check_timer_runs_after_boot_and_every_fifteen_minutes(
        self, role: str, tmp_path: Path
    ):
        check = _render("render_update_check_unit", role, tmp_path)
        timer = _render("render_update_timer", role, tmp_path)

        assert "Type=oneshot" in check
        assert f"check --role {role}" in check
        assert "OnBootSec=" in timer
        assert "OnUnitActiveSec=15min" in timer
        assert "Persistent=true" in timer
        assert f"Unit=pcbasm-update-check@{role}.service" in timer


class TestLimitedSudoers:
    def test_allows_only_fixed_check_apply_and_restart_units(self, tmp_path: Path):
        sudoers = _render("render_update_sudoers", None, tmp_path)

        for role in ("api", "ui"):
            assert f"systemctl start pcbasm-update@{role}.service" in sudoers
            assert f"systemctl start pcbasm-update-check@{role}.service" in sudoers
            assert f"systemctl restart pcbasm-{role}.service" in sudoers
        assert "apt" not in sudoers
        assert "*" not in sudoers
        assert "/bin/sh" not in sudoers


class TestPersistentReleasePaths:
    def test_tracked_directory_is_replaced_by_management_link(self, tmp_path: Path):
        source = tmp_path / "management" / "config"
        destination = tmp_path / "release" / "config"
        source.mkdir(parents=True)
        destination.mkdir(parents=True)
        (destination / "tracked.toml").write_text("snapshot")

        completed = subprocess.run(
            (
                "bash",
                "-c",
                'source "$1"; replace_with_persistent_link "$2" "$3"',
                "bash",
                str(SERVICE_SCRIPT),
                str(source),
                str(destination),
            ),
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        assert completed.returncode == 0, completed.stderr
        assert destination.is_symlink()
        assert destination.resolve() == source.resolve()


class TestOsPackageManifest:
    def test_manifest_and_schema_version_are_present(self):
        packages = [
            line.strip()
            for line in PACKAGE_MANIFEST.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

        assert packages == [
            "alsa-utils",
            "v4l-utils",
            "curl",
            "kicad",
            "make",
            "git",
            "git-lfs",
            "python3-picamera2",
        ]
        assert SCHEMA_VERSION.read_text().strip().isdigit()

    def test_general_installer_delegates_apt_responsibility(self):
        script = SOFTWARE_INSTALLER.read_text()

        assert "install-os-packages.sh" in script
        assert "apt-get install" not in script
        assert OS_INSTALLER.is_file()
