"""バックアップ zip のダウンロードと、設定履歴（machine.toml の世代）の一覧と復元の API."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from web.api.backup import build_backup
from web.api.deps import ControlDep, DevicesDep, EnvDep, LockDep, MachineConfigDep
from web.api.env import resolve_machine_id
from web.api.history import UnknownSnapshotError
from web.contract.models import ConfigHistoryResponse

router = APIRouter(prefix="/api/backup")


@router.get("")
def get_backup(env: EnvDep) -> Response:
    """設定と運用データの zip を返す（中身は `web.api.backup`）."""
    machine_id = resolve_machine_id(env)
    created_at = datetime.now()
    content = build_backup(env, machine_id=machine_id, created_at=created_at)
    filename = f"pcbasm-backup-{machine_id}-{created_at:%Y%m%d-%H%M%S}.zip"
    return Response(
        content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/history")
def get_config_history(machine_config: MachineConfigDep) -> ConfigHistoryResponse:
    """machine.toml の世代を新しい順に、次に入った変更の要約付きで返す."""
    return ConfigHistoryResponse(entries=machine_config.history_entries())


@router.post("/history/{name}/restore")
def restore_config_history(
    name: str,
    lock: LockDep,
    devices: DevicesDep,
    machine_config: MachineConfigDep,
    _control: ControlDep,
) -> ConfigHistoryResponse:
    """指定の世代を machine.toml へ書き戻し、書き戻した後の世代一覧を返す.

    書き戻す前の状態も世代に残る。カメラ設定も戻りうるので、カメラは常に再構築する。

    404: 世代が無い / 409: 装置が使用中。
    """
    with lock.hold("settings"):
        try:
            machine_config.restore(name)
        except UnknownSnapshotError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        devices.rebuild_camera()
    return ConfigHistoryResponse(entries=machine_config.history_entries())
