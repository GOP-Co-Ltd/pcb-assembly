"""外部コマンドの argv 組み立てと、その実行（ハング対策込み）.

argv を組む関数は純関数で、`git` / `uv` / `systemctl` / `sudo` を一切呼ばない。
`restart_command` の出力は `/etc/sudoers.d/pcbasm-update` が許す argv と 1 文字でも
違えば実機で `sudo: a password is required` になるため、`scripts/install-update-sudoers.sh`
の `render_sudoers` と対で維持する（unit の並びも契約）。

`run_command` をこのモジュールに置くのは、外部コマンドを「組み立てる」場所と
「走らせる」場所を 1 つにまとめるため（`repo` / `service` / `runner` が共有する）。
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import attrs

from web.selfupdate.settings import UpdateSettings

# unit → 更新後に import できることを確かめるモジュール。
# そのホストが実際に動かすものだけを対象にする。`web.api.app` は picamera2 /
# pcbnew / cv2 を eager import するので、frontend 専用機で import すると必ず
# ModuleNotFoundError になる。すると「ソースだけ新しくなり、再起動されない」状態から
# 抜け出せなくなる。
SMOKE_MODULES: dict[str, str] = {
    "pcbasm-api.service": "web.api.app",
    "pcbasm-ui.service": "web.ui.app",
}

# 全部入り（同居機）の既定値。unit を観測できない文脈で参照する
ALL_SMOKE_MODULES: tuple[str, ...] = ("web.api.app", "web.ui.app")


def sync_command(settings: UpdateSettings) -> tuple[str, ...]:
    """依存を lock ファイル通りに揃える argv."""
    return (settings.uv_bin, "sync", *settings.uv_sync_args)


def smoke_modules(units: Sequence[str]) -> tuple[str, ...]:
    """Active な unit が実際に動かすアプリのモジュール名（canonical 順）.

    Args:
        units: フル unit 名（`active_units()` の結果）

    Returns:
        import して確かめるモジュール名（該当なしなら空）
    """
    return tuple(SMOKE_MODULES[unit] for unit in units if unit in SMOKE_MODULES)


def smoke_command(
    settings: UpdateSettings, modules: Sequence[str] = ALL_SMOKE_MODULES
) -> tuple[str, ...]:
    """更新後の import smoke の argv（`--no-sync` で依存を書き換えない）.

    本番では必ず `smoke_modules(active_units)` の結果を渡す。既定の全部入りを
    frontend 専用機で使うと、`web.api.app` が picamera2 / pcbnew を要求して必ず失敗する
    （既定を残しているのは契約テストが引数なしの形を固定しているため）。

    Args:
        settings: バイナリのパスを持つ設定
        modules: import するモジュール（既定は同居機と同じ全部入り）

    Returns:
        `uv run --no-sync python -c "import ..."`
    """
    source = f"import {', '.join(modules)}"
    return (settings.uv_bin, "run", "--no-sync", "python", "-c", source)


def restart_command(settings: UpdateSettings, units: Sequence[str]) -> tuple[str, ...]:
    """サービス再起動の argv（sudoers が許す固定 argv と完全一致させる）.

    `--no-block` を付けると job を enqueue した時点で exit する。そのため、完了を待つ間に
    自分の cgroup ごと kill されて結果がわからなくなることがない。`-n` が無いと、
    非対話プロセスがパスワード入力待ちでハングする。

    Args:
        settings: バイナリのパスを持つ設定
        units: 再起動するフル unit 名（`CANONICAL_ORDER` の順）

    Returns:
        `sudo -n <systemctl> restart --no-block <units...>`
    """
    return (
        settings.sudo_bin,
        "-n",
        settings.systemctl_bin,
        "restart",
        "--no-block",
        *units,
    )


@attrs.frozen
class CommandResult:
    """外部コマンド 1 回分の結果（stdout と stderr は 1 本にまとめる）."""

    argv: tuple[str, ...]
    returncode: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """正常終了したか."""
        return self.returncode == 0 and not self.timed_out


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    """プロセスグループごと SIGKILL する（子を残さない）.

    `start_new_session=True` で起動しているので、`git` が起動した `ssh` や
    シェルスクリプトが起動した子プロセスもまとめて kill できる。既に終了していれば
    何もしない。
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float,
) -> CommandResult:
    """外部コマンドを実行して結果を返す（例外を投げない）.

    非対話プロセスから呼ぶので、入力待ちで止まらないよう `stdin` を `DEVNULL` に
    つなぎ、`timeout` を必ず与える。タイムアウト時はプロセスグループごと kill する。
    実行ファイルが見つからない場合も送出せず、失敗した結果として返す。

    Args:
        argv: 実行する argv
        cwd: 実行ディレクトリ
        env: 環境変数（None なら親のまま）
        timeout: 打ち切りまでの秒数

    Returns:
        終了コード・出力・タイムアウトの有無
    """
    argv = tuple(argv)
    # `with` は使わない。Popen.__exit__ は無期限の wait() を呼ぶため、kill しきれない
    # 子プロセスがパイプを掴んでいると、テストが失敗せずに終わらなくなる
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        # 実行ファイルが無いホスト（git の無いコンテナ等）。例外にすると
        # `GET /api/update/status` が「常に 200 を返す」契約を守れない
        return CommandResult(argv, -1, f"{argv[0]} を起動できません: {exc}")
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        try:
            output, _ = process.communicate(timeout=10.0)
        except subprocess.TimeoutExpired:
            output = ""
        if process.stdout is not None:
            process.stdout.close()
        return CommandResult(argv, process.returncode or -1, output, True)
    return CommandResult(argv, process.returncode, output)


def tail(text: str, lines: int) -> str:
    """出力の末尾 `lines` 行だけを残す（report が際限なく大きくならないように）.

    `lines <= 0` なら何も残さない。素の `[-0:]` はスライスの仕様で全文になるため、 明示的に空文字を返す。
    """
    if lines <= 0:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])
