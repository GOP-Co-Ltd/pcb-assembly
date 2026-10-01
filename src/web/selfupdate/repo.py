"""Git リポジトリの観測と fast-forward 更新.

`git pull` は使わず、`fetch` → 判定 → `merge --ff-only` に分解する。
`git pull --ff-only` を 1 回で実行すると中断理由をエラー文字列から読み取ることになり、
dirty / ローカル commit / 分岐 / detached を区別できない。

`fast_forward_blocker` は「例外でなく `str | None` を返す」規約を適用する箇所で、
router がその戻り値を `HTTPException` に変換する。

dirty 判定の対象は追跡ファイルの変更だけ（`--untracked-files=no`）。untracked は記録する
だけで中断しない。ignore 漏れのファイル 1 つで全機体の更新が止まるのを避けるため。
実際に pull と衝突する場合は `merge --ff-only` が非 0 で失敗し、fail-closed になる。
"""

from __future__ import annotations

import os

import attrs

from web.selfupdate.settings import UpdateSettings
from web.selfupdate.steps import CommandResult, run_command

# 認証やホスト鍵の確認を求められたときに待ち続けないための ssh オプション。
# 実機の unit には ssh-agent が無いので、対話入力を求められた時点で永久にハングする。
GIT_SSH_COMMAND = (
    "ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes"
)


def git_env() -> dict[str, str]:
    """Git が対話入力を求めないようにする環境変数（純関数）."""
    return os.environ | {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": GIT_SSH_COMMAND,
    }


@attrs.frozen
class RepoState:
    """作業リポジトリの観測値（1 時点のスナップショット）."""

    head: str
    head_subject: str
    branch: str | None
    upstream: str | None
    upstream_head: str | None
    ahead: int
    behind: int
    dirty_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]

    @property
    def up_to_date(self) -> bool:
        """追従先に追い付いているか（upstream が無ければ判定できないので False）."""
        return self.upstream is not None and self.behind == 0


def _git(
    settings: UpdateSettings, *arguments: str, timeout: float | None = None
) -> CommandResult:
    """作業リポジトリで git を 1 回実行する（例外を投げない）."""
    return run_command(
        (settings.git_bin, *arguments),
        cwd=settings.repo_root,
        env=git_env(),
        timeout=settings.git_timeout if timeout is None else timeout,
    )


def _first_line(result: CommandResult) -> str:
    return result.output.strip().splitlines()[0] if result.output.strip() else ""


def _optional(result: CommandResult) -> str | None:
    """成功時だけ 1 行目を返す（失敗は「その情報が無い」を意味する）."""
    return _first_line(result) if result.ok else None


def _upstream(settings: UpdateSettings) -> str | None:
    """追従先の参照名（未設定なら None）."""
    return _optional(
        _git(
            settings, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
        )
    )


def _dirty_path(line: str) -> str:
    """`git status --porcelain` の 1 行からパスを取り出す（rename は移動後を使う）.

    非 ASCII 名は `core.quotepath` で `"\346..."` の形に引用されるが、ここは
    「どのファイルが変更されているか」を人に見せるだけなので、復号せずそのまま出す
    （判定に使うのは行数だけ）。
    """
    path = line[3:] if len(line) > 3 else line
    return path.rpartition(" -> ")[2] or path


