"""`install.sh` の契約テスト.

unit と sudoers は、設置済みの機体と 1 文字も違わない内容を描く（違えば自己更新が drift を警告する）。
どの段も再実行して同じ状態に落ち着く。

実機の systemd・sudoers・Klipper には触れない。設置先は継ぎ目の env（SYSTEMD_UNIT_DIR、SUDOERS_DIR、SUDO、VISUDO、SERIAL_BY_ID_DIR）と HOME で一時ディレクトリへ向け、systemctl は PATH 先頭のスタブが受ける。
"""

import grp
import os
import pwd
import re
import shutil
import subprocess
from pathlib import Path

import attrs
import pytest

from pcbasm.paths import PROJECT_ROOT
from web.selfupdate.settings import UpdateSettings

INSTALL = PROJECT_ROOT / "install.sh"
SHIM = PROJECT_ROOT / "scripts" / "web-service.sh"
TEMPLATES = PROJECT_ROOT / "deploy" / "templates"

UNIT = """\
[Unit]
Description={description}
Wants=network-online.target
After=network-online.target
# 起動失敗の再試行を打ち切らせない。WebUI からの自己更新はロールバックしないので、
# start-limit-hit で停止すると復旧手段が ssh だけになる。
StartLimitIntervalSec=0

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={root}
Environment="HOME={home}"
Environment="PATH={uv_dir}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart={make} {target}
Restart=on-failure
RestartSec=5
# 停止に手間取っても既定の 90 秒は待たない（自己更新の restart が復帰待ちを食い潰す）
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
"""

DESCRIPTIONS = {"api": "PCB Assembly backend WebAPI", "ui": "PCB Assembly UI frontend"}

# 自己更新が sudo -n で呼ぶ argv（web.selfupdate.steps.restart_command）。順序も契約
SUDOERS = """\
# WebUI からのソフトウェア更新が使う唯一の特権操作。
# install.sh services が deploy/sudoers から生成する（手で編集しない）。
# 引数まで固定した argv だけを許可し、ワイルドカードは 1 文字も置かない。
Cmnd_Alias PCBASM_UPDATE_RESTART = \\
    /usr/bin/systemctl restart --no-block pcbasm-api.service, \\
    /usr/bin/systemctl restart --no-block pcbasm-ui.service, \\
    /usr/bin/systemctl restart --no-block pcbasm-api.service pcbasm-ui.service
{user} ALL=(root:root) NOPASSWD: PCBASM_UPDATE_RESTART
"""


def expected_unit(target: str) -> str:
    """この利用者とリポジトリで設置済みの機体が持つ unit の内容."""
    uv, make = shutil.which("uv"), shutil.which("make")
    assert uv is not None and make is not None
    return UNIT.format(
        description=DESCRIPTIONS[target],
        user=pwd.getpwuid(os.getuid()).pw_name,
        group=grp.getgrgid(os.getgid()).gr_name,
        root=PROJECT_ROOT,
        home=os.environ["HOME"],
        uv_dir=Path(uv).parent,
        make=make,
        target=target,
    )


def run(
    command: Path, *args: str, env: dict[str, str] | None = None, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(command), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=os.environ | (env or {}),
        timeout=30,
    )


def write_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/bash\n{body}")
    path.chmod(0o755)
    return path


@attrs.frozen
class Host:
    """設置先を一時ディレクトリへ向けたホスト（systemctl の呼び出しは log に残る）."""

    root: Path

    @property
    def units(self) -> Path:
        return self.root / "systemd"

    @property
    def sudoers(self) -> Path:
        return self.root / "sudoers.d"

    @property
    def log(self) -> Path:
        return self.root / "systemctl.log"

    def env(self, *, visudo_ok: bool = True) -> dict[str, str]:
        bin_dir = self.root / "bin"
        write_script(bin_dir / "systemctl", f'echo "$*" >> "{self.log}"\n')
        # sudo -n -l（設置後の許可の確認）は許可済みとして答え、それ以外はそのまま実行する
        sudo = write_script(
            bin_dir / "sudo", 'if [ "$1" = "-n" ]; then exit 0; fi\nexec "$@"\n'
        )
        visudo = write_script(bin_dir / "visudo", f"exit {0 if visudo_ok else 1}\n")
        self.units.mkdir(exist_ok=True)
        self.sudoers.mkdir(exist_ok=True)
        return {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SUDO": str(sudo),
            "VISUDO": str(visudo),
            "SYSTEMD_UNIT_DIR": str(self.units),
            "SUDOERS_DIR": str(self.sudoers),
        }

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def files(self) -> dict[str, str]:
        return {
            path.name: path.read_text()
            for directory in (self.units, self.sudoers)
            for path in sorted(directory.iterdir())
        }


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


