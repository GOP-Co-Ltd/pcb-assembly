"""更新の計画と結果（永続化される唯一の状態）と、その API 表現.

`UpdateReport` は **プロセスが死んでも残る唯一の状態**。更新は自分自身を再起動する
ので、実行中のメモリ上の記録は必ず失われる。ブラウザは再起動を跨いでこの JSON を
読み直し、`run_id` の一致と `to_head` への到達で「戻ってきた」ことを判定する。

API 表現（pydantic）への変換をここに置くのは、backend（`web.api.routers.update`）と
frontend（`web.ui.update_api`）が **同じ 1 つの変換**を使うため。表示文字列の組み立ては
すべてサーバ側で完結させる（`webui-thin-wrapper` 準拠）。
"""

from __future__ import annotations

import json
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

import attrs

from pcbasm.atomic import write_text_atomic
from web.api.models import (
    UpdateRepositoryInfo,
    UpdateRunInfo,
    UpdateStatusResponse,
    UpdateStepInfo,
)
from web.selfupdate.repo import RepoState
from web.selfupdate.service import restart_notice


class UpdateState(StrEnum):
    """更新全体の状態."""

    IDLE = "idle"
    RUNNING = "running"
    # 成功して再起動を予約した状態。自分が死ぬのでこれが実質の終状態
    RESTARTING = "restarting"
    # 再起動対象の unit が 1 つも動いていないホストでの終状態
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class UpdateStep(StrEnum):
    """更新の手順（実行順）."""

    PREFLIGHT = "preflight"
    FETCH = "fetch"
    MERGE = "merge"
    SYNC = "sync"
    SMOKE = "smoke"


STATE_LABELS: dict[UpdateState, str] = {
    UpdateState.IDLE: "未実行",
    UpdateState.RUNNING: "更新中",
    UpdateState.RESTARTING: "再起動しています",
    UpdateState.SUCCEEDED: "完了",
    UpdateState.FAILED: "失敗",
}

STEP_LABELS: dict[UpdateStep, str] = {
    UpdateStep.PREFLIGHT: "再起動権限の確認",
    UpdateStep.FETCH: "リモートの取得",
    UpdateStep.MERGE: "fast-forward 更新",
    UpdateStep.SYNC: "依存の同期（uv sync）",
    UpdateStep.SMOKE: "起動チェック（import）",
}


@attrs.define
class UpdateStepRecord:
    """手順 1 つの結果."""

    step: UpdateStep
    ok: bool
    detail: str = ""