def _ahead_behind(settings: UpdateSettings, upstream: str | None) -> tuple[int, int]:
    if upstream is None:
        return (0, 0)
    result = _git(settings, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
    if not result.ok:
        return (0, 0)
    counts = _first_line(result).split()
    if len(counts) != 2:
        return (0, 0)
    return (int(counts[0]), int(counts[1]))


def capture_state(settings: UpdateSettings) -> tuple[RepoState | None, str | None]:
    """作業リポジトリの現在値を観測する（例外を投げない）.

    `.git` が無いディレクトリや git が壊れている場合も例外にせず理由を返す
    （`GET /api/update/status` が常に 200 を返せるように）。

    Args:
        settings: 対象リポジトリを指す設定

    Returns:
        `(状態, None)` か `(None, 観測できない理由)`
    """
    probe = _git(settings, "rev-parse", "--git-dir")
    if not probe.ok:
        return None, (
            f"git リポジトリとして読めません: {settings.repo_root}"
            f"（{_first_line(probe) or 'git の実行に失敗しました'}）"
        )

    head = _git(settings, "rev-parse", "--short", "HEAD")
    if not head.ok:
        return None, f"HEAD を解決できません: {_first_line(head)}"

    upstream = _upstream(settings)
    ahead, behind = _ahead_behind(settings, upstream)
    # 失敗した git の stderr を「未コミットの変更」として画面に出さない
    # （index.lock 残留などで status が失敗すると原因が判らなくなる）
    # status の stat キャッシュ更新が index.lock を取ると、同時進行の merge が
    # 「別の git が実行中」で失敗する。ポーリングは観測だけにする。
    status = _git(
        settings, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no"
    )
    if not status.ok:
        return None, f"git status に失敗しました: {_first_line(status)}"
    untracked = _git(settings, "ls-files", "--others", "--exclude-standard")
    if not untracked.ok:
        return None, f"git ls-files に失敗しました: {_first_line(untracked)}"
    return (
        RepoState(
            head=_first_line(head),
            head_subject=_first_line(_git(settings, "log", "-1", "--format=%s")),
            branch=_optional(
                _git(settings, "symbolic-ref", "--quiet", "--short", "HEAD")
            ),
            upstream=upstream,
            upstream_head=(
                _optional(_git(settings, "rev-parse", "--short", upstream))
                if upstream
                else None
            ),
            ahead=ahead,
            behind=behind,
            dirty_paths=tuple(
                _dirty_path(line) for line in status.output.splitlines() if line.strip()
            ),
            untracked_paths=tuple(
                line for line in untracked.output.splitlines() if line.strip()
            ),
        ),
        None,
    )


def fast_forward_blocker(state: RepoState) -> str | None:
    """Fast-forward を妨げる事情を 1 つ返す（無ければ None）.

    確定要件「ff 不可（dirty / ローカル commit / 分岐）なら何もせず中断」を判定する箇所。 untracked
    はここでは弾かない（衝突すれば `merge --ff-only` が失敗する）。
    """
    if state.branch is None:
        return (
            "HEAD がブランチから外れています（detached）。"
            "ssh して 'git checkout main' で戻してください。"
        )
    if state.upstream is None:
        return (
            f"ブランチ {state.branch} に追従先（upstream）が設定されていません。"
            "ssh して 'git branch --set-upstream-to=origin/main main' を実行してください。"
        )
    if state.dirty_paths:
        listed = ", ".join(state.dirty_paths[:5])
        return f"未コミットの変更があります: {listed}"
    if state.ahead:
        return (
            f"ローカルに未 push の commit が {state.ahead} 件あります"
            f"（{state.branch} が {state.upstream} より先行しています）。"
        )
    return None


def fetch(settings: UpdateSettings, *, timeout: float | None = None) -> str | None:
    """Remote の最新を取得する（失敗理由を返す。ネットワーク I/O を伴う唯一の経路）.

    Args:
        settings: 対象リポジトリと既定タイムアウトを持つ設定
        timeout: 打ち切りまでの秒数（None なら `fetch_timeout`）。画面から押す
            「更新を確認」は認可なしで呼べるので、呼び出し側が短い値を渡す

    Returns:
        成功なら None、失敗なら理由
    """
    limit = settings.fetch_timeout if timeout is None else timeout
    result = _git(settings, "fetch", timeout=limit)
    if result.timed_out:
        return f"git fetch が {limit:.0f} 秒で応答しませんでした。"
    if not result.ok:
        return f"git fetch に失敗しました: {result.output.strip()}"
    return None


def merge_fast_forward(settings: UpdateSettings) -> str | None:
    """追従先へ fast-forward する（失敗理由を返す）.

    `--ff-only` なので、git 自身が merge を拒否した場合（分岐・dirty・untracked 衝突）は
    HEAD も作業ツリーも 1 バイトも変わらない。ただし checkout の途中で打ち切られた
    場合はこの限りではない。そのため、タイムアウトは `merge_timeout`（既定 600 秒）と
    長く取ってある。このリポジトリは git-lfs を使っており、smudge フィルタが
    ネットワーク待ちに入ると数十秒かかりうる。ここで SIGKILL すると一部だけ新版の
    dirty な作業ツリーが残り、以後の更新が全部止まる。
    """
    upstream = _upstream(settings)
    if upstream is None:
        return "追従先（upstream）が設定されていないため fast-forward できません。"
    result = _git(
        settings, "merge", "--ff-only", upstream, timeout=settings.merge_timeout
    )
    if result.timed_out:
        return (
            f"git merge が {settings.merge_timeout:.0f} 秒で応答しませんでした。"
            "作業ツリーが中途半端な状態になっている可能性があります"
            "（ssh して 'git status' と 'git lfs pull' を確認してください）。"
        )
    if not result.ok:
        return f"git merge --ff-only に失敗しました: {result.output.strip()}"
    return None
