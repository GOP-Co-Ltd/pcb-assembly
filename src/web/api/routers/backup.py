"""バックアップ zip のダウンロード API."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import Response

from web.api.backup import build_backup
from web.api.dependencies import EnvDep
from web.api.env import resolve_machine_id

router = APIRouter(prefix="/api")


@router.get("/backup")
def get_backup(settings: EnvDep) -> Response:
    """設定と運用データの zip を返す（中身は `web.api.backup`）."""
    machine_id = resolve_machine_id(settings)
    created_at = datetime.now()
    content = build_backup(settings, machine_id=machine_id, created_at=created_at)
    filename = f"pcbasm-backup-{machine_id}-{created_at:%Y%m%d-%H%M%S}.zip"
    return Response(
        content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
