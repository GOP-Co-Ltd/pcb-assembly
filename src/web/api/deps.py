"""FastAPI の依存取得（Backend からサービスを取り出す型エイリアス）."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from starlette.requests import HTTPConnection

from pcbasm.machine.devices import Devices
from pcbasm.pasting.overrides import BoardOverrides
from pcbasm.pasting.test_board.generator import BoardGenerator
from web.api.backend import Backend
from web.api.camera import CameraFeed
from web.api.control import ClientIdentity, ControlLease, LeaseInfo
from web.api.env import Env
from web.api.identity import get_identity
from web.api.jobs.catalog import JobCatalog
from web.api.jobs.manager import JobManager
from web.api.lock import MachineLock
from web.api.machine_config import MachineConfigFile
from web.api.selection import Selection
from web.selfupdate.runner import UpdateRunner


def get_backend(connection: HTTPConnection) -> Backend:
    """アプリの Backend（HTTP と WebSocket の両方から引ける）."""
    return connection.app.state.backend


def _env(request: Request) -> Env:
    return get_backend(request).env


def _machine_config(request: Request) -> MachineConfigFile:
    return get_backend(request).machine_config


def _devices(request: Request) -> Devices:
    return get_backend(request).devices


def _lock(request: Request) -> MachineLock:
    return get_backend(request).lock


def _selection(request: Request) -> Selection:
    return get_backend(request).selection


def _camera(request: Request) -> CameraFeed:
    return get_backend(request).camera


def _jobs(request: Request) -> JobManager:
    return get_backend(request).jobs


def _catalog(request: Request) -> JobCatalog:
    return get_backend(request).catalog


def _overrides(request: Request) -> BoardOverrides:
    return get_backend(request).overrides


def _board_generator(request: Request) -> BoardGenerator:
    return get_backend(request).board_generator


def _control(request: Request) -> ControlLease:
    return get_backend(request).control


def _update(request: Request) -> UpdateRunner:
    return get_backend(request).update


def require_control(request: Request) -> LeaseInfo:
    """操作権を検証する（空きなら取得する。変更系エンドポイント用）.

    **必ず `Depends`（= `ControlDep`）として使う。**

    Raises:
        ControlDeniedError: 他クライアントが操作権を保持している場合（``web.api.errors``
            が 423 Locked へ変換する）
    """
    return get_backend(request).control.claim(get_identity(request))


BackendDep = Annotated[Backend, Depends(get_backend)]
EnvDep = Annotated[Env, Depends(_env)]
MachineConfigDep = Annotated[MachineConfigFile, Depends(_machine_config)]
DevicesDep = Annotated[Devices, Depends(_devices)]
LockDep = Annotated[MachineLock, Depends(_lock)]
SelectionDep = Annotated[Selection, Depends(_selection)]
CameraDep = Annotated[CameraFeed, Depends(_camera)]
JobsDep = Annotated[JobManager, Depends(_jobs)]
CatalogDep = Annotated[JobCatalog, Depends(_catalog)]
OverridesDep = Annotated[BoardOverrides, Depends(_overrides)]
BoardGeneratorDep = Annotated[BoardGenerator, Depends(_board_generator)]
LeaseDep = Annotated[ControlLease, Depends(_control)]
# 自己更新のランナー（ホストに 1 つ。装置ドメインではなくデプロイ運用）
UpdateDep = Annotated[UpdateRunner, Depends(_update)]
IdentityDep = Annotated[ClientIdentity, Depends(get_identity)]
# 変更系エンドポイントの認可。ハンドラ本体に入る前に 423 で断る
ControlDep = Annotated[LeaseInfo, Depends(require_control)]
