"""`web-service.sh` が生成する systemd unit と対象解決の契約テスト.

このスクリプトは実機の systemd を書き換えるため、テストは**実機の systemd に一切触れない**
形に限る。スクリプト側は「unit テキストを組む」「対象を解決する」を systemctl を呼ばない
関数に分けてあり、`SYSTEMD_UNIT_DIR`（unit の設置先）と `SUDO`（特権コマンドの実行手段）を
env で差し替えられる。

seam の使い分け:

- `SUDO` に `exec "$@"` するだけの no-op ラッパ（`stub_sudo`）を渡すと `install` / `rm` が
  **実際に走る**ので、`SYSTEMD_UNIT_DIR`（一時ディレクトリ）上の実結果を観測できる。
  `systemctl` は PATH 先頭のスタブ（`stub_systemctl`）が受けるため systemd には触らない。
  ファイルシステムを見るテストはこちらを使う。
- `SUDO=echo`（既定）はコマンド列だけを観測する。`rm` も `install` も実行されないため、
  「削除されたか」を見る assert には使えない（空振りする）。

`tests/test_claude_hooks.py` / `tests/test_makefile_fake_targets.py` と同じくミラーレイアウト外の
トップレベルテスト（`src/` に対応物が無い成果物のため）。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from pcbasm.utils import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "web-service.sh"
LEGACY_UNIT = "pcbasm-webui.service"


def script_env(
    unit_dir: Path,
    *,
    sudo: str = "echo",
    stub_bin: Path | None = None,
    tmpdir: Path | None = None,
    path: str | None = None,
) -> dict[str, str]:
    """スクリプトに渡す環境変数を組む.

    Args:
        unit_dir: `SYSTEMD_UNIT_DIR` に渡す一時ディレクトリ
        sudo: `SUDO` の値（既定の `echo` はコマンド列の観測用）
        stub_bin: PATH の先頭に足すディレクトリ（スタブの設置先）
        tmpdir: `TMPDIR`（`mktemp` の出力先を観測したいとき）
        path: PATH を丸ごと置き換える値（`stub_bin` と併用しない）

    Returns:
        `subprocess.run` に渡す env
    """
    env = os.environ | {"SYSTEMD_UNIT_DIR": str(unit_dir), "SUDO": sudo}
    if path is not None:
        env["PATH"] = path
    if stub_bin is not None:
        env["PATH"] = f"{stub_bin}:{env['PATH']}"
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    return env


def run_snippet(
    snippet: str,
    unit_dir: Path,
    *,
    sudo: str = "echo",
    stub_bin: Path | None = None,
    tmpdir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """スクリプトを source して関数を呼ぶ.

    `$0` が `bash` になるため末尾の dispatch は走らない（source されたときは
    `main` を呼ばない設計）。

    Args:
        snippet: source 後に評価する bash コード
        unit_dir: `SYSTEMD_UNIT_DIR` に渡す一時ディレクトリ
        sudo: `SUDO` の値
        stub_bin: PATH の先頭に足すディレクトリ
        tmpdir: `TMPDIR`

    Returns:
        実行結果（`check` はしない。戻り値を検証するテストがあるため）
    """
    return subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"\n{snippet}'],
        capture_output=True,
        text=True,
        env=script_env(unit_dir, sudo=sudo, stub_bin=stub_bin, tmpdir=tmpdir),
    )


def run_script(
    args: list[str],
    unit_dir: Path,
    stub_bin: Path | None = None,
    *,
    sudo: str = "echo",
    path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """スクリプトを実プロセスとして起動する（`main` のディスパッチを含む経路）.

    Args:
        args: スクリプトへの引数
        unit_dir: `SYSTEMD_UNIT_DIR` に渡す一時ディレクトリ
        stub_bin: PATH の先頭に置くディレクトリ（`systemctl` スタブの設置先）
        sudo: `SUDO` の値
        path: PATH を丸ごと置き換える値

    Returns:
        実行結果
    """
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=script_env(unit_dir, sudo=sudo, stub_bin=stub_bin, path=path),
    )


def stub_systemctl(bin_dir: Path, failing_units: tuple[str, ...] = ()) -> Path:
    """`systemctl` スタブを置く（実機の systemd を絶対に呼ばないため）.

    引数をそのまま stdout に出し、`failing_units` のどれかが引数に含まれていれば
    `systemctl status` の「inactive」と同じ 3 で終了する。

    Args:
        bin_dir: スタブを置くディレクトリ（PATH の先頭に入れる）
        failing_units: 非 0 で終了させる unit 名

    Returns:
        `bin_dir`（`run_script` の `stub_bin` にそのまま渡せる）
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    patterns = "\n".join(f'    *" {unit} "*) exit 3 ;;' for unit in failing_units)
    script = bin_dir / "systemctl"
    script.write_text(
        "#!/bin/bash\n"
        'echo "systemctl $*"\n'
        'case " $* " in\n'
        f"{patterns}\n"
        "esac\n"
        "exit 0\n"
    )
    script.chmod(0o755)
    return bin_dir


