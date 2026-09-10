"""Systemd unit の観測と再起動の予約.

特権を使うのは `sudo -n <systemctl> restart --no-block <units>` の 1 経路だけ。
`systemctl is-active` は**非特権で読める**ので sudoers に入れない（`sudo systemctl
status` はページャ経由で root シェルを取られる）。
"""

from __future__ import annotations

import os
import time

from web.selfupdate.settings import UpdateSettings
from web.selfupdate.steps import restart_command, run_command

# キー → フル unit 名。sudoers の argv と直結するので、外へ出す値は常にフル unit 名
UNIT_NAMES: dict[str, str] = {
    "api": "pcbasm-api.service",
    "ui": "pcbasm-ui.service",
}

# 同居機の argv は api → ui の 1 変種しか sudoers に無い。**順序も契約**
CANONICAL_ORDER: tuple[str, ...] = ("api", "ui")

# フル unit 名 → キー（`web-service.sh` の対象名）。unit を `render` に掛けるときに使う
UNIT_KEYS: dict[str, str] = {name: key for key, name in UNIT_NAMES.items()}

# 表示名（サーバ側で文言を組むため。JS やテンプレートに複製しない）
UNIT_LABELS: dict[str, str] = {
    "pcbasm-api.service": "backend WebAPI",
    "pcbasm-ui.service": "UI frontend",
}


# `systemctl is-active` の答えのうち「更新後に再起動すべき」もの。
# `failed` を含めるのが要点: 起動に失敗したリビジョンを直して更新し直すとき、
# 落ちている unit を対象から外すと **修正が永久に適用されない**
# （計画書「既知のリスク 1: ロールバック無し」からの復帰経路）。
# `inactive` / `deactivating` は意図的に止めているので起こさない。
RESTARTABLE_STATES = frozenset({"active", "activating", "reloading", "failed"})


def active_units(settings: UpdateSettings) -> tuple[str, ...]:
    """このホストで起動中（または復帰させるべき）の pcbasm unit を canonical 順で返す.

    `systemctl is-active` は非特権で読めるので sudoers には入れない。
    """
    running: list[str] = []
    for key in CANONICAL_ORDER:
        unit = UNIT_NAMES[key]
        result = run_command(
            (settings.systemctl_bin, "is-active", unit),
            timeout=settings.systemctl_timeout,
        )
        state = result.output.strip().splitlines()[:1]
        if state and state[0] in RESTARTABLE_STATES:
            running.append(unit)
    return tuple(running)


def _unwrapped(text: str) -> str:
    """折り返しを畳んで空白を 1 個に正規化する.

    実 `sudo -l` は tty が無いと端末幅（既定 80 桁）で単語折り返しし、継続行を 字下げする。許可行は 84〜102
    文字あるので、生のまま substring 照合すると **正しく設置した実機でも一致しない**。テストのスタブは折り返さないので、
    ここが無いと「実装の仮定をミラーしたテスト」になる。
    """
    return " ".join(text.replace("\\\n", "\n").split())


def restart_permitted(settings: UpdateSettings, units: tuple[str, ...]) -> str | None:
    """その argv を非対話 sudo で実行できるか下見する（失敗理由を返す）.

    実際に再起動する前に確かめるのは、sudoers 未設置の機体で「git だけ進んで
    再起動できない」状態を作らないため（実行順序 1）。

    折り返し対策を二重にかけるのは、**どちらの一手も単独では穴が残る**ため。
    `COLUMNS` は sudo が tty を持たないときだけ見る env フォールバックなので、
    本番（unit から起動された非対話プロセス）では効くが、tty のある手元実行では
    実端末幅で折り返される。そこを畳むのが `_unwrapped` の役目で、こちらは
    sudo の折り返し方に依存しない。片方だけでは、正しく設置した機体で照合が
    外れて「許可されていません」と嘘をつく経路が残る。

    Args:
        settings: バイナリのパスを持つ設定
        units: 再起動予定のフル unit 名（canonical 順。空なら再起動しないので許可不要）

    Returns:
        許可されていれば None、そうでなければ理由
    """
    if not units:
        return None
    listing = run_command(
        (settings.sudo_bin, "-n", "-l"),
        env=os.environ | {"COLUMNS": "1000"},
        timeout=settings.systemctl_timeout,
    )
    hint = (
        "./scripts/install-update-sudoers.sh install を実行してください"
        "（現在の許可は sudo -n -l で確認できます）。"
    )
    if not listing.ok:
        return f"非対話 sudo の許可を確認できませんでした。{hint}"
    permitted = " ".join(restart_command(settings, units)[2:])
    if _unwrapped(permitted) not in _unwrapped(listing.output):
        return f"'{permitted}' が sudoers で許可されていません。{hint}"
    return None