@attrs.define
class UpdateReport:
    """更新 1 回分の記録（JSON へ往復する）."""

    run_id: str = ""
    state: UpdateState = UpdateState.IDLE
    step: UpdateStep | None = None
    steps: tuple[UpdateStepRecord, ...] = ()
    from_head: str | None = None
    from_subject: str | None = None
    to_head: str | None = None
    to_subject: str | None = None
    restart_units: tuple[str, ...] = ()
    error: str = ""
    # 更新は成功したが人手の対応が要ること（unit 定義の再 install など）
    warnings: tuple[str, ...] = ()
    started_at: float = 0.0
    finished_at: float | None = None

    @property
    def failed_detail(self) -> str:
        """失敗した手順の出力（無ければ空）."""
        return next((record.detail for record in self.steps if not record.ok), "")

    def fail(self, reason: str) -> None:
        """手順の外で起きた失敗（実行ファイル欠如など）を記録する."""
        self.state = UpdateState.FAILED
        self.error = reason
        self.finished_at = time.time()

    def record(self, step: UpdateStep, reason: str | None, detail: str = "") -> None:
        """手順 1 つの結果を積む（失敗なら state を FAILED にする）."""
        self.step = step
        self.steps = (*self.steps, UpdateStepRecord(step, reason is None, detail))
        if reason is not None:
            self.fail(reason)

    def as_dict(self) -> dict[str, Any]:
        """JSON へ落とせる素の dict にする."""
        return {
            "run_id": self.run_id,
            "state": self.state.value,
            "step": self.step.value if self.step is not None else None,
            "steps": [
                {"step": record.step.value, "ok": record.ok, "detail": record.detail}
                for record in self.steps
            ],
            "from_head": self.from_head,
            "from_subject": self.from_subject,
            "to_head": self.to_head,
            "to_subject": self.to_subject,
            "restart_units": list(self.restart_units),
            "error": self.error,
            "warnings": list(self.warnings),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> UpdateReport:
        """`as_dict` の逆（未知の値は既定へ落とす）."""
        step = payload.get("step")
        return cls(
            run_id=str(payload.get("run_id", "")),
            state=UpdateState(payload.get("state", UpdateState.IDLE.value)),
            step=UpdateStep(step) if step else None,
            steps=tuple(
                UpdateStepRecord(
                    UpdateStep(record["step"]),
                    bool(record["ok"]),
                    str(record.get("detail", "")),
                )
                for record in payload.get("steps", [])
            ),
            from_head=payload.get("from_head"),
            from_subject=payload.get("from_subject"),
            to_head=payload.get("to_head"),
            to_subject=payload.get("to_subject"),
            restart_units=tuple(payload.get("restart_units", [])),
            error=str(payload.get("error", "")),
            warnings=tuple(payload.get("warnings", [])),
            started_at=float(payload.get("started_at", 0.0)),
            finished_at=payload.get("finished_at"),
        )


def save_report(path: Path, report: UpdateReport) -> None:
    """Report を atomic に書き出す（読み手が途中状態を見ないように）."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(report.as_dict(), ensure_ascii=False, indent=2))


def load_report(path: Path) -> UpdateReport:
    """永続化された report を読む（未実行・壊れている場合は IDLE を返す）."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return UpdateReport()
    if not isinstance(payload, dict):
        return UpdateReport()
    try:
        return UpdateReport.from_dict(payload)
    except (KeyError, TypeError, ValueError):
        return UpdateReport()


@attrs.frozen
class UpdatePlan:
    """「今このホストで更新を押すと何が起きるか」の観測値."""

    enabled: bool
    repository: RepoState | None
    repository_error: str | None
    blocker: str | None
    restart_units: tuple[str, ...]
    fetch_error: str | None = None

    @property
    def update_available(self) -> bool:
        """押せば実際に前へ進むか（既に最新・中断事由ありなら False）."""
        return (
            self.enabled
            and self.repository is not None
            and self.blocker is None
            and not self.repository.up_to_date
        )


def repository_payload(plan: UpdatePlan) -> UpdateRepositoryInfo:
    """リポジトリ観測値の API 表現."""
    state = plan.repository
    if state is None:
        return UpdateRepositoryInfo(
            error=plan.repository_error or "状態を取得できません"
        )
    return UpdateRepositoryInfo(
        head=state.head,
        head_subject=state.head_subject,
        head_label=f"{state.head} {state.head_subject}".strip(),
        branch=state.branch,
        upstream=state.upstream,
        upstream_head=state.upstream_head,
        ahead=state.ahead,
        behind=state.behind,
        dirty_paths=list(state.dirty_paths),
        untracked_paths=list(state.untracked_paths),
        up_to_date=state.up_to_date,
    )


def run_payload(report: UpdateReport) -> UpdateRunInfo:
    """更新結果の API 表現（表示ラベルもここで付ける）."""
    return UpdateRunInfo(
        run_id=report.run_id,
        state=report.state.value,
        state_label=STATE_LABELS[report.state],
        step=report.step.value if report.step is not None else None,
        steps=[
            UpdateStepInfo(
                step=record.step.value,
                label=STEP_LABELS[record.step],
                ok=record.ok,
                detail=record.detail,
            )
            for record in report.steps
        ],
        from_head=report.from_head,
        from_subject=report.from_subject,
        to_head=report.to_head,
        to_subject=report.to_subject,
        restart_units=list(report.restart_units),
        error=report.error,
        failed_detail=report.failed_detail,
        warnings=list(report.warnings),
        started_at=report.started_at or None,
        finished_at=report.finished_at,
    )


def summary_text(plan: UpdatePlan) -> str:
    """リポジトリが**どういう状態か**を 1 行にまとめる（表示文字列はサーバが組む）.

    更新できない理由は `blocker` が別に持つ。ここで blocker をそのまま返すと、
    画面で同じ文が要約と警告の 2 箇所に出る。

    追従先が無いときだけは件数を語れない（`behind` が 0 のままなので「最新です」が
    嘘になる）ので、状態としてそれを述べる。
    """
    if not plan.enabled:
        return "この機体では WebUI からの更新が無効です。"
    state = plan.repository
    if state is None:
        return plan.repository_error or "リポジトリの状態を取得できません。"
    if state.upstream is None:
        return f"追従先が設定されていません（{state.head} {state.head_subject}）。"
    if state.up_to_date:
        return f"最新です（{state.head} {state.head_subject}）。"
    return (
        f"{state.behind} 件の更新があります"
        f"（{state.head} → {state.upstream_head}、{state.upstream}）。"
    )


def status_payload(
    plan: UpdatePlan, report: UpdateReport, *, hostname: str
) -> UpdateStatusResponse:
    """`GET /api/update/status` 相当の全体表現（backend / frontend 共通）."""
    return UpdateStatusResponse(
        enabled=plan.enabled,
        hostname=hostname,
        summary=summary_text(plan),
        repository=repository_payload(plan),
        blocker=plan.blocker,
        fetch_error=plan.fetch_error,
        update_available=plan.update_available,
        restart_units=list(plan.restart_units),
        restart_notice=restart_notice(plan.restart_units),
        run=run_payload(report),
    )