def stub_sudo(bin_dir: Path) -> Path:
    """`SUDO` に渡す no-op ラッパ（引数をそのまま `exec` するだけ）を置く.

    `sudo` という名前で置くので、`require_command sudo` も PATH 先頭のこれで満たされ、
    実機の `sudo` は一度も呼ばれない。`SUDO=echo` と違い `install` / `rm` が実際に走るため、
    unit ディレクトリ（一時ディレクトリ）上の実結果を観測できる。特権が必要な操作は
    `systemctl` だけで、それは `stub_systemctl` が受ける。

    Args:
        bin_dir: スタブを置くディレクトリ

    Returns:
        置いたラッパの絶対パス（`sudo=` にそのまま渡せる）
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "sudo"
    script.write_text('#!/bin/bash\nexec "$@"\n')
    script.chmod(0o755)
    return script


def privileged_seam(
    tmp_path: Path, failing_units: tuple[str, ...] = ()
) -> tuple[str, Path]:
    """特権操作を「本当に実行する」seam を用意する.

    Args:
        tmp_path: テストの一時ディレクトリ
        failing_units: `systemctl` スタブを非 0 で終わらせる unit 名

    Returns:
        `(sudo, stub_bin)`。`run_script` / `run_snippet` にそのまま渡す
    """
    bin_dir = tmp_path / "bin"
    stub_systemctl(bin_dir, failing_units)
    return str(stub_sudo(bin_dir)), bin_dir


def render_unit(target: str, unit_dir: Path) -> str:
    """`render_unit` の出力を得る.

    Args:
        target: `api` または `ui`
        unit_dir: `SYSTEMD_UNIT_DIR` に渡す一時ディレクトリ

    Returns:
        unit ファイルのテキスト
    """
    completed = run_snippet(f"render_unit {target}", unit_dir)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


class TestRenderUnit:
    """生成される unit テキスト."""

    @pytest.mark.parametrize("target", ("api", "ui"))
    def test_execstart_runs_the_make_target_of_the_same_name(
        self, target: str, tmp_path: Path
    ):
        unit = render_unit(target, tmp_path)
        execstart = [
            line for line in unit.splitlines() if line.startswith("ExecStart=")
        ]
        assert len(execstart) == 1
        assert re.fullmatch(rf"ExecStart=/\S*/make {target}", execstart[0]), execstart

    @pytest.mark.parametrize("target", ("api", "ui"))
    def test_no_ordering_dependency_between_the_two_services(
        self, target: str, tmp_path: Path
    ):
        """`After=pcbasm-api.service` を付けない回帰.

        付けると同居機で backend の起動失敗が frontend まで止める。frontend は backend が
        落ちていても起動でき、問い合わせが 503 になるだけで復帰できる。
        """
        unit = render_unit(target, tmp_path)
        ordering = [
            line
            for line in unit.splitlines()
            if line.startswith(("After=", "Before=", "Requires=", "BindsTo="))
        ]
        assert ordering == ["After=network-online.target"]
        assert "pcbasm-api.service" not in unit
        assert "avahi" not in unit

    @pytest.mark.parametrize(
        ("target", "description"),
        (
            ("api", "Description=PCB Assembly backend WebAPI"),
            ("ui", "Description=PCB Assembly UI frontend"),
        ),
    )
    def test_description_identifies_the_process(
        self, target: str, description: str, tmp_path: Path
    ):
        assert description in render_unit(target, tmp_path)

    @pytest.mark.parametrize("target", ("api", "ui"))
    def test_unit_is_enabled_for_boot(self, target: str, tmp_path: Path):
        assert "WantedBy=multi-user.target" in render_unit(target, tmp_path)


class TestTargetResolution:
    """`[api|ui|all]` 引数の解決."""

    @pytest.mark.parametrize(
        ("argument", "resolved"),
        (
            ("", "api"),  # 既定は backend（機体ごとに置くのが最多数）
            ("api", "api"),
            ("ui", "ui"),
            ("all", "api ui"),  # 順序も契約（api → ui）
        ),
    )
    def test_resolves(self, argument: str, resolved: str, tmp_path: Path):
        completed = run_snippet(f'resolve_targets "{argument}"', tmp_path)
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.split() == resolved.split()

    @pytest.mark.parametrize("argument", ("webui", "API", "api ui", "-x"))
    def test_rejects_unknown_target(self, argument: str, tmp_path: Path):
        completed = run_snippet(f'resolve_targets "{argument}"', tmp_path)
        assert completed.returncode != 0

    @pytest.mark.parametrize("args", (["status", "webui"], ["install", "webui"]))
    def test_script_exits_nonzero_with_usage(self, args: list[str], tmp_path: Path):
        """不正な対象は systemd に触る前に落ちる（`install` でも副作用が無い）."""
        completed = run_script(args, tmp_path)
        assert completed.returncode != 0
        assert "Usage:" in completed.stderr
        assert not list(tmp_path.iterdir())

    def test_script_exits_nonzero_on_unknown_command(self, tmp_path: Path):
        completed = run_script(["reload"], tmp_path)
        assert completed.returncode != 0
        assert "Usage:" in completed.stderr


def write_legacy_unit(unit_dir: Path) -> Path:
    """未移行の機体を模して旧 unit を置く.

    Args:
        unit_dir: `SYSTEMD_UNIT_DIR` に渡す一時ディレクトリ

    Returns:
        置いた旧 unit のパス
    """
    legacy = unit_dir / LEGACY_UNIT
    legacy.write_text("[Service]\nExecStart=/usr/bin/make webui\n")
    return legacy


class TestInstall:
    """`install_service` のファイルシステム上の結果（`SUDO` は no-op ラッパ）."""

    @pytest.mark.parametrize("target", ("api", "ui"))
    def test_rendered_unit_is_installed_at_the_unit_path(
        self, target: str, tmp_path: Path
    ):
        sudo, stub = privileged_seam(tmp_path)

        completed = run_snippet(
            f"install_service {target}", tmp_path, sudo=sudo, stub_bin=stub
        )

        assert completed.returncode == 0, completed.stderr
        installed = tmp_path / f"pcbasm-{target}.service"
        assert installed.read_text() == render_unit(target, tmp_path)
        assert installed.stat().st_mode & 0o777 == 0o644
        assert f"systemctl enable pcbasm-{target}.service" in completed.stdout

    def test_daemon_reload_precedes_enable(self, tmp_path: Path):
        """`daemon-reload` を欠くと `enable` が古い unit 定義を掴む."""
        sudo, stub = privileged_seam(tmp_path)

        completed = run_snippet(
            "install_service api", tmp_path, sudo=sudo, stub_bin=stub
        )

        assert completed.returncode == 0, completed.stderr
        lines = completed.stdout.splitlines()
        assert "systemctl daemon-reload" in lines, completed.stdout
        assert "systemctl enable pcbasm-api.service" in lines, completed.stdout
        assert lines.index("systemctl daemon-reload") < lines.index(
            "systemctl enable pcbasm-api.service"
        ), completed.stdout

    @pytest.mark.parametrize("target", ("api", "ui"))
    def test_install_restarts_the_service(self, target: str, tmp_path: Path):
        """Restart を欠くと「unit は設置・enable されたのに新 backend が起動しない」."""
        sudo, stub = privileged_seam(tmp_path)

        completed = run_snippet(
            f"install_service {target}", tmp_path, sudo=sudo, stub_bin=stub
        )

        assert completed.returncode == 0, completed.stderr
        assert f"systemctl restart pcbasm-{target}.service" in completed.stdout

    def test_install_api_purges_the_legacy_unit(self, tmp_path: Path):
        """単体の `install api`（最多数の運用）でも旧 unit を掃除する."""
        sudo, stub = privileged_seam(tmp_path)
        legacy = write_legacy_unit(tmp_path)

        completed = run_snippet(
            "install_service api", tmp_path, sudo=sudo, stub_bin=stub
        )

        assert completed.returncode == 0, completed.stderr
        assert not legacy.exists(), "旧 unit が削除されていない"
        assert f"systemctl disable --now {LEGACY_UNIT}" in completed.stdout

    def test_install_ui_keeps_the_legacy_unit_and_warns(self, tmp_path: Path):
        """旧 unit は backend 本体。`install ui` で消すと稼働中の backend が消える."""
        sudo, stub = privileged_seam(tmp_path)
        legacy = write_legacy_unit(tmp_path)

        completed = run_snippet(
            "install_service ui", tmp_path, sudo=sudo, stub_bin=stub
        )

        assert completed.returncode == 0, completed.stderr
        assert legacy.exists(), "旧 backend unit が削除された"
        assert f"disable --now {LEGACY_UNIT}" not in completed.stdout
        assert LEGACY_UNIT in completed.stderr
        assert "install api" in completed.stderr

    def test_install_ui_does_not_warn_without_the_legacy_unit(self, tmp_path: Path):
        """旧 unit を持ったことのない frontend 専用機に誤警告を出さない."""
        sudo, stub = privileged_seam(tmp_path)

        completed = run_snippet(
            "install_service ui", tmp_path, sudo=sudo, stub_bin=stub
        )

        assert completed.returncode == 0, completed.stderr
        assert LEGACY_UNIT not in completed.stderr, completed.stderr

    def test_temporary_unit_file_is_removed_when_the_privileged_install_fails(
        self, tmp_path: Path
    ):
        """特権 install が失敗しても `mktemp` したファイルを残さない（EXIT trap）."""
        mktemp_dir = tmp_path / "mktemp"
        mktemp_dir.mkdir()

        completed = run_snippet(
            "install_service api", tmp_path, sudo="false", tmpdir=mktemp_dir
        )

        assert completed.returncode != 0
        assert not list(mktemp_dir.iterdir()), "一時 unit ファイルが残っている"


class TestRemove:
    """`remove_service` のファイルシステム上の結果."""

    def test_remove_api_purges_the_legacy_unit_on_an_unmigrated_machine(
        self, tmp_path: Path
    ):
        """新 unit が無くても旧 unit を撤去する（install と対称）.

        掃除しないと「登録されていません」で抜けて、`make webui` を失って restart ループに入る旧 unit が
        enabled のまま残る。
        """
        sudo, stub = privileged_seam(tmp_path)
        legacy = write_legacy_unit(tmp_path)

        completed = run_snippet(
            "remove_service api", tmp_path, sudo=sudo, stub_bin=stub
        )

        assert completed.returncode == 0, completed.stderr
        assert not legacy.exists(), "旧 unit が削除されていない"
        assert f"systemctl disable --now {LEGACY_UNIT}" in completed.stdout

    def test_remove_ui_keeps_the_legacy_unit(self, tmp_path: Path):
        sudo, stub = privileged_seam(tmp_path)
        legacy = write_legacy_unit(tmp_path)

        completed = run_snippet("remove_service ui", tmp_path, sudo=sudo, stub_bin=stub)

        assert completed.returncode == 0, completed.stderr
        assert legacy.exists(), "旧 backend unit が削除された"
        assert f"disable --now {LEGACY_UNIT}" not in completed.stdout

    def test_remove_deletes_the_unit_and_reloads(self, tmp_path: Path):
        sudo, stub = privileged_seam(tmp_path)
        unit = tmp_path / "pcbasm-ui.service"
        unit.write_text("[Service]\n")

        completed = run_snippet("remove_service ui", tmp_path, sudo=sudo, stub_bin=stub)

        assert completed.returncode == 0, completed.stderr
        assert not unit.exists(), "unit ファイルが残っている"
        assert "systemctl disable --now pcbasm-ui.service" in completed.stdout
        assert "systemctl daemon-reload" in completed.stdout


class TestMainDispatch:
    """`main` 経路（プロセス起動）の `install` / `remove` 成功パス.

    実機の移行チェックリストが叩く命令。関数を直呼びするテストだけでは `main` の
    `install)` / `remove)` 分岐が壊れていても気付けない。
    """

    @pytest.mark.parametrize("args", (["install"], ["install", "api"]))
    def test_install_writes_and_enables_the_unit(self, args: list[str], tmp_path: Path):
        """対象を省略した `install` は `install api` と同じ（既定は backend）."""
        sudo, stub = privileged_seam(tmp_path)

        completed = run_script(args, tmp_path, stub_bin=stub, sudo=sudo)

        assert completed.returncode == 0, completed.stderr
        installed = tmp_path / "pcbasm-api.service"
        assert installed.read_text() == render_unit("api", tmp_path)
        assert "systemctl enable pcbasm-api.service" in completed.stdout
        assert "systemctl restart pcbasm-api.service" in completed.stdout

    def test_install_all_registers_both_targets(self, tmp_path: Path):
        sudo, stub = privileged_seam(tmp_path)

        completed = run_script(["install", "all"], tmp_path, stub_bin=stub, sudo=sudo)

        assert completed.returncode == 0, completed.stderr
        for target in ("api", "ui"):
            installed = tmp_path / f"pcbasm-{target}.service"
            assert installed.read_text() == render_unit(target, tmp_path)
            assert f"systemctl enable pcbasm-{target}.service" in completed.stdout
            assert f"systemctl restart pcbasm-{target}.service" in completed.stdout

    def test_install_all_purges_the_legacy_unit_once(self, tmp_path: Path):
        """同居機の移行（`install all`）: 旧 unit が消えて新 2 unit に置き換わる."""
        sudo, stub = privileged_seam(tmp_path)
        legacy = write_legacy_unit(tmp_path)

        completed = run_script(["install", "all"], tmp_path, stub_bin=stub, sudo=sudo)

        assert completed.returncode == 0, completed.stderr
        assert not legacy.exists(), "旧 unit が削除されていない"
        assert LEGACY_UNIT not in completed.stderr, "api 側で掃除済みなのに警告が出た"

    def test_remove_deletes_the_installed_unit(self, tmp_path: Path):
        sudo, stub = privileged_seam(tmp_path)
        unit = tmp_path / "pcbasm-api.service"
        unit.write_text("[Service]\n")

        completed = run_script(["remove", "api"], tmp_path, stub_bin=stub, sudo=sudo)

        assert completed.returncode == 0, completed.stderr
        assert not unit.exists(), "unit ファイルが残っている"
        assert "systemctl disable --now pcbasm-api.service" in completed.stdout

    def test_remove_all_deletes_both_units_and_the_legacy_one(self, tmp_path: Path):
        sudo, stub = privileged_seam(tmp_path)
        legacy = write_legacy_unit(tmp_path)
        units = [tmp_path / f"pcbasm-{target}.service" for target in ("api", "ui")]
        for unit in units:
            unit.write_text("[Service]\n")

        completed = run_script(["remove", "all"], tmp_path, stub_bin=stub, sudo=sudo)

        assert completed.returncode == 0, completed.stderr
        assert not legacy.exists(), "旧 unit が削除されていない"
        assert [unit for unit in units if unit.exists()] == []


class TestRequiredTools:
    """`main` が特権コマンドの存在を先に確かめる（`require_privileged_tools`）.

    非 root チェック（`EUID=0`）はテストから再現できないため未検証。`systemctl` が PATH に
    無いときに明確なエラーで終わることで、`main` からの呼び出しの消失は検出できる。
    """

    def test_install_fails_clearly_when_systemctl_is_missing(self, tmp_path: Path):
        bin_dir = tmp_path / "bin"
        # PROJECT_ROOT の算出に使う dirname だけ通し、systemctl は置かない
        stub_sudo(bin_dir)
        (bin_dir / "dirname").symlink_to("/usr/bin/dirname")
        unit_dir = tmp_path / "units"
        unit_dir.mkdir()

        completed = run_script(["install", "api"], unit_dir, path=str(bin_dir))

        assert completed.returncode != 0
        assert "systemctl" in completed.stderr
        assert not list(unit_dir.iterdir())


class TestStatusAll:
    """`status all` は全対象を必ず表示する.

    片方が落ちている同居機を診断するための経路。1 本目の `systemctl status` が非 0 （inactive=3 /
    未登録=4）でも打ち切らない。
    """

    def test_shows_every_target_even_when_the_first_is_unhealthy(self, tmp_path: Path):
        stub = stub_systemctl(tmp_path / "bin", failing_units=("pcbasm-api.service",))

        completed = run_script(["status", "all"], tmp_path, stub_bin=stub)

        assert "status --no-pager pcbasm-api.service" in completed.stdout
        assert "status --no-pager pcbasm-ui.service" in completed.stdout
        assert completed.returncode != 0

    def test_exits_zero_when_every_target_is_healthy(self, tmp_path: Path):
        stub = stub_systemctl(tmp_path / "bin")

        completed = run_script(["status", "all"], tmp_path, stub_bin=stub)

        assert completed.returncode == 0, completed.stderr
        assert "status --no-pager pcbasm-api.service" in completed.stdout
        assert "status --no-pager pcbasm-ui.service" in completed.stdout


class TestControlMissingUnit:
    """`start|stop|restart` の未登録の扱い.

    `all` では片方だけ入っている構成が正常なので警告して次へ進む（同じループ内の
    `remove` と同じ扱い）。単体指定は打ち間違い / install 忘れなのでエラー終了する。
    """

    @pytest.mark.parametrize("command", ("start", "stop", "restart"))
    def test_all_skips_the_unregistered_target_and_continues(
        self, command: str, tmp_path: Path
    ):
        stub = stub_systemctl(tmp_path / "bin")
        (tmp_path / "pcbasm-ui.service").write_text("[Service]\n")

        completed = run_script([command, "all"], tmp_path, stub_bin=stub)

        assert completed.returncode == 0, completed.stderr
        assert "pcbasm-api.service" in completed.stderr
        assert f"systemctl {command} pcbasm-ui.service" in completed.stdout

    def test_single_target_errors_when_unregistered(self, tmp_path: Path):
        stub = stub_systemctl(tmp_path / "bin")

        completed = run_script(["start", "api"], tmp_path, stub_bin=stub)

        assert completed.returncode != 0
        assert "pcbasm-api.service" in completed.stderr
        assert "systemctl start" not in completed.stdout


class TestLegacyUnitPurge:
    """旧 `pcbasm-webui.service` の掃除そのもの."""

    def test_purge_disables_and_deletes_the_legacy_unit(self, tmp_path: Path):
        sudo, stub = privileged_seam(tmp_path)
        legacy = write_legacy_unit(tmp_path)

        completed = run_snippet("purge_legacy_unit", tmp_path, sudo=sudo, stub_bin=stub)

        assert completed.returncode == 0, completed.stderr
        assert not legacy.exists(), "旧 unit が削除されていない"
        assert f"systemctl disable --now {LEGACY_UNIT}" in completed.stdout
        assert "systemctl daemon-reload" in completed.stdout

    def test_purge_is_a_noop_without_the_legacy_unit(self, tmp_path: Path):
        sudo, stub = privileged_seam(tmp_path)

        completed = run_snippet("purge_legacy_unit", tmp_path, sudo=sudo, stub_bin=stub)

        assert completed.returncode == 0, completed.stderr
        assert "systemctl" not in completed.stdout


class TestUnitNames:
    """Unit 名と設置先."""

    @pytest.mark.parametrize(
        ("target", "name"), (("api", "pcbasm-api.service"), ("ui", "pcbasm-ui.service"))
    )
    def test_service_name(self, target: str, name: str, tmp_path: Path):
        completed = run_snippet(f"service_name {target}", tmp_path)
        assert completed.stdout.strip() == name

    def test_unit_path_is_under_the_systemd_unit_dir(self, tmp_path: Path):
        completed = run_snippet("unit_path api", tmp_path)
        assert completed.stdout.strip() == str(tmp_path / "pcbasm-api.service")
