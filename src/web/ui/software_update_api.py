"""UI host 自身の health と software update API."""

from __future__ import annotations

import subprocess

from fastapi import APIRouter, HTTPException, Request

from pcbasm.software_update import UpdateCoordinatorContract, UpdateStatus
from web.api.routers.software_update import (
    HealthResponse,
    SoftwareUpdateApplyRequest,
    UpdateRequestAccepted,
    validate_update_target,
)

router = APIRouter(prefix="/api")


def _coordinator(request: Request) -> UpdateCoordinatorContract | None:
    return request.app.state.update_coordinator


@router.get("/health")
def get_health(request: Request) -> HealthResponse:
    return HealthResponse(
        service="ui", revision=request.app.state.revision, status="ok"
    )


@router.get("/software-update")
def get_software_update(request: Request) -> UpdateStatus:
    coordinator = _coordinator(request)
    return coordinator.status() if coordinator is not None else UpdateStatus(role="ui")


@router.post("/software-update/check", status_code=202)
def post_software_update_check(request: Request) -> UpdateRequestAccepted:
    coordinator = _coordinator(request)
    if coordinator is None:
        raise HTTPException(status_code=503, detail="software update is not installed")
    try:
        return UpdateRequestAccepted(request_id=coordinator.request_check())
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/software-update/apply", status_code=202)
def post_software_update_apply(
    body: SoftwareUpdateApplyRequest, request: Request
) -> UpdateRequestAccepted:
    coordinator = _coordinator(request)
    if coordinator is None:
        raise HTTPException(status_code=503, detail="software update is not installed")
    validate_update_target(coordinator.status(), body)
    try:
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
