"""機体の設定と運用データのバックアップ zip.

中身は次のとおり。symlink は辿って実体を入れる（printer.cfg は Klipper 側の実ファイル）。

- config/: machine.toml、machines.toml、設定履歴、カメラ校正、printer.cfg
- data/webui/board_settings/ と data/webui/webui_state.json: 基板ごとの塗布設定、選択中の PCB とジョブ既定値
- uploads/: アップロードした PCB（board_settings が参照する原本）
- data/paste-volume-calibrations/: 塗布量校正
- manifest.json: 機体名、作成時刻、旧パス（data/board_settings、data/webui_state.json）の有無とファイル一覧
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

from web.api.env import Settings


def build_backup(settings: Settings, *, machine_id: str, created_at: datetime) -> bytes:
    """バックアップ zip の中身を返す."""
    webui = settings.webui_data_dir
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        _add_tree(archive, settings.config_dir, "config")
        _add_tree(archive, webui / "board_settings", "data/webui/board_settings")
        _add_file(archive, webui / "webui_state.json", "data/webui/webui_state.json")
        _add_tree(archive, settings.pcb_upload_dir, "uploads")
        _add_tree(
            archive,
            settings.paste_volume_calibration_dir,
            "data/paste-volume-calibrations",
        )
        manifest = {
            "machine_id": machine_id,
            "created_at": created_at.isoformat(timespec="seconds"),
            "legacy_paths": _legacy_paths(settings.data_dir),
        }
        archive.writestr(
            "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
        )
    return buffer.getvalue()


def _legacy_paths(data_dir: Path) -> dict[str, list[str] | None]:
    """旧パスごとのファイル一覧（無ければ None）."""
    board_settings = data_dir / "board_settings"
    state = data_dir / "webui_state.json"
    return {
        "data/board_settings": (
            sorted(path.name for path in board_settings.iterdir())
            if board_settings.is_dir()
            else None
        ),
        "data/webui_state.json": [state.name] if state.is_file() else None,
    }


def _add_tree(archive: zipfile.ZipFile, root: Path, arcroot: str) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            archive.write(path, f"{arcroot}/{path.relative_to(root).as_posix()}")


def _add_file(archive: zipfile.ZipFile, path: Path, arcname: str) -> None:
    if path.is_file():
        archive.write(path, arcname)