class TestRender:
    """`install.sh render`（systemd に触らない。自己更新の drift 判定が読む）."""

    @pytest.mark.parametrize("target", ["api", "ui"])
    def test_unit_matches_the_installed_units(self, target: str):
        result = run(INSTALL, "render", target)

        assert result.returncode == 0, result.stderr
        assert result.stdout == expected_unit(target)

    def test_sudoers_allows_exactly_the_restart_argv(self):
        result = run(INSTALL, "render", "sudoers")

        assert result.returncode == 0, result.stderr
        assert result.stdout == SUDOERS.format(user=pwd.getpwuid(os.getuid()).pw_name)

    def test_sudoers_has_no_wildcard(self):
        """Sudo の glob は `/` にも一致するので、`*` が 1 つあれば任意のコマンドを root で実行できる."""
        assert "*" not in run(INSTALL, "render", "sudoers").stdout

    @pytest.mark.parametrize("target", ["all", "webui", ""])
    def test_rejects_other_targets(self, target: str):
        assert run(INSTALL, "render", target).returncode != 0


class TestServices:
    """`install.sh services`: unit と sudoers を設置して起動する."""

    def test_installs_both_units_and_the_sudoers(self, host: Host):
        result = run(INSTALL, "services", env=host.env())

        assert result.returncode == 0, result.stderr
        assert host.files() == {
            "pcbasm-api.service": expected_unit("api"),
            "pcbasm-ui.service": expected_unit("ui"),
            "pcbasm-update": SUDOERS.format(user=pwd.getpwuid(os.getuid()).pw_name),
        }
        assert (host.units / "pcbasm-api.service").stat().st_mode & 0o777 == 0o644
        assert (host.sudoers / "pcbasm-update").stat().st_mode & 0o777 == 0o440
        assert host.calls() == [
            "daemon-reload",
            "enable pcbasm-api.service",
            "restart pcbasm-api.service",
            "daemon-reload",
            "enable pcbasm-ui.service",
            "restart pcbasm-ui.service",
        ]

    def test_rerun_keeps_the_files_and_does_not_restart(self, host: Host):
        """実機の sudoers は root 所有の 0440 で読めないので、sudoers だけは毎回置き直す（中身は同じ）."""
        env = host.env()
        run(INSTALL, "services", env=env)
        installed = host.files()
        host.log.unlink()

        result = run(INSTALL, "services", env=env)

        assert result.returncode == 0, result.stderr
        assert host.files() == installed
        assert host.calls() == [
            "enable --now pcbasm-api.service",
            "enable --now pcbasm-ui.service",
        ]

    def test_changed_unit_is_replaced_and_restarted(self, host: Host):
        env = host.env()
        (host.units / "pcbasm-api.service").write_text("[Service]\n")

        run(INSTALL, "services", "api", env=env)

        assert host.files()["pcbasm-api.service"] == expected_unit("api")
        assert "restart pcbasm-api.service" in host.calls()

    def test_ui_only_installs_only_the_frontend(self, host: Host):
        result = run(INSTALL, "--ui-only", "services", env=host.env())

        assert result.returncode == 0, result.stderr
        assert sorted(host.files()) == ["pcbasm-ui.service", "pcbasm-update"]

    def test_sudoers_rejected_by_visudo_is_not_installed(self, host: Host):
        """壊れた sudoers は sudo 全体を止め、復旧の手段まで失う."""
        result = run(INSTALL, "services", env=host.env(visudo_ok=False))

        assert result.returncode != 0
        assert list(host.sudoers.iterdir()) == []

    def test_unknown_target_touches_nothing(self, host: Host):
        result = run(INSTALL, "services", "webui", env=host.env())

        assert result.returncode != 0
        assert host.files() == {}
        assert host.calls() == []


