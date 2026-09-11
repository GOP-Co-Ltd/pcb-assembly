"""`scripts/install-update-sudoers.sh` が生成・設置する sudoers 断片の契約テスト.

計画書「4. `scripts/web-service.sh` と sudoers」が契約。WebUI からの自己更新で唯一
root 権限を使うのが `sudo -n /usr/bin/systemctl restart --no-block <units>` であり、
**その 3 変種の固定 argv だけ**を NOPASSWD で許す。ここが緩むと「LAN に居る者が任意
コードを root で実行できる」に直結するため、`*` を 1 文字も含まないことを最重要の
回帰テストとして固定する。

`tests/test_web_service_script.py` / `tests/test_claude_hooks.py` と同じくミラーレイアウト外の
トップレベルテスト（`src/` に対応物が無いリポジトリ資産のため）。

seam は `web-service.sh` の作法を踏襲する:

- `SUDOERS_DIR`: 設置先（テストでは一時ディレクトリ）
- `VISUDO`: 構文検査コマンド（テストでは引数を echo するスタブ。非 0 にもできる）
- `SUDO`: 特権コマンドの実行手段。`exec "$@"` する no-op ラッパを渡すと `install` /
  `rm` が実際に走るので一時ディレクトリ上の実結果を観測できる。`sudo -n -l` の
  自己検査だけは実 sudo を呼ばずに許可済みの listing を返す

実機の `/etc/sudoers.d` には一切触れない。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from pcbasm.utils import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "scripts" / "install-update-sudoers.sh"

# 設置される sudoers 断片のファイル名。sudo はドットを含む名前を読み飛ばすため
# 拡張子を付けない（計画書「4.」）。
SUDOERS_FILE_NAME = "pcbasm-update"

SYSTEMCTL = "/usr/bin/systemctl"

# NOPASSWD で許す固定 argv。順序も契約（コードは常に CANONICAL_ORDER = api → ui で組む）。
ALLOWED_COMMANDS = (
    f"{SYSTEMCTL} restart --no-block pcbasm-api.service",
    f"{SYSTEMCTL} restart --no-block pcbasm-ui.service",
    f"{SYSTEMCTL} restart --no-block pcbasm-api.service pcbasm-ui.service",
)


def stub_visudo(bin_dir: Path, *, exit_code: int = 0) -> str:
    """`VISUDO` に渡すスタブを置く（実 visudo を呼ばないため）.

    Args:
        bin_dir: スタブを置くディレクトリ
        exit_code: 終了コード（1 にすると「構文エラー」を模す）

    Returns:
        置いたスタブの絶対パス（`VISUDO=` にそのまま渡せる）
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "visudo"
    script.write_text(f'#!/bin/bash\necho "visudo $*"\nexit {exit_code}\n')
    script.chmod(0o755)
    return str(script)


