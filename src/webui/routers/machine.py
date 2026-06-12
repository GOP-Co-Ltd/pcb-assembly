"""マシン選択とアプリ状態の API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from webui.app import SettingsDep, StateDep, StoreDep
from webui.settings import Settings
from webui.state import AppState

router = APIRouter(prefix="/api")


class MachineSelect(BaseModel):
    name: str


class StateResponse(BaseModel):
    machine: str
    pcb_file: str | None
    busy: bool
    busy_owner: str | None
    focus_z: float | None
    mainsail_url: str


class MachinesResponse(BaseModel):
    machines: list[str]
    selected: str


def build_state_response(state: AppState, settings: Settings) -> StateResponse:
    """現在のアプリ状態から StateResponse を構築する."""
    owner = state.busy_owner
    pcb = state.selected_pcb
    return StateResponse(
        machine=state.selected_machine,
        pcb_file=pcb.as_posix() if pcb else None,
        busy=owner is not None,
        busy_owner=owner,
        focus_z=state.focus_z(),
        mainsail_url=settings.mainsail_url,
    )


@router.get("/state")
def get_state(state: StateDep, settings: SettingsDep) -> StateResponse:
    return build_state_response(state, settings)


@router.get("/machines")
def get_machines(state: StateDep, store: StoreDep) -> MachinesResponse:
    return MachinesResponse(
        machines=store.list_machines(), selected=state.selected_machine
    )


@router.get("/machine")
def get_machine(state: StateDep) -> MachineSelect:
    return MachineSelect(name=state.selected_machine)


@router.put("/machine")
def put_machine(body: MachineSelect, state: StateDep) -> MachineSelect:
    try:
        state.select_machine(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return MachineSelect(name=state.selected_machine)
