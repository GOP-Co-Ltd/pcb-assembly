"""更新の逐次実行（単一実行ロック・report 永続化・再起動の予約）.

実行順序（計画書「1. 共通モジュール」）:

```
0 単一実行ロック（flock: state_dir/update.lock）  … 同居機の api/ui 同時実行も止まる
1 restart_permitted()  … sudoers 未設置なら pull する前に中断
2 fetch → fast_forward_blocker → 中断 or 継続
3 merge --ff-only          （失敗 → 中断）
4 uv sync --locked --inexact（失敗 → 再起動しない）
5 import smoke              （失敗 → 再起動しない）
6 report を JSON へ永続化   … プロセスが死んでも残る唯一の状態
7 schedule_restart(active_units())
```

**参照先は絶対にリクエストパラメータにしない。** ブランチ・remote・ref・`uv` の引数は
すべて `UpdateSettings` 側の固定値。ここを開けると「LAN から任意コード実行」に悪化する。

更新以外に 2 つの役目を兼ねる（どちらも同じ `UpdateSettings` と観測値を使うため）:

- `restart_services()`: 更新せずに unit だけ再起動する（ファームウェア再起動から呼ぶ）
- `start_watching()`: 通知のための定期 `git fetch`（`GET status` は fetch しないので、
  これが無いと「更新があります」が誰かの手動確認まで出ない）
"""

from __future__ import annotations

import fcntl
import logging
import secrets
import threading
import time
from typing import IO

from web.selfupdate.repo import (
    RepoState,
    capture_state,
    fast_forward_blocker,
    fetch,
    git_env,
    merge_fast_forward,
)
from web.selfupdate.report import (
    UpdatePlan,
    UpdateReport,
    UpdateState,
    UpdateStep,
    load_report,
    save_report,
)
from web.selfupdate.service import (
    active_units,
    restart_permitted,
    schedule_restart,
    stale_unit_warning,
    stale_units,
)
from web.selfupdate.settings import UpdateSettings
from web.selfupdate.steps import (
    CommandResult,
    run_command,
    smoke_command,
    smoke_modules,
    sync_command,
    tail,
)

logger = logging.getLogger(__name__)