def stub_sudo(bin_dir: Path) -> str:
    """`SUDO` に渡す no-op ラッパを置く.

    `install` / `rm` は `exec "$@"` でそのまま実行するので、一時ディレクトリ上の実結果を
    観測できる。`sudo -n -l`（設置後の自己検査）だけは実 sudo を呼ばず、許可済みの
    listing を出して 0 で返す。

    Args:
        bin_dir: スタブを置くディレクトリ

    Returns:
        置いたラッパの絶対パス（`SUDO=` にそのまま渡せる）
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    listing = "\n".join(f"    (root : root) NOPASSWD: {c}" for c in ALLOWED_COMMANDS)
    script = bin_dir / "sudo"
    script.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "-n" ]; then\n'
        f'  printf "%s\\n" "{listing}"\n'
        "  exit 0\n"
        "fi\n"
        'exec "$@"\n'
    )
    script.chmod(0o755)
    return str(script)


def script_env(
    sudoers_dir: Path,
    *,
    sudo: str = "echo",
    visudo: str = "true",
    stub_bin: Path | None = None,
) -> dict[str, str]:
    """スクリプトに渡す環境変数を組む.

    Args:
        sudoers_dir: `SUDOERS_DIR` に渡す一時ディレクトリ
        sudo: `SUDO` の値
        visudo: `VISUDO` の値
        stub_bin: PATH の先頭に足すディレクトリ

    Returns:
        `subprocess.run` に渡す env
    """
    env = os.environ | {
        "SUDOERS_DIR": str(sudoers_dir),
        "SUDO": sudo,
        "VISUDO": visudo,
    }
    if stub_bin is not None:
        env["PATH"] = f"{stub_bin}:{env['PATH']}"
    return env


def run_script(
    args: list[str],
    sudoers_dir: Path,
    *,
    sudo: str = "echo",
    visudo: str = "true",
    stub_bin: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """スクリプトを実プロセスとして起動する（`main` のディスパッチを含む経路）."""
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=script_env(sudoers_dir, sudo=sudo, visudo=visudo, stub_bin=stub_bin),
    )


def run_snippet(
    snippet: str,
    sudoers_dir: Path,
    *,
    sudo: str = "echo",
    visudo: str = "true",
    stub_bin: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """スクリプトを source して関数を呼ぶ（`$0` が `bash` になり dispatch は走らない）."""
    return subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"\n{snippet}'],
        capture_output=True,
        text=True,
        env=script_env(sudoers_dir, sudo=sudo, visudo=visudo, stub_bin=stub_bin),
    )


def rendered(sudoers_dir: Path) -> str:
    """`render_sudoers` の出力を得る（純関数。stdout に吐くだけ）."""
    completed = run_snippet("render_sudoers", sudoers_dir)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def alias_commands(text: str) -> list[str]:
    """`Cmnd_Alias` 定義に並ぶコマンドを 1 件ずつ取り出す.

    行継続（末尾 `\\`）と区切りのカンマ、インデントを除いた argv 文字列を返す。 インデント幅ではなく **argv そのもの**
    を固定するための正規化。
    """
    body: list[str] = []
    collecting = False
    for line in text.splitlines():
        if line.startswith("Cmnd_Alias"):
            collecting = True
            body.append(line.split("=", 1)[1])
            continue
        if collecting:
            body.append(line)
        if collecting and not line.rstrip().endswith("\\"):
            break
    joined = " ".join(part.rstrip().removesuffix("\\") for part in body)
    return [command.strip() for command in joined.split(",") if command.strip()]


class TestRenderSudoers:
    """生成される sudoers 断片のテキスト（設置しない純関数）."""

    def test_allows_exactly_the_three_restart_invocations(self, tmp_path: Path):
        """許可する argv は api 単体 / ui 単体 / 同居機の 3 変種だけ（順序も契約）."""
        assert alias_commands(rendered(tmp_path)) == list(ALLOWED_COMMANDS)

    def test_contains_no_wildcard(self, tmp_path: Path):
        """`*` は 1 文字も含めない（最重要のセキュリティ回帰）.

        sudo の glob は `/` も食うため、引数にワイルドカードを 1 つ入れるだけで 「任意のコマンドを root
        で実行」に悪化しうる。
        """
        assert "*" not in rendered(tmp_path)

    @pytest.mark.parametrize("forbidden", ("sh -c", "bash", "systemd-run"))
    def test_does_not_grant_a_shell(self, forbidden: str, tmp_path: Path):
        """シェルや `systemd-run` を許すと固定 argv の制約が意味を失う（計画書の却下案）."""
        assert forbidden not in rendered(tmp_path)

    def test_does_not_grant_anything_under_the_repository(self, tmp_path: Path):
        """リポジトリ追跡下のスクリプトに root を与えない.

        この機能自身がリポジトリを書き換える経路なので、リポジトリ配下のパスへ NOPASSWD
        を与えると自己更新が特権昇格に化ける（計画書の却下案「sudoers で `web-service.sh` を許可」）。
        """
        assert str(PROJECT_ROOT) not in rendered(tmp_path)

    def test_grants_the_alias_to_the_current_user_as_root(self, tmp_path: Path):
        """`<user> ALL=(root:root) NOPASSWD: PCBASM_UPDATE_RESTART` 行."""
        user = subprocess.run(
            ["id", "-un"], capture_output=True, text=True, check=True
        ).stdout.strip()

        grant = [
            line for line in rendered(tmp_path).splitlines() if "NOPASSWD:" in line
        ]

        assert len(grant) == 1, rendered(tmp_path)
        assert grant[0].startswith(user)
        assert "(root:root)" in grant[0]
        assert "PCBASM_UPDATE_RESTART" in grant[0]


class TestInstallSudoers:
    """`install` / `remove` のファイルシステム上の結果（`SUDO` は no-op ラッパ）."""

    def test_install_writes_the_fragment_with_read_only_permissions(
        self, tmp_path: Path
    ):
        sudoers_dir = tmp_path / "sudoers.d"
        sudoers_dir.mkdir()
        bin_dir = tmp_path / "bin"

        completed = run_script(
            ["install"],
            sudoers_dir,
            sudo=stub_sudo(bin_dir),
            visudo=stub_visudo(bin_dir),
            stub_bin=bin_dir,
        )

        assert completed.returncode == 0, completed.stderr
        installed = sudoers_dir / SUDOERS_FILE_NAME
        assert installed.read_text() == rendered(sudoers_dir)
        assert installed.stat().st_mode & 0o777 == 0o440

    def test_show_matches_what_install_writes(self, tmp_path: Path):
        """`show` で事前に確認した内容と設置される内容が食い違わない."""
        sudoers_dir = tmp_path / "sudoers.d"
        sudoers_dir.mkdir()
        bin_dir = tmp_path / "bin"
        shown = run_script(["show"], sudoers_dir)

        run_script(
            ["install"],
            sudoers_dir,
            sudo=stub_sudo(bin_dir),
            visudo=stub_visudo(bin_dir),
            stub_bin=bin_dir,
        )

        assert shown.returncode == 0, shown.stderr
        assert (sudoers_dir / SUDOERS_FILE_NAME).read_text() == shown.stdout

    def test_nothing_is_installed_when_visudo_rejects_the_fragment(
        self, tmp_path: Path
    ):
        """壊れた sudoers を置くと `sudo` 全体が死に、復旧手段まで失う（計画書「4.」）."""
        sudoers_dir = tmp_path / "sudoers.d"
        sudoers_dir.mkdir()
        bin_dir = tmp_path / "bin"

        completed = run_script(
            ["install"],
            sudoers_dir,
            sudo=stub_sudo(bin_dir),
            visudo=stub_visudo(bin_dir, exit_code=1),
            stub_bin=bin_dir,
        )

        assert completed.returncode != 0
        assert not list(sudoers_dir.iterdir()), "検査に落ちたのに設置された"

    def test_install_is_idempotent(self, tmp_path: Path):
        """既設置の機体で再実行しても同じ内容に落ち着く（差分なしなら早期 return）."""
        sudoers_dir = tmp_path / "sudoers.d"
        sudoers_dir.mkdir()
        bin_dir = tmp_path / "bin"
        sudo, visudo = stub_sudo(bin_dir), stub_visudo(bin_dir)

        first = run_script(
            ["install"], sudoers_dir, sudo=sudo, visudo=visudo, stub_bin=bin_dir
        )
        second = run_script(
            ["install"], sudoers_dir, sudo=sudo, visudo=visudo, stub_bin=bin_dir
        )

        assert first.returncode == 0, first.stderr
        assert second.returncode == 0, second.stderr
        assert [path.name for path in sudoers_dir.iterdir()] == [SUDOERS_FILE_NAME]
        assert (sudoers_dir / SUDOERS_FILE_NAME).read_text() == rendered(sudoers_dir)

    def test_remove_deletes_the_fragment(self, tmp_path: Path):
        sudoers_dir = tmp_path / "sudoers.d"
        sudoers_dir.mkdir()
        bin_dir = tmp_path / "bin"
        sudo, visudo = stub_sudo(bin_dir), stub_visudo(bin_dir)
        run_script(["install"], sudoers_dir, sudo=sudo, visudo=visudo, stub_bin=bin_dir)

        completed = run_script(
            ["remove"], sudoers_dir, sudo=sudo, visudo=visudo, stub_bin=bin_dir
        )

        assert completed.returncode == 0, completed.stderr
        assert not (sudoers_dir / SUDOERS_FILE_NAME).exists()

    def test_remove_succeeds_when_nothing_is_installed(self, tmp_path: Path):
        """未設置の機体で `remove` を叩いてもエラーにしない（web-service.sh と同じ扱い）."""
        sudoers_dir = tmp_path / "sudoers.d"
        sudoers_dir.mkdir()
        bin_dir = tmp_path / "bin"

        completed = run_script(
            ["remove"],
            sudoers_dir,
            sudo=stub_sudo(bin_dir),
            visudo=stub_visudo(bin_dir),
            stub_bin=bin_dir,
        )

        assert completed.returncode == 0, completed.stderr


class TestMainDispatch:
    """`main` 経路の引数解釈."""

    @pytest.mark.parametrize("args", ([], ["enable"], ["install", "api"]))
    def test_rejects_unknown_invocation_with_usage(
        self, args: list[str], tmp_path: Path
    ):
        completed = run_script(args, tmp_path)

        assert completed.returncode != 0
        assert "Usage:" in completed.stderr
        assert not list(tmp_path.iterdir()), "引数を弾く前に副作用が出ている"

    def test_show_needs_no_privileges(self, tmp_path: Path):
        """設置前に内容を確認する経路。`sudo` も `visudo` も呼ばない."""
        completed = run_script(["show"], tmp_path, sudo="false", visudo="false")

        assert completed.returncode == 0, completed.stderr
        assert alias_commands(completed.stdout) == list(ALLOWED_COMMANDS)
        assert not list(tmp_path.iterdir())
