"""PCB の閲覧・選択・アップロードの API.

`pcb_browse_root` は `board_id` の算出基準なので `/` 固定だが、実際に読める範囲は
`Env.pcb_browse_allowed` のサブツリーに限る。許可サブツリーへ辿り着くための
祖先ディレクトリは列挙だけ許す（`/` から `/media` の USB を選べるようにするため）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel

from web.api.deps import BackendDep, ControlDep, EnvDep, IdentityDep
from web.api.env import Env
from web.api.routers.session import build_state_response
from web.contract.models import StateResponse

router = APIRouter(prefix="/api/selection")

PCB_SUFFIX = ".kicad_pcb"


class FileEntry(BaseModel):
    name: str
    type: Literal["dir", "file"]


class FilesResponse(BaseModel):
    path: str
    entries: list[FileEntry]


class PcbFileSelect(BaseModel):
    path: str


def _allowed_roots(env: Env) -> tuple[Path, ...]:
    """公開を許可するサブツリーの絶対パス（symlink 解決済み）."""
    return tuple(allowed.resolve() for allowed in env.pcb_browse_allowed)


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


def _resolve_browsable(env: Env, rel: str) -> Path:
    """Root 配下へ解決し、許可サブツリーの内側であることを検証する.

    Raises:
        HTTPException: 許可サブツリー外の場合（400）
    """
    resolved = _resolve_under_root(env.pcb_browse_root.resolve(), rel)
    if not _is_allowed(resolved, _allowed_roots(env)):
        raise HTTPException(
            status_code=400, detail=f"公開が許可されていないパスです: {rel}"
        )
    return resolved


@router.get("/files")
def list_files(env: EnvDep, path: str = "") -> FilesResponse:
    """ディレクトリと ``.kicad_pcb`` ファイルを列挙する（``path`` は pcb_browse_root からの相対）.

    400: 絶対パス・範囲外・許可サブツリー外 / 404: ディレクトリが無い。
    """
    root = env.pcb_browse_root.resolve()
    allowed = _allowed_roots(env)
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
        # 許可サブツリーへ辿る途中のディレクトリ。中身は見せず、許可サブツリーへ続く子だけ出す
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
    env: EnvDep,
    backend: BackendDep,
    identity: IdentityDep,
    _control: ControlDep,
) -> StateResponse:
    """既存の PCB ファイルを選択し、更新後のアプリ状態を返す.

    400: 許可サブツリー外・拡張子不正 / 404: ファイルが無い / 409: 装置が使用中。
    """
    root = env.pcb_browse_root.resolve()
    resolved = _resolve_browsable(env, body.path)
    if resolved.suffix != PCB_SUFFIX:
        raise HTTPException(
            status_code=400,
            detail=f"{PCB_SUFFIX} ファイルを指定してください: {body.path}",
        )
    if not resolved.is_file():
        raise HTTPException(
            status_code=404, detail=f"ファイルが存在しません: {body.path}"
        )
    backend.selection.select(resolved.relative_to(root))
    backend.events.publish_state_changed()
    return build_state_response(backend, identity)


@router.post("/pcb-file/upload", status_code=201)
async def upload_pcb_file(
    file: UploadFile,
    env: EnvDep,
    backend: BackendDep,
    identity: IdentityDep,
    _control: ControlDep,
) -> StateResponse:
    """PCB ファイルを pcb_upload_dir に保存し、そのまま選択する."""
    # Path(...).name でディレクトリ成分を除く（traversal 防止）
    filename = Path(file.filename or "").name
    if not filename or not filename.endswith(PCB_SUFFIX):
        raise HTTPException(
            status_code=400,
            detail=f"{PCB_SUFFIX} ファイルをアップロードしてください: {file.filename}",
        )
    root = env.pcb_browse_root.resolve()
    upload_dir = env.pcb_upload_dir.resolve()
    if not _is_allowed(upload_dir, _allowed_roots(env)):
        raise HTTPException(
            status_code=400,
            detail=f"アップロード先が公開範囲外です: {upload_dir}",
        )
    # 保存先のファイル自体が公開範囲外への symlink の場合も、書き込み前に拒否する。
    destination = _resolve_browsable(
        env, (upload_dir / filename).relative_to(root).as_posix()
    )
    backend.selection.select(destination.relative_to(root), content=await file.read())
    backend.events.publish_state_changed()
    return build_state_response(backend, identity)
