"""マシン選択とアプリ状態の API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from webui.dependencies import JobsDep, PreviewDep, SettingsDep, StateDep, StoreDep
from webui.routers.common import StateResponse, build_state_response

router = APIRouter(prefix="/api")


class MachineSelect(BaseModel):
    name: str


class MachinesResponse(BaseModel):
    machines: list[str]
    selected: str


@router.get("/state")
def get_state(
    state: StateDep, settings: SettingsDep, preview: PreviewDep, jobs: JobsDep
) -> StateResponse:
    return build_state_response(state, settings, preview, jobs)


@router.get("/machines")
def get_machines(state: StateDep, store: StoreDep) -> MachinesResponse:
    return MachinesResponse(
        machines=store.list_machines(), selected=state.selected_machine
    )


@router.get("/machine")
def get_machine(state: StateDep) -> MachineSelect:
    return MachineSelect(name=state.selected_machine)


@router.put("/machine")
def put_machine(body: MachineSelect, state: StateDep, jobs: JobsDep) -> MachineSelect:
    try:
        state.select_machine(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    jobs.publish_state_changed()
    return MachineSelect(name=state.selected_machine)
