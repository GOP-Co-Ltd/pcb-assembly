"""PCB ファイルブラウザの API.

`pcb_browse_root` は `board_id` の算出基準なので `/` 固定だが、実際に読める範囲は
`Settings.pcb_browse_allowed` のサブツリーに限る。許可サブツリーへ辿り着くための
祖先ディレクトリは列挙だけ許す（`/` から `/media` の USB を選べるようにするため）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel

from web.api.dependencies import (
    ControlDep,
    IdentityDep,
    JobsDep,
    PreviewDep,
    SettingsDep,
    StateDep,
)
from web.api.routers.common import StateResponse, build_state_response
from web.api.settings import Settings

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


def _allowed_roots(settings: Settings) -> tuple[Path, ...]:
    """公開を許可するサブツリーの絶対パス（symlink 解決済み）."""
    return tuple(allowed.resolve() for allowed in settings.pcb_browse_allowed)


def _is_allowed(resolved: Path, allowed: tuple[Path, ...]) -> bool:
    """許可サブツリーの内側か（= 中身を読める / 選択できる）."""
    return any(resolved.is_relative_to(root) for root in allowed)


def _leads_to_allowed(resolved: Path, allowed: tuple[Path, ...]) -> bool:
    """許可サブツリー自身か、そこへ辿る途中の祖先ディレクトリか（列挙のみ許す）."""
    return any(root.is_relative_to(resolved) for root in allowed)


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


def _resolve_browsable(settings: Settings, rel: str) -> Path:
    """Root 配下へ解決し、許可サブツリーの内側であることを検証する.

    Raises:
        HTTPException: 許可サブツリー外の場合（400）
    """
    resolved = _resolve_under_root(settings.pcb_browse_root.resolve(), rel)
    if not _is_allowed(resolved, _allowed_roots(settings)):
        raise HTTPException(
            status_code=400, detail=f"公開が許可されていないパスです: {rel}"
        )
    return resolved


@router.get("/files")
def list_files(settings: SettingsDep, path: str = "") -> FilesResponse:
    root = settings.pcb_browse_root.resolve()
    allowed = _allowed_roots(settings)
    directory = _resolve_under_root(root, path)
    inside = _is_allowed(directory, allowed)
    if not inside and not _leads_to_allowed(directory, allowed):
        raise HTTPException(
            status_code=400, detail=f"公開が許可されていないパスです: {path}"
        )
    if not directory.is_dir():
        raise HTTPException(
            status_code=404, detail=f"ディレクトリが存在しません: {path}"
        )

    children = sorted(directory.iterdir(), key=lambda p: p.name)
    if not inside:
        # 許可サブツリーへ辿る途中のディレクトリ。中身は見せず、続きの道だけ出す
        children = [
            child
            for child in children
            if child.is_dir() and _leads_to_allowed(child, allowed)
        ]
    entries = [
        FileEntry(name=child.name, type="dir" if child.is_dir() else "file")
        for child in children
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
    identity: IdentityDep,
    control: ControlDep,
) -> StateResponse:
    root = settings.pcb_browse_root.resolve()
    resolved = _resolve_browsable(settings, body.path)
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
    return build_state_response(state, settings, preview, jobs, control, identity)


@router.post("/pcb-file/upload", status_code=201)
async def upload_pcb_file(
    file: UploadFile,
    state: StateDep,
    settings: SettingsDep,
    preview: PreviewDep,
    jobs: JobsDep,
    identity: IdentityDep,
    control: ControlDep,
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
    upload_dir = settings.pcb_upload_dir.resolve()
    if not _is_allowed(upload_dir, _allowed_roots(settings)):
        raise HTTPException(
            status_code=400,
            detail=f"アップロード先が公開範囲外です: {upload_dir}",
        )
    # 保存先のファイル自体が公開範囲外への symlink の場合も、書き込み前に拒否する。
    destination = _resolve_browsable(
        settings, (upload_dir / filename).relative_to(root).as_posix()
    )
    state.select_pcb(destination.relative_to(root), content=await file.read())
    jobs.publish_state_changed()
    return build_state_response(state, settings, preview, jobs, control, identity)