def schedule_restart(settings: UpdateSettings, units: tuple[str, ...]) -> str | None:
    """少し待ってから再起動を 1 回だけ投げる（失敗理由を返す）.

    **リクエストスレッドから呼ばない。** 呼び出し元は既に 202 を返し終えた
    バックグラウンドの更新スレッド（daemon）で、そこで待つことで応答がフラッシュされ、
    クライアントが 1 回ポーリングして `restarting` を観測できる（生存性のためではない）。

    `--no-block` は job を enqueue した時点で exit するので、投げた側が直後に
    SIGTERM で死んでも restart は PID 1 側で完走する。逆に **sudo に拒否された場合は
    自分が生き残る**ので、その事実を呼び出し元へ返して report に残せる。

    Args:
        settings: バイナリのパスと遅延を持つ設定
        units: 再起動するフル unit 名（空なら何もしない）

    Returns:
        投げられたら None、拒否・タイムアウトなら理由
    """
    if not units:
        return None
    time.sleep(settings.restart_delay)
    result = run_command(
        restart_command(settings, units), timeout=settings.systemctl_timeout
    )
    if result.timed_out:
        return "再起動コマンドが応答しませんでした。"
    if not result.ok:
        return (
            f"再起動コマンドが失敗しました（終了コード {result.returncode}）: "
            f"{result.output.strip()}"
        )
    return None


def stale_units(settings: UpdateSettings, units: tuple[str, ...]) -> tuple[str, ...]:
    """設置済み unit ファイルが現在の `web-service.sh` の出力と食い違うものを返す.

    計画書「既知のリスク 6」。`render_unit` の出力が変わった更新を取り込んでも、
    `/etc/systemd/system` の unit は古いままになる（本 MR 自身が `TimeoutStopSec` と
    `StartLimitIntervalSec` を足した）。**読み取りだけを行い、自動 install はしない**
    （root 権限を増やさない）。判定できない場合は「食い違い無し」に倒す。

    Args:
        settings: リポジトリの場所を持つ設定
        units: 調べるフル unit 名

    Returns:
        再 install が要る unit 名
    """
    script = settings.repo_root / "scripts" / "web-service.sh"
    if not script.is_file():
        return ()
    stale: list[str] = []
    for unit in units:
        target = UNIT_KEYS.get(unit)
        installed = settings.unit_dir / unit
        if target is None or not installed.is_file():
            continue
        # スクリプトを直接 argv で起動する（`bash -c` の文字列組み立て = 実質シェルを
        # 他の全経路と同じく避ける）。`render` は systemd に触らない読み取り専用の命令
        rendered = run_command(
            (str(script), "render", target),
            cwd=settings.repo_root,
            timeout=settings.systemctl_timeout,
        )
        if not rendered.ok:
            continue
        try:
            current = installed.read_text(encoding="utf-8")
        except OSError:
            continue
        if current.strip() != rendered.output.strip():
            stale.append(unit)
    return tuple(stale)


def stale_unit_warning(units: tuple[str, ...]) -> str:
    """Unit 定義が古い旨の警告文（表示文字列はサーバが組む）."""
    return (
        f"systemd の unit 定義が現在のソースと食い違っています（{' '.join(units)}）。"
        "ssh して './scripts/web-service.sh install all' を実行し直してください"
        "（更新自体は完了しています）。"
    )


def restart_notice(units: tuple[str, ...]) -> str:
    """再起動範囲を利用者向けの 1 文にする（表示文言はサーバが組む）."""
    if not units:
        return (
            "pcbasm のサービスが systemd で動いていないため、更新後の再起動は行いません"
            "（手元で起動している場合は自分で起動し直してください）。"
        )
    labels = "・".join(UNIT_LABELS.get(unit, unit) for unit in units)
    notice = f"更新後に {labels}（{' '.join(units)}）を再起動します。"
    if UNIT_NAMES["ui"] in units:
        notice += (
            "この画面を配信しているサービスも含まれるため、一時的に接続が切れます。"
        )
    return notice