class UpdateRunner:
    """1 ホスト分の自己更新を受け持つ（`app.state.update` に 1 つ置く）."""

    def __init__(self, settings: UpdateSettings) -> None:
        """設定を保持する（副作用なし）.

        `state_dir` は実行時に作る。アプリ生成だけでディレクトリが生えると、
        更新を一度も使わないテストや frontend 専用機にゴミを残すため。

        Args:
            settings: リポジトリ・外部バイナリ・タイムアウトの設定
        """
        self._settings = settings
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._finished = threading.Event()
        self._finished.set()
        self._units: tuple[str, ...] | None = None
        self._watch: threading.Thread | None = None
        self._stop_watch = threading.Event()

    @property
    def enabled(self) -> bool:
        """この機体で WebUI からの更新を許しているか."""
        return self._settings.enabled

    def restart_units(self) -> tuple[str, ...]:
        """再起動対象（active な pcbasm unit）。プロセス生存中は 1 度だけ観測する.

        unit の構成が変わるのは install / 更新のときで、そのときはこのプロセス自身が
        再起動している。毎回のポーリングで `systemctl` を起こさないためにキャッシュする。
        """
        if self._units is None:
            try:
                self._units = active_units(self._settings)
            except OSError:
                # systemctl が無いホストでも `GET status` は 200 を返す（契約）
                self._units = ()
        return self._units

    def plan(self) -> UpdatePlan:
        """今の観測値（fetch はしない。ポーリングが毎秒ネットワークに出ないように）."""
        return self._plan()

    def check(self) -> UpdatePlan:
        """Remote を fetch してから観測する（更新の有無を確かめる操作）."""
        fetch_error = None
        if self._settings.enabled and not self.running:
            # 無認可で叩ける経路なので短く縛る（スレッドプールを長時間占有させない）
            fetch_error = fetch(
                self._settings, timeout=self._settings.check_fetch_timeout
            )
        return self._plan(fetch_error=fetch_error)

    def refresh_remote(self) -> str | None:
        """通知のために remote を取り込む（失敗理由を返す）.

        `check()` と違って観測値を組み立てない。`GET status` が返す `behind` を
        新しく保つためだけのもので、無効な機体と更新実行中は何もしない。
        """
        if not self._settings.enabled or self.running:
            return None
        return fetch(self._settings)

    def start_watching(self) -> None:
        """`watch_interval` ごとの `refresh_remote()` を始める（lifespan から呼ぶ）.

        最初の fetch は 1 周期待ってから行う（起動直後の I/O を避ける）。
        """
        if self._watch is not None or self._settings.watch_interval <= 0:
            return
        self._stop_watch.clear()
        self._watch = threading.Thread(
            target=self._watch_remote, name="pcbasm-update-watch", daemon=True
        )
        self._watch.start()

    def stop_watching(self) -> None:
        """定期 fetch を止めて合流する（lifespan の終了時に呼ぶ）."""
        watch, self._watch = self._watch, None
        if watch is None:
            return
        self._stop_watch.set()
        # 実行中の git fetch は待たない（daemon なのでプロセス終了を妨げない）
        watch.join(timeout=1.0)

    def restart_services(self) -> str | None:
        """更新せずに pcbasm の unit を再起動する（断る理由があれば返す）.

        ファームウェア再起動から呼ぶ「装置ごと立て直す」経路。**再起動対象も argv も
        更新時と同一**（`active_units()` + `restart_command`）なので、sudoers に許可を
        足す必要はない。

        更新と同じ flock を取る。`running` はこのプロセスの更新スレッドしか見ないが、
        同居機では backend と frontend が同じ `update.lock` を共有するので、**相方が
        `uv sync` の最中に unit を落とす**のを止められるのはロックだけ。ロックは
        再起動を投げ終えるまで握り続ける（先に返すと同じ窓が開き直す）。

        Returns:
            予約できたら None、できない理由があればその文字列
        """
        units = self.restart_units()
        if not units:
            return None
        with self._guard:
            if self.running:
                return (
                    "ソフトウェア更新の実行中です。"
                    "終わるまでサービスの再起動はできません。"
                )
            self._settings.state_dir.mkdir(parents=True, exist_ok=True)
            lock = self._acquire_lock()
            if lock is None:
                return (
                    "別のプロセスがソフトウェア更新を実行中です。"
                    "終わるまでサービスの再起動はできません。"
                )
            if reason := restart_permitted(self._settings, units):
                self._release(lock)
                return reason
            # リクエストスレッドから待たない（`schedule_restart` は応答を返し終えて
            # から投げるための遅延を持つ）
            threading.Thread(
                target=self._restart_holding,
                args=(lock, units),
                name="pcbasm-service-restart",
                daemon=True,
            ).start()
        return None

    def status(self) -> UpdateReport:
        """永続化された最後の report（未実行なら IDLE）."""
        return load_report(self._settings.report_path)

    @property
    def running(self) -> bool:
        """このプロセスで更新スレッドが動いているか."""
        return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout: float) -> bool:
        """実行中の更新が終わるまで待つ（テストと同期実行用）."""
        return self._finished.wait(timeout)

    def start(self, expected_head: str | None = None) -> tuple[str | None, str | None]:
        """更新を開始する（受理したら run_id、断るなら理由を返す）.

        Args:
            expected_head: 画面が見ていた HEAD。現在値と違えば断る（楽観ロック）。
                開きっぱなしの古いタブや `curl` 一発を弾くためのもので、認証ではない

        Returns:
            `(run_id, None)` か `(None, 断る理由)`
        """
        settings = self._settings
        if not settings.enabled:
            return None, (
                "この機体では WebUI からの更新が無効化されています"
                "（PCBASM_API_UPDATE_ENABLED / PCBASM_UI_UPDATE_ENABLED）。"
            )
        with self._guard:
            if self.running:
                return None, "更新が既に実行中です。"
            settings.state_dir.mkdir(parents=True, exist_ok=True)
            lock = self._acquire_lock()
            if lock is None:
                return None, (
                    "別のプロセスが更新を実行中です"
                    "（同居機では backend と UI frontend で 1 本ずつしか走りません）。"
                )
            state, reason = self._refuse(expected_head)
            if reason is not None:
                self._release(lock)
                return None, reason
            report = self._new_report(state)
            save_report(settings.report_path, report)
            self._finished.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(report, lock),
                name="pcbasm-update",
                daemon=True,
            )
            self._thread.start()
            return report.run_id, None

    # ---- internals ----

    def _restart_holding(self, lock: IO[str], units: tuple[str, ...]) -> None:
        """ロックを握ったまま再起動を投げる（更新を伴わない経路）.

        拒否されればこのプロセスは生き残るので、その事実をログに残す。更新と違って report
        には書かない（更新の記録を、更新でないものが上書きしないため）。
        """
        try:
            if reason := schedule_restart(self._settings, units):
                logger.warning("サービスの再起動に失敗しました: %s", reason)
        finally:
            self._release(lock)

    def _watch_remote(self) -> None:
        """周期ごとに remote を取り込む（失敗しても黙って次の周期を待つ）.

        通知のための補助経路なので、ネットワーク不通で例外を上げない
        （`fetch` は理由を返すだけで送出しない）。
        """
        while not self._stop_watch.wait(self._settings.watch_interval):
            self.refresh_remote()

    def _plan(self, *, fetch_error: str | None = None) -> UpdatePlan:
        state, error = capture_state(self._settings)
        return UpdatePlan(
            enabled=self._settings.enabled,
            repository=state,
            repository_error=error,
            blocker=None if state is None else fast_forward_blocker(state),
            restart_units=self.restart_units(),
            fetch_error=fetch_error,
        )

    def _refuse(self, expected_head: str | None) -> tuple[RepoState | None, str | None]:
        """開始前の門番（何もせず断れる事情をすべてここで見る）.

        観測した状態も返す。`_new_report` が同じ値を使うことで git をもう一度
        起こさずに済み、**判定した HEAD と report に残す HEAD が必ず一致する**。
        """
        state, error = capture_state(self._settings)
        if state is None:
            return None, error
        if expected_head is not None and expected_head != state.head:
            return state, (
                f"画面の表示が古くなっています（画面: {expected_head} / "
                f"現在: {state.head}）。再読み込みしてからやり直してください。"
            )
        return state, fast_forward_blocker(state)

    def _new_report(self, state: RepoState | None) -> UpdateReport:
        return UpdateReport(
            run_id=secrets.token_hex(6),
            state=UpdateState.RUNNING,
            from_head=state.head if state else None,
            from_subject=state.head_subject if state else None,
            restart_units=self.restart_units(),
            started_at=time.time(),
        )

    def _acquire_lock(self) -> IO[str] | None:
        """`state_dir/update.lock` を非ブロッキングで掴む（掴めなければ None）."""
        handle = self._settings.lock_path.open("w", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        return handle

    def _release(self, lock: IO[str]) -> None:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()

    def _run(self, report: UpdateReport, lock: IO[str]) -> None:
        try:
            self._execute(report)
        except Exception as exc:  # noqa: BLE001 - report に残さないと「更新中」で固まる
            # 例: 設定した絶対パスに uv が無い（Popen が FileNotFoundError）。
            # 握り潰すと merge 済みのまま running で止まり、画面は 180 秒待って諦める
            report.fail(f"更新中に想定外のエラーが発生しました: {exc!r}")
            self._save(report)
        finally:
            self._release(lock)
            self._finished.set()

    def _save(self, report: UpdateReport) -> None:
        save_report(self._settings.report_path, report)

    def _step(
        self,
        report: UpdateReport,
        step: UpdateStep,
        reason: str | None,
        detail: str = "",
    ) -> bool:
        """手順 1 つの結果を積んで永続化する（続行してよければ True）.

        保存を挟むのは、この直後にプロセスが死んでも「どこで止まったか」が残るように
        するため（メモリ上の記録は自己再起動で必ず失われる）。
        """
        report.record(step, reason, detail)
        self._save(report)
        return reason is None

    def _step_command(
        self, report: UpdateReport, step: UpdateStep, result: CommandResult, label: str
    ) -> bool:
        """外部コマンド 1 本の結果を report へ積む（成功なら True）."""
        if result.timed_out:
            reason = f"{label} が時間内に終わりませんでした。"
        elif not result.ok:
            reason = f"{label} が失敗しました（終了コード {result.returncode}）。"
        else:
            reason = None
        detail = tail(result.output, self._settings.output_tail_lines)
        return self._step(report, step, reason, detail)

    def _execute(self, report: UpdateReport) -> None:
        settings = self._settings
        units = report.restart_units

        # 1 sudoers 未設置なら pull する前に中断する（再起動しないホストでは不要）
        if not self._step(
            report, UpdateStep.PREFLIGHT, restart_permitted(settings, units)
        ):
            return

        # 2 fetch してから改めて中断事由を見る（開始判定との間に tree が動きうる）
        reason = fetch(settings)
        if reason is None:
            state, error = capture_state(settings)
            reason = error if state is None else fast_forward_blocker(state)
        if not self._step(report, UpdateStep.FETCH, reason):
            return

        # 3 fast-forward（git が自分で断った場合は HEAD も作業ツリーも動かない）
        if not self._step(report, UpdateStep.MERGE, merge_fast_forward(settings)):
            return
        merged, _ = capture_state(settings)
        report.to_head = merged.head if merged else None
        report.to_subject = merged.head_subject if merged else None

        # 4 依存の同期（失敗したら再起動しない = 起動不能を作らない）
        synced = run_command(
            sync_command(settings),
            cwd=settings.repo_root,
            env=git_env(),
            timeout=settings.sync_timeout,
        )
        if not self._step_command(report, UpdateStep.SYNC, synced, "uv sync"):
            return

        # 5 新リビジョンが import できるか（起動失敗で WebUI ごと到達不能になるのを防ぐ）。
        # 対象は **このホストが実際に動かすアプリだけ**（frontend 専用機で
        # web.api.app を読むと picamera2 / pcbnew が無くて必ず落ちる）
        modules = smoke_modules(units)
        if modules:
            smoke = run_command(
                smoke_command(settings, modules),
                cwd=settings.repo_root,
                env=git_env(),
                timeout=settings.smoke_timeout,
            )
            if not self._step_command(
                report, UpdateStep.SMOKE, smoke, "import チェック"
            ):
                return
        else:
            self._step(
                report,
                UpdateStep.SMOKE,
                None,
                "起動中の pcbasm サービスが無いため省略しました",
            )

        # unit 定義が古いままなら警告する（読み取りだけ。自動 install はしない）。
        # **警告の取得に失敗しても本流は止めない** — ここで例外を通すと
        # uv sync も smoke も通ったのに再起動されない（警告機能が更新を殺す）
        try:
            drifted = stale_units(settings, units)
        except OSError:
            drifted = ()
        if drifted:
            report.warnings = (*report.warnings, stale_unit_warning(drifted))

        # 6 ここまでの結果を残してから 7 再起動を予約する（順序を逆にしない）
        report.state = UpdateState.RESTARTING if units else UpdateState.SUCCEEDED
        report.finished_at = time.time()
        self._save(report)

        # 7 sudo に拒否されればこのプロセスは生き残るので、その事実を残す
        if reason := schedule_restart(settings, units):
            report.fail(reason)
            self._save(report)