class TestPython:
    """`install.sh python`: 版を固定した uv で venv を用意する."""

    def test_existing_venv_is_synced_not_recreated(self, tmp_path: Path):
        """稼働中のサービスが使う venv を消さず、自己更新と同じ引数で同期だけする."""
        version = re.search(r'^UV_VERSION="(.+)"$', INSTALL.read_text(), re.M)
        assert version is not None and (PROJECT_ROOT / ".venv").is_dir()
        log = tmp_path / "calls.log"
        bin_dir = tmp_path / "bin"
        write_script(
            bin_dir / "uv",
            f'if [ "$1" = --version ]; then echo "uv {version[1]}"; '
            f'else echo "uv $*" >> "{log}"; fi\n',
        )
        write_script(bin_dir / "make", f'echo "make $*" >> "{log}"\n')

        result = run(
            INSTALL,
            "python",
            env={"HOME": str(tmp_path), "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        )

        assert result.returncode == 0, result.stderr
        assert log.read_text().splitlines() == [
            " ".join(("uv", "sync", *UpdateSettings().uv_sync_args))
        ]


class TestKlipper:
    """`install.sh klipper`: 導入済みの Klipper へ独自 kinematics を symlink する."""

    def test_rerun_keeps_the_kinematics_link(self, tmp_path: Path):
        kinematics = tmp_path / "klipper" / "klippy" / "kinematics"
        kinematics.mkdir(parents=True)
        write_script(tmp_path / "klippy-env" / "bin" / "pip", "exit 0\n")

        for _ in range(2):
            result = run(INSTALL, "klipper", env={"HOME": str(tmp_path)})
            assert result.returncode == 0, result.stderr

        link = kinematics / "inversed_corexy.py"
        assert link.readlink() == PROJECT_ROOT / "klipper" / "inversed_corexy.py"


@attrs.frozen
class MachineHost:
    """Klipper 導入直後のホスト（printer.cfg は KIAUH が置く stub）."""

    root: Path

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def printer_cfg(self) -> Path:
        return self.root / "printer_data" / "config" / "printer.cfg"

    @property
    def serial(self) -> Path:
        return self.root / "serial" / "usb-Klipper_rp2040_E66138935F2E2B2C-if00"

    def env(self) -> dict[str, str]:
        return {
            "HOME": str(self.root),
            "PCBASM_CONFIG_DIR": str(self.config),
            "SERIAL_BY_ID_DIR": str(self.serial.parent),
        }

    def backups(self) -> list[Path]:
        return sorted(self.printer_cfg.parent.glob("printer.cfg.bak.*"))


@pytest.fixture
def machine_host(tmp_path: Path) -> MachineHost:
    machine = MachineHost(tmp_path)
    machine.printer_cfg.parent.mkdir(parents=True)
    machine.printer_cfg.write_text(
        "[mcu]\nserial: /dev/null\n[printer]\nkinematics: none\n"
    )
    machine.serial.parent.mkdir()
    machine.serial.touch()
    return machine


class TestMachine:
    """`install.sh machine`: 機種のテンプレートから設定を置き、置いた設定は再実行で変えない."""

    def test_places_the_chosen_template(self, machine_host: MachineHost):
        # 機種は名前順に並ぶ（1. kurousagi、2. usaremino、3. usaremino_pnp）
        result = run(INSTALL, "machine", env=machine_host.env(), stdin="2\n")

        assert result.returncode == 0, result.stderr
        template = TEMPLATES / "usaremino"
        assert (machine_host.config / "machine.toml").read_text() == (
            template / "machine.toml"
        ).read_text()
        assert machine_host.printer_cfg.read_text() == (
            template / "printer.cfg"
        ).read_text().replace(
            "serial: /dev/serial/by-id/<your-mcu-id>", f"serial: {machine_host.serial}"
        )
        assert (
            machine_host.config / "printer.cfg"
        ).readlink() == machine_host.printer_cfg
        assert [path.read_text() for path in machine_host.backups()] == [
            "[mcu]\nserial: /dev/null\n[printer]\nkinematics: none\n"
        ]

    def test_places_the_pnp_template(self, machine_host: MachineHost):
        """部品実装機の雛形は共通の節と空の [pnp] を置き、printer.cfg は同じ筐体のものを使う."""
        result = run(INSTALL, "machine", env=machine_host.env(), stdin="3\n")

        assert result.returncode == 0, result.stderr
        machine_toml = (machine_host.config / "machine.toml").read_text()
        assert (
            machine_toml == (TEMPLATES / "usaremino_pnp" / "machine.toml").read_text()
        )
        assert "[pnp]" in machine_toml and "[solder" not in machine_toml
        assert (
            "[printer]\nkinematics: cartesian" in machine_host.printer_cfg.read_text()
        )

    def test_rerun_changes_nothing(self, machine_host: MachineHost):
        run(INSTALL, "machine", env=machine_host.env(), stdin="2\n")
        machine_toml = (machine_host.config / "machine.toml").read_text()
        printer_cfg = machine_host.printer_cfg.read_text()

        result = run(INSTALL, "machine", env=machine_host.env())

        assert result.returncode == 0, result.stderr
        assert (machine_host.config / "machine.toml").read_text() == machine_toml
        assert machine_host.printer_cfg.read_text() == printer_cfg
        assert len(machine_host.backups()) == 1

    def test_keeps_a_calibrated_printer_cfg(self, machine_host: MachineHost):
        """SAVE_CONFIG の較正値を持つ printer.cfg は、machine.toml が無くても上書きしない."""
        machine_host.printer_cfg.write_text("[printer]\nkinematics: cartesian\n")

        result = run(INSTALL, "machine", env=machine_host.env(), stdin="1\n")

        assert result.returncode == 0, result.stderr
        assert (
            machine_host.printer_cfg.read_text() == "[printer]\nkinematics: cartesian\n"
        )
        assert (machine_host.config / "machine.toml").read_text() == (
            TEMPLATES / "kurousagi" / "machine.toml"
        ).read_text()
        assert machine_host.backups() == []


class TestWebServiceShim:
    """旧版の自己更新が呼ぶ `scripts/web-service.sh` は install.sh に委譲する."""

    @pytest.mark.parametrize("target", ["api", "ui"])
    def test_render_draws_the_same_unit(self, target: str):
        result = run(SHIM, "render", target)

        assert result.returncode == 0, result.stderr
        assert result.stdout == expected_unit(target)

    def test_install_installs_through_install_sh(self, host: Host):
        result = run(SHIM, "install", "all", env=host.env())

        assert result.returncode == 0, result.stderr
        assert sorted(host.files()) == [
            "pcbasm-api.service",
            "pcbasm-ui.service",
            "pcbasm-update",
        ]
