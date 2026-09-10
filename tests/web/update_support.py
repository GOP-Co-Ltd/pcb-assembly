"""自己更新を HTTP 越しに検証するための実リポジトリ + スタブ一式.

`tests/web/selfupdate/conftest.py`（契約テストが使う実 git fixture とスタブ生成）を
そのまま流用し、router / frontend / E2E から 1 行で組める形にまとめる。ここでも
3rd-party 表面（`git` / `uv` / `systemctl` / `sudo`）は一切モックしない:

- git は **実物**。remote は同じ tmp ディレクトリ上の bare リポジトリなので
  ネットワークには出ない
- `uv` / `sudo` / `systemctl` は `UpdateSettings` の絶対パス seam に差した
  **実スタブ実行ファイル**（PATH はいじらない）

実機の systemd にも sudoers にも触れない。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import attrs

from pcbasm.utils import PROJECT_ROOT
from tests.web.selfupdate.conftest import (
    call_log,
    git,
    head,
    push_commit,
    restart_calls,
    write_stub,
)
from web.selfupdate.service import UNIT_NAMES
from web.selfupdate.settings import UpdateSettings

ALL_UNITS: tuple[str, ...] = (UNIT_NAMES["api"], UNIT_NAMES["ui"])


@attrs.frozen
class UpdateSandbox:
    """更新の被験体（作業リポジトリ）と、それを進めるための remote・ログ."""

    settings: UpdateSettings
    publisher: Path
    log: Path
    # 設置済み unit を模した読み取り専用ディレクトリ（実機の /etc/systemd/system 代役）
    unit_dir: Path
    # 積んだ commit 数（同じ内容を 2 度 push すると git が「変更なし」で落ちる）
    _pushed: list[str] = attrs.field(factory=list)

    @property
    def clone(self) -> Path:
        """被験体の作業リポジトリ."""
        return self.settings.repo_root

    def push(self) -> str:
        """Origin へ 1 commit 積む（被験体から見て behind を 1 増やす）.

        Returns:
            積んだ commit の短縮 SHA（更新後の到達先）
        """
        sha = push_commit(self.publisher, body=f"revision {len(self._pushed) + 1}\n")
        self._pushed.append(sha)
        return sha

    def head(self) -> str:
        """被験体の HEAD（短縮 SHA）."""
        return head(self.clone)

    def install_unit(self, unit: str, text: str) -> None:
        """設置済み unit ファイルを置く（unit 定義の差分チェック用）."""
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        (self.unit_dir / unit).write_text(text, encoding="utf-8")

    def rendered_unit(self, target: str) -> str:
        """`web-service.sh render <target>` の出力（現在のソースが期待する unit）."""
        import subprocess

        return subprocess.run(
            [str(self.clone / "scripts" / "web-service.sh"), "render", target],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def calls(self) -> list[str]:
        """スタブが記録した外部コマンド呼び出しの全行."""
        return call_log(self.log)

    def restarts(self) -> list[str]:
        """再起動を試みた呼び出しだけ（要件「失敗したら再起動しない」の観測点）."""
        return restart_calls(self.log)


def write_systemctl_stub(
    path: Path,
    *,
    log: Path,
    active: tuple[str, ...] = ALL_UNITS,
    active_state: str = "active",
) -> str:
    """`systemctl` のスタブ（`is-active` の答えを unit ごとに選べる）.

    `tests/web/selfupdate/conftest.py` のものは常に active を返す。frontend 専用機や
    「systemd で動かしていない開発機」を再現するために、ここでは対象を選べるようにする。

    Args:
        path: 書き出す実行ファイルのパス
        log: 呼び出しを追記するログ
        active: `active_state` と答えるフル unit 名
        active_state: その unit に対する `is-active` の答え
            （`activating` / `failed` など、実 systemd が返す他の状態も試せる）

    Returns:
        書き出した実行ファイルの絶対パス
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    listed = " ".join(active)
    path.write_text(
        "#!/bin/bash\n"
        f'printf "%s %s\\n" systemctl "$*" >> "{log}"\n'
        'if [ "$1" = "is-active" ]; then\n'
        f'  case " {listed} " in\n'
        f'    *" $2 "*) echo {active_state} ;;\n'
        "    *) echo inactive; exit 3 ;;\n"
        "  esac\n"
        "fi\n"
        "exit 0\n"
    )
    path.chmod(0o755)
    return str(path)


