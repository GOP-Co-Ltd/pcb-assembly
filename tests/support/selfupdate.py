"""自己更新を、実 git のリポジトリと外部コマンドの実スタブ実行ファイルで検証する部品.

3rd-party 表面（`git` / `uv` / `systemctl` / `sudo`）はモックしない。

- git は実物。remote は同じ tmp ディレクトリ上の bare リポジトリなので、ネットワークには出ない
- `uv` / `sudo` / `systemctl` は `UpdateSettings` の絶対パス seam に差した実スタブ実行ファイル（PATH はいじらない）
- スタブは引数を共有ログへ追記するので、呼び出しの有無と順序をログの追記順で観測できる

実機の systemd にも sudoers にも触れない。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import attrs

from pcbasm.paths import PROJECT_ROOT
from web.selfupdate.service import UNIT_NAMES
from web.selfupdate.settings import UpdateSettings

ALL_UNITS: tuple[str, ...] = (UNIT_NAMES["api"], UNIT_NAMES["ui"])

# 利用者の ~/.gitconfig に左右されないよう、global / system の config を切る
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "tester",
    "GIT_AUTHOR_EMAIL": "tester@example.invalid",
    "GIT_COMMITTER_NAME": "tester",
    "GIT_COMMITTER_EMAIL": "tester@example.invalid",
}


def git(cwd: Path, *arguments: str) -> str:
    """実 git を走らせて stdout（末尾の改行を除く）を返す（失敗したら例外）."""
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=os.environ | GIT_ENV,
    )
    return completed.stdout.strip()


def init_origin(root: Path) -> Path:
    """1 commit を持つ bare リポジトリ（remote 役）を ``root/origin.git`` に作る."""
    bare = root / "origin.git"
    git(root, "init", "--bare", "--initial-branch", "main", str(bare))
    seed = clone_origin(bare, root / "seed")
    (seed / "tracked.txt").write_text("initial\n", encoding="utf-8")
    git(seed, "add", "tracked.txt")
    git(seed, "commit", "-m", "initial")
    git(seed, "push", "-u", "origin", "main")
    return bare


def clone_origin(origin: Path, path: Path) -> Path:
    """``origin`` を ``path`` へ clone する（``main`` が ``origin/main`` を追跡する）."""
    git(path.parent, "clone", str(origin), str(path))
    git(path, "config", "user.email", "tester@example.invalid")
    git(path, "config", "user.name", "tester")
    return path


def push_commit(publisher: Path, *, name: str = "tracked.txt", body: str) -> str:
    """Origin へ 1 commit 積む（被験体から見て behind を 1 増やす）.

    Args:
        publisher: `publisher` fixture のパス
        name: 変更するファイル名
        body: 書き込む内容

    Returns:
        積んだ commit の短縮 SHA
    """
    (publisher / name).write_text(body, encoding="utf-8")
    git(publisher, "add", name)
    git(publisher, "commit", "-m", f"update {name}")
    git(publisher, "push", "origin", "main")
    return git(publisher, "rev-parse", "--short", "HEAD")


def local_commit(clone: Path, *, name: str = "local.txt", body: str = "local\n") -> str:
    """被験体側にローカル commit を積む（ahead を 1 増やす）.

    Returns:
        積んだ commit の短縮 SHA
    """
    (clone / name).write_text(body, encoding="utf-8")
    git(clone, "add", name)
    git(clone, "commit", "-m", f"local {name}")
    return git(clone, "rev-parse", "--short", "HEAD")


def head(repository: Path) -> str:
    """HEAD の短縮 SHA."""
    return git(repository, "rev-parse", "--short", "HEAD")


def write_stub(
    path: Path,
    *,
    exit_code: int = 0,
    log: Path | None = None,
    sleep: float = 0.0,
    fail_match: str | None = None,
    pid_file: Path | None = None,
) -> str:
    """引数をログへ追記して指定の終了コードで終わる実行ファイルを書く.

    `UpdateSettings` の `uv_bin` / `systemctl_bin` / `sudo_bin` へ差し込むためのもの。
    モックではなく **実プロセス**なので、runner の `subprocess` 呼び出し（引数の組み立て、
    タイムアウト、プロセスグループの後始末）が本物のまま検証できる。

    Args:
        path: 書き出す実行ファイルのパス（親ディレクトリは作る）
        exit_code: 既定の終了コード
        log: 追記先。`<コマンド名> <引数...>` の 1 行を追記する
        sleep: 終了前に待つ秒数（タイムアウト検証用）
        fail_match: 引数のどれかがこの文字列と一致したら 1 で終わる
            （`uv sync` は成功して `uv run` だけ失敗する、のような出し分け用）
        pid_file: 自身と `sleep` 子プロセスの PID を追記する先
            （タイムアウト時に **プロセスグループごと**殺されたことを確かめる）

    Returns:
        書き出した実行ファイルの絶対パス（`UpdateSettings` にそのまま渡せる）
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#!/bin/bash", 'name="$(basename "$0")"']
    if log is not None:
        lines.append(f'printf "%s %s\\n" "$name" "$*" >> "{log}"')
    if pid_file is not None:
        lines.append(f'printf "%s\\n" "$$" >> "{pid_file}"')
    if sleep > 0:
        lines.append(f"sleep {sleep} &")
        lines.append("child=$!")
        if pid_file is not None:
            lines.append(f'printf "%s\\n" "$child" >> "{pid_file}"')
        lines.append('wait "$child"')
    if fail_match is not None:
        lines.append(
            'for argument in "$@"; do\n'
            f'  if [ "$argument" = "{fail_match}" ]; then exit 1; fi\n'
            "done"
        )
    lines.append(f"exit {exit_code}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o755)
    return str(path)


def write_systemctl_stub(
    path: Path,
    *,
    log: Path | None = None,
    active: tuple[str, ...] = ALL_UNITS,
    active_state: str = "active",
) -> str:
    """`systemctl` のスタブ（`is-active` の答えを unit ごとに選べる）.

    対象を選べるのは、frontend 専用機や「systemd で動かしていない開発機」を再現するため。

    Args:
        path: 書き出す実行ファイルのパス
        log: 呼び出しを追記するログ（None なら記録しない）
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
        f"{_log_line('systemctl', log)}\n"
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
    log: Path | None = None,
    permitted: bool = True,
    wrap: bool = False,
    restart_exit_code: int = 0,
) -> str:
    """`sudo` のスタブ.

    2 通りの呼ばれ方をする:

    - `sudo -n -l`（`restart_permitted` の下見）: 許可済みの listing を stdout に出す。
      `permitted=False` なら sudoers 未設置と同じく非 0 で終わる
    - `sudo -n <systemctl> restart --no-block <units>`（実際の再起動）: 引数をログへ追記し、`restart_exit_code` で終わる

    実 `sudo -l` は tty が無いと 80 桁で折り返す（許可行は 84〜102 文字）ので、`wrap=True` でその挙動を再現する。
    スタブが実物より甘いと「実装の仮定をミラーしたテスト」になる。

    Args:
        path: 書き出す実行ファイルのパス
        systemctl_bin: listing に載せる systemctl の絶対パス
        log: 呼び出しを追記するログ（None なら記録しない）
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
        f"{_log_line('sudo', log)}\n"
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

    折り返した行の末尾には、実物と同じく継続文字 `\\` を置く（省くと実物より甘いスタブになる）。
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


def _log_line(name: str, log: Path | None) -> str:
    """スタブが呼ばれた引数を ``log`` へ追記する shell の 1 行（``log`` が None なら何もしない）."""
    if log is None:
        return ":"
    return f'printf "%s %s\\n" {name} "$*" >> "{log}"'


def call_log(log: Path) -> list[str]:
    """スタブが追記した呼び出しログを行のリストで読む（未作成なら空）."""
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def restart_calls(log: Path) -> list[str]:
    """再起動を試みた呼び出しだけを抜き出す.

    `sudo` 側と `systemctl` 側の両方を見るので、どちらの seam を通っても検出する。
    「`uv sync` が失敗したら再起動しない」という要件はこれが空であることで確かめる。
    """
    return [line for line in call_log(log) if "restart" in line]


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
        """`install.sh render <target>` の出力（現在のソースが期待する unit）."""
        return subprocess.run(
            [str(self.clone / "install.sh"), "render", target],
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
    with_installer: bool = False,
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
        with_installer: 実物の unit の生成器（`install.sh` と雛形、旧版向けの
            `scripts/web-service.sh`）を clone へ複製するか（unit 定義の差分チェックを走らせたいときだけ True）

    Returns:
        組み立てた sandbox
    """
    root.mkdir(parents=True, exist_ok=True)
    origin = init_origin(root)
    clone = clone_origin(origin, root / "clone")
    publisher = clone_origin(origin, root / "publisher")

    if with_installer:
        for path in (
            "install.sh",
            "deploy/systemd/pcbasm.service.in",
            "scripts/web-service.sh",
        ):
            (clone / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PROJECT_ROOT / path, clone / path)

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
