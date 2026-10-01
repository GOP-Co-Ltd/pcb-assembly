"""自己更新の設定（外部バイナリ・パス・タイムアウトの集約点）.

`pcbasm` ではなく `web` の下に置く。これはデプロイ運用であって装置ドメインではなく、
`pcbasm` に `pcbasm-api.service` や `uv` を知らせたくない。

外部コマンドの絶対パスはすべてここに集める。テストは PATH を変更せず、この
seam にスタブ実行ファイルを差し込むことで、実プロセス経路のまま検証できる。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import attrs

from pcbasm.utils import PROJECT_ROOT


def _which(name: str, fallback: str) -> str:
    """PATH から実行ファイルの絶対パスを探す（見つからなければ既定値）."""
    return shutil.which(name) or fallback


def _default_git_bin() -> str:
    return _which("git", "/usr/bin/git")


def _default_uv_bin() -> str:
    return _which("uv", "/usr/bin/uv")


@attrs.frozen
class UpdateSettings:
    """`git pull` → `uv sync` → サービス再起動に必要な設定一式."""

    repo_root: Path = PROJECT_ROOT
    # report と単一実行ロックの置き場所（プロセスを跨ぐ唯一の状態）
    state_dir: Path = PROJECT_ROOT / "data" / "selfupdate"
    git_bin: str = attrs.field(factory=_default_git_bin)
    uv_bin: str = attrs.field(factory=_default_uv_bin)
    # sudoers で argv を固定するので、この 2 つは実機の絶対パスから動かさない
    systemctl_bin: str = "/usr/bin/systemctl"
    sudo_bin: str = "/usr/bin/sudo"
    # 設置済み unit の置き場所（読み取り専用。書き込みは web-service.sh の特権経路）
    unit_dir: Path = Path("/etc/systemd/system")
    # 素の `uv sync` は (a) 指定しなかった dependency group を削除し（機体ごとに
    # 足した group が更新のたびに消える）、(b) pyproject と lock がずれると uv.lock を
    # 書き換える（tree が dirty になり以後の更新が全部止まる）。既定の引数でこの 2 つを防ぐ
    uv_sync_args: tuple[str, ...] = ("--locked", "--inexact")
    # 機体ごとに無効化できる安全弁（無効なら start() は何もせず理由を返す）
    enabled: bool = True
    # ハング時に打ち切るまでの上限。git はネットワーク I/O、uv sync は依存の取得を含む。
    # git_timeout は rev-parse / status などローカルで完結する読み取り専用コマンド用
    git_timeout: float = 30.0
    fetch_timeout: float = 120.0
    # merge は checkout を含み、git-lfs の smudge フィルタがネットワーク待ちに入る。
    # ここで打ち切ると checkout 途中で SIGKILL され、一部だけ新版の dirty な作業ツリーが
    # 残る（以後の更新が全部「未コミットの変更があります」で止まる）。そのため長めに取る
    merge_timeout: float = 600.0
    # 画面から押す「更新を確認」の fetch。認可なしで呼べるので短く制限する
    # （同期ハンドラなのでスレッドプールを長時間占有させない）
    check_fetch_timeout: float = 20.0
    sync_timeout: float = 900.0
    smoke_timeout: float = 180.0
    systemctl_timeout: float = 30.0
    # 202 応答をフラッシュし、クライアントが 1 回ポーリングして restarting を
    # 観測するための遅延（生存性のためではない）
    restart_delay: float = 1.0
    # 更新通知のために remote を fetch する周期 [s]（0 以下で無効）。`GET status` は
    # 毎秒ポーリングされるため fetch しない。誰も「更新を確認」を押さないと
    # behind が永久に 0 のままになるので、更新通知が出るのはこの定期 fetch があるときだけ
    watch_interval: float = 1800.0
    # report に残す各ステップ出力の行数上限（全量返して差分管理を作らない）
    output_tail_lines: int = 200

    @property
    def report_path(self) -> Path:
        """更新結果の永続化先（再起動を跨いで読む唯一の状態）."""
        return self.state_dir / "update.json"

    @property
    def lock_path(self) -> Path:
        """単一実行ロック（flock）。同居機の api / ui 同時実行もこれで止まる."""
        return self.state_dir / "update.lock"