def write_sudo_stub(
    path: Path,
    *,
    systemctl_bin: str,
    log: Path,
    permitted: bool = True,
    wrap: bool = False,
    restart_exit_code: int = 0,
) -> str:
    """`sudo` のスタブ.

    `tests/web/selfupdate/conftest.py` のものは listing を折り返さず、再起動を常に
    受け入れる。実 `sudo -l` は tty が無いと 80 桁で折り返す（許可行は 84〜102 文字）
    ので、`wrap=True` でその挙動を再現できるようにする — スタブが実物より甘いと
    「実装の仮定をミラーしたテスト」になる。

    Args:
        path: 書き出す実行ファイルのパス
        systemctl_bin: listing に載せる systemctl の絶対パス
        log: 呼び出しを追記するログ
        permitted: `-l` の下見を成功させるか
        wrap: listing を実 sudo と同じように折り返すか
        restart_exit_code: 実際の再起動呼び出しの終了コード（非 0 = sudo が拒否）

    Returns:
        書き出した実行ファイルの絶対パス
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    commands = [
        f"{systemctl_bin} restart --no-block {units}"
        for units in (
            UNIT_NAMES["api"],
            UNIT_NAMES["ui"],
            f"{UNIT_NAMES['api']} {UNIT_NAMES['ui']}",
        )
    ]
    body = "".join(f"    (root : root) NOPASSWD: {command}\n" for command in commands)
    if wrap:
        body = _wrapped(commands)
    path.write_text(
        "#!/bin/bash\n"
        f'printf "%s %s\\n" sudo "$*" >> "{log}"\n'
        'for argument in "$@"; do\n'
        '  if [ "$argument" = "-l" ]; then\n'
        f"    cat <<'SUDOERS_LISTING'\n{body}SUDOERS_LISTING\n"
        f"    exit {0 if permitted else 1}\n"
        "  fi\n"
        "done\n"
        f"exit {restart_exit_code}\n"
    )
    path.chmod(0o755)
    return str(path)


def _wrapped(commands: list[str], width: int = 80) -> str:
    """実 sudo と同じように単語境界で折り返した listing を作る.

    折り返した行の末尾には継続文字 `\\` を置く。ここを省くと実物より甘いスタブになり、 「折り返しに強い」という回帰テストが 1
    段弱くなる。
    """
    words = " ".join(
        f"(root : root) NOPASSWD: {command}," for command in commands
    ).split()
    lines: list[str] = []
    current = "   "
    for word in words:
        if len(current) + 1 + len(word) > width:
            lines.append(f"{current} \\")
            current = "       "
        current = f"{current} {word}"
    lines.append(current.rstrip(","))
    return "User tester may run the following commands:\n" + "\n".join(lines) + "\n"


def make_update_sandbox(
    root: Path,
    *,
    enabled: bool = True,
    permitted: bool = True,
    uv_fail_match: str | None = None,
    active: tuple[str, ...] = ALL_UNITS,
    active_state: str = "active",
    wrap_sudo_listing: bool = False,
    restart_exit_code: int = 0,
    with_service_script: bool = False,
) -> UpdateSandbox:
    """実 git の origin / clone / publisher とスタブ実行ファイルを作る.

    Args:
        root: 一式を作るディレクトリ（テストの `tmp_path` 配下）
        enabled: 自己更新を有効にするか（`enabled=False` は 403 の検証用）
        permitted: `sudo -n -l` の下見を成功させるか（sudoers 未設置の再現）
        uv_fail_match: この引数が渡されたときだけ `uv` を失敗させる（`sync` / `run`）
        active: `systemctl is-active` が `active_state` と答える unit（frontend 専用機の再現）
        active_state: その答え（`failed` など再起動対象の判定を試すのに使う）
        wrap_sudo_listing: `sudo -l` の出力を実機と同じ 80 桁で折り返すか
        restart_exit_code: 再起動呼び出しの終了コード（非 0 = sudo が実際に拒否）
        with_service_script: 実物の `scripts/web-service.sh` を clone へ複製するか
            （unit 定義の差分チェックを走らせたいときだけ True）

    Returns:
        組み立てた sandbox
    """
    root.mkdir(parents=True, exist_ok=True)
    bare = root / "origin.git"
    git(root, "init", "--bare", "--initial-branch", "main", str(bare))

    seed = root / "seed"
    git(root, "clone", str(bare), str(seed))
    _configure(seed)
    (seed / "tracked.txt").write_text("initial\n", encoding="utf-8")
    git(seed, "add", "tracked.txt")
    git(seed, "commit", "-m", "initial")
    git(seed, "push", "-u", "origin", "main")

    clone = root / "clone"
    git(root, "clone", str(bare), str(clone))
    _configure(clone)
    publisher = root / "publisher"
    git(root, "clone", str(bare), str(publisher))
    _configure(publisher)

    if with_service_script:
        scripts = clone / "scripts"
        scripts.mkdir(exist_ok=True)
        shutil.copy2(PROJECT_ROOT / "scripts" / "web-service.sh", scripts)

    bin_dir = root / "bin"
    log = root / "calls.log"
    unit_dir = root / "systemd"
    systemctl = write_systemctl_stub(
        bin_dir / "systemctl", log=log, active=active, active_state=active_state
    )
    settings = UpdateSettings(
        repo_root=clone,
        state_dir=root / "state",
        uv_bin=write_stub(bin_dir / "uv", log=log, fail_match=uv_fail_match),
        systemctl_bin=systemctl,
        sudo_bin=write_sudo_stub(
            bin_dir / "sudo",
            systemctl_bin=systemctl,
            log=log,
            permitted=permitted,
            wrap=wrap_sudo_listing,
            restart_exit_code=restart_exit_code,
        ),
        unit_dir=unit_dir,
        enabled=enabled,
        restart_delay=0.05,
    )
    return UpdateSandbox(
        settings=settings, publisher=publisher, log=log, unit_dir=unit_dir
    )


def _configure(repository: Path) -> None:
    git(repository, "config", "user.email", "tester@example.invalid")
    git(repository, "config", "user.name", "tester")
