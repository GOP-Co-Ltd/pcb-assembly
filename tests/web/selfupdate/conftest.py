"""`src/web/selfupdate/` の契約テストが共有する実 git リポジトリとスタブ実行ファイル.

計画書「テスト計画」の方針に従い、3rd-party 表面（`subprocess` / `git` / `uv` /
`systemctl` / `sudo`）を一切モックしない。

- git は **実物**を使う（bare origin + clone を `tmp_path` に作る）。
  `tests/ml/experiment/test_provenance.py` が先例
- 外部バイナリは `UpdateSettings` が絶対パスで持つ seam に **実スタブ実行ファイル**を
  差し込む（PATH をいじらない）。引数を共有ログへ追記するので、呼び出しの有無と
  順序をログの追記順で観測できる

ネットワークは一切使わない（remote は tmp_path 上のローカル bare リポジトリ）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "tester",
    "GIT_AUTHOR_EMAIL": "tester@example.invalid",
    "GIT_COMMITTER_NAME": "tester",
    "GIT_COMMITTER_EMAIL": "tester@example.invalid",
}


def git(cwd: Path, *arguments: str) -> str:
    """実 git を走らせて stdout を返す（失敗したら例外）.

    ユーザーの `~/.gitconfig` に左右されないよう、global / system の config を切る。

    Args:
        cwd: 実行ディレクトリ
        *arguments: git への引数

    Returns:
        stdout（末尾の改行を除いたもの）
    """
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=_git_process_env(),
    )
    return completed.stdout.strip()


def _git_process_env() -> dict[str, str]:
    return os.environ | GIT_ENV


def _configure(repository: Path) -> None:
    git(repository, "config", "user.email", "tester@example.invalid")
    git(repository, "config", "user.name", "tester")


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """1 commit を持つ bare リポジトリ（remote 役）を作る.

    Returns:
        bare リポジトリのパス（`git clone` の対象）
    """
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch", "main", str(bare))

    seed = tmp_path / "seed"
    git(tmp_path, "clone", str(bare), str(seed))
    _configure(seed)
    (seed / "tracked.txt").write_text("initial\n", encoding="utf-8")
    git(seed, "add", "tracked.txt")
    git(seed, "commit", "-m", "initial")
    git(seed, "push", "-u", "origin", "main")
    return bare


@pytest.fixture
def clone(tmp_path: Path, origin: Path) -> Path:
    """被験体の作業リポジトリ（`main` が `origin/main` を追跡している）."""
    working = tmp_path / "clone"
    git(tmp_path, "clone", str(origin), str(working))
    _configure(working)
    return working


@pytest.fixture
def publisher(tmp_path: Path, origin: Path) -> Path:
    """Origin に commit を積むための 2 本目の clone（被験体とは別ディレクトリ）."""
    working = tmp_path / "publisher"
    git(tmp_path, "clone", str(origin), str(working))
    _configure(working)
    return working


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


def write_sudo_stub(
    path: Path, *, systemctl_bin: str, log: Path | None = None, permitted: bool = True
) -> str:
    """`sudo` の seam に差し込むスタブを書く.

    2 通りの呼ばれ方をする:

    - `sudo -n -l`（`restart_permitted` の下見）: 許可済みの listing を stdout に出す。
      `permitted=False` なら sudoers 未設置と同じく非 0 で終わる
    - `sudo -n <systemctl> restart --no-block <units>`（実際の再起動）: 引数をログへ
      追記するだけ

    Args:
        path: 書き出す実行ファイルのパス
        systemctl_bin: listing に載せる systemctl の絶対パス（`UpdateSettings` と揃える）
        log: 追記先
        permitted: `-l` の下見を成功させるか

    Returns:
        書き出した実行ファイルの絶対パス
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    listing = "\\n".join(
        f"    (root : root) NOPASSWD: {systemctl_bin} restart --no-block {units}"
        for units in (
            "pcbasm-api.service",
            "pcbasm-ui.service",
            "pcbasm-api.service pcbasm-ui.service",
        )
    )
    log_line = f'printf "%s %s\\n" sudo "$*" >> "{log}"' if log is not None else ":"
    path.write_text(
        "#!/bin/bash\n"
        f"{log_line}\n"
        'for argument in "$@"; do\n'
        '  if [ "$argument" = "-l" ]; then\n'
        f'    printf "{listing}\\n"\n'
        f"    exit {0 if permitted else 1}\n"
        "  fi\n"
        "done\n"
        "exit 0\n"
    )
    path.chmod(0o755)
    return str(path)


def write_systemctl_stub(path: Path, *, log: Path | None = None) -> str:
    """`systemctl` の seam に差し込むスタブを書く（`is-active` は active を返す）."""
    path.parent.mkdir(parents=True, exist_ok=True)
    log_line = (
        f'printf "%s %s\\n" systemctl "$*" >> "{log}"' if log is not None else ":"
    )
    path.write_text(
        "#!/bin/bash\n"
        f"{log_line}\n"
        'if [ "$1" = "is-active" ]; then\n'
        '  echo "active"\n'
        "fi\n"
        "exit 0\n"
    )
    path.chmod(0o755)
    return str(path)


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
