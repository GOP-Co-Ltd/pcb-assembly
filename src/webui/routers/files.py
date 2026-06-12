"""PCB ファイルブラウザの API."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel

from webui.app import JobsDep, PreviewDep, SettingsDep, StateDep
from webui.routers.machine import StateResponse, build_state_response

router = APIRouter(prefix="/api")

PCB_SUFFIX = ".kicad_pcb"


class FileEntry(BaseModel):
    name: str
    type: Literal["dir", "file"]


class FilesResponse(BaseModel):
    path: str
    entries: list[FileEntry]


class PcbFileSelect(BaseModel):
    path: str


def _resolve_under_root(root: Path, rel: str) -> Path:
    """Root 配下の絶対パスへ解決する.

    Raises:
        HTTPException: 絶対パス指定または root 範囲外（traversal）の場合（400）
    """
    candidate = Path(rel)
    if candidate.is_absolute():
        raise HTTPException(status_code=400, detail=f"絶対パスは指定できません: {rel}")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise HTTPException(status_code=400, detail=f"範囲外のパスです: {rel}")
    return resolved


@router.get("/files")
def list_files(settings: SettingsDep, path: str = "") -> FilesResponse:
    root = settings.pcb_browse_root.resolve()
    directory = _resolve_under_root(root, path)
    if not directory.is_dir():
        raise HTTPException(
            status_code=404, detail=f"ディレクトリが存在しません: {path}"
        )

    entries = [
        FileEntry(name=child.name, type="dir" if child.is_dir() else "file")
        for child in sorted(directory.iterdir(), key=lambda p: p.name)
        if child.is_dir() or child.suffix == PCB_SUFFIX
    ]
    rel = "" if directory == root else directory.relative_to(root).as_posix()
    return FilesResponse(path=rel, entries=entries)


@router.put("/pcb-file")
def put_pcb_file(
    body: PcbFileSelect,
    state: StateDep,
    settings: SettingsDep,
    preview: PreviewDep,
    jobs: JobsDep,
) -> StateResponse:
    root = settings.pcb_browse_root.resolve()
    resolved = _resolve_under_root(root, body.path)
    if resolved.suffix != PCB_SUFFIX:
        raise HTTPException(
            status_code=400,
            detail=f"{PCB_SUFFIX} ファイルを指定してください: {body.path}",
        )
    if not resolved.is_file():
        raise HTTPException(
            status_code=404, detail=f"ファイルが存在しません: {body.path}"
        )
    state.select_pcb(resolved.relative_to(root))
    jobs.publish_state_changed()
    return build_state_response(state, settings, preview, jobs)


@router.post("/pcb-file/upload", status_code=201)
async def upload_pcb_file(
    file: UploadFile,
    state: StateDep,
    settings: SettingsDep,
    preview: PreviewDep,
    jobs: JobsDep,
) -> StateResponse:
    """PCB ファイルを pcb_upload_dir に保存し、そのまま選択する."""
    # Path(...).name でディレクトリ成分を落とす（traversal 防止）
    filename = Path(file.filename or "").name
    if not filename or not filename.endswith(PCB_SUFFIX):
        raise HTTPException(
            status_code=400,
            detail=f"{PCB_SUFFIX} ファイルをアップロードしてください: {file.filename}",
        )
    root = settings.pcb_browse_root.resolve()
    upload_dir = settings.pcb_upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)
    destination = upload_dir.resolve() / filename
    destination.write_bytes(await file.read())
    state.select_pcb(destination.relative_to(root))
    jobs.publish_state_changed()
    return build_state_response(state, settings, preview, jobs)
