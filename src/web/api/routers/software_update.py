"""Backend の health と software update HTTP 境界."""

from __future__ import annotations

import subprocess

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from pcbasm.software_update import UpdateCoordinatorContract, UpdateStatus
from web.api.dependencies import ControlDep, StateDep

router = APIRouter(prefix="/api")


class SoftwareUpdateApplyRequest(BaseModel):
    """操作者が確認画面で固定した target だけを受け取る."""

    model_config = ConfigDict(extra="forbid")

    expected_branch: str
    expected_revision: str
    confirmed: bool
    branch_confirmation: str | None = None


class HealthResponse(BaseModel):
    service: str
    revision: str
    status: str = "ok"


class UpdateRequestAccepted(BaseModel):
    request_id: str


def _coordinator(request: Request) -> UpdateCoordinatorContract | None:
    return request.app.state.update_coordinator


def _disabled_status(role: str) -> UpdateStatus:
    return UpdateStatus(role=role)


def validate_update_target(
    status: UpdateStatus, body: SoftwareUpdateApplyRequest
) -> None:
    if not body.confirmed:
        raise HTTPException(status_code=409, detail="明示確認が必要です")
    if status.running:
        raise HTTPException(status_code=409, detail="更新処理が実行中です")
    if (
        status.branch != body.expected_branch
        or status.available != body.expected_revision
    ):
        raise HTTPException(
            status_code=409, detail="branch または revision が確認時点から変わりました"
        )
    if status.blockers:
        raise HTTPException(status_code=409, detail=status.message)
    if status.branch_change:
        if body.branch_confirmation != status.branch:
            raise HTTPException(status_code=409, detail=status.message)
    elif not status.can_apply:
        raise HTTPException(status_code=409, detail=status.message)


def _request_check(
    coordinator: UpdateCoordinatorContract | None,
) -> UpdateRequestAccepted:
    if coordinator is None:
        raise HTTPException(status_code=503, detail="software update is not installed")
    try:
        return UpdateRequestAccepted(request_id=coordinator.request_check())
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/health")
def get_health(request: Request) -> HealthResponse:
    return HealthResponse(
        service="api", revision=request.app.state.revision, status="ok"
    )


@router.get("/software-update")
def get_software_update(request: Request) -> UpdateStatus:
    coordinator = _coordinator(request)
    return coordinator.status() if coordinator is not None else _disabled_status("api")


@router.post("/software-update/check", status_code=202)
def post_software_update_check(request: Request) -> UpdateRequestAccepted:
    return _request_check(_coordinator(request))


@router.post("/software-update/apply", status_code=202)
def post_software_update_apply(
    body: SoftwareUpdateApplyRequest,
    request: Request,
    state: StateDep,
    _control: ControlDep,
) -> UpdateRequestAccepted:
    coordinator = _coordinator(request)
    if coordinator is None:
        raise HTTPException(status_code=503, detail="software update is not installed")
    status = coordinator.status()
    validate_update_target(status, body)
    try:
        with state.machine_lock("software-update"):
            request_id = coordinator.request_apply(
                expected_branch=body.expected_branch,
                expected_revision=body.expected_revision,
                confirmed=body.confirmed,
                branch_confirmation=body.branch_confirmation,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return UpdateRequestAccepted(request_id=request_id)
