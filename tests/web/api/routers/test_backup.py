"""`web.api.routers.backup` の仕様テスト.

GET /api/backup は、ssh 無しで機体の設定と運用データを持ち出すための zip を返す。

- config/ を丸ごと入れる。printer.cfg などの symlink は辿って実体を入れる
- 基板ごとの塗布設定、選択状態、アップロードした PCB、塗布量校正を入れる
- manifest.json に旧パス（data/board_settings、data/webui_state.json）の有無とファイル一覧を書く
"""

import io
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from web.api.env import Settings


def _backup(client: TestClient) -> zipfile.ZipFile:
    response = client.get("/api/backup")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith("attachment;")
    return zipfile.ZipFile(io.BytesIO(response.content))


class TestBackup:
    def test_contains_config_and_operational_data(
        self, client: TestClient, webui_settings: Settings, tmp_path: Path
    ):
        config_dir = webui_settings.config_dir
        printer_cfg = tmp_path / "printer_data" / "printer.cfg"
        printer_cfg.parent.mkdir()
        printer_cfg.write_text("[printer]\n")
        (config_dir / "printer.cfg").unlink(missing_ok=True)
        (config_dir / "printer.cfg").symlink_to(printer_cfg)
        client.put(
            "/api/settings/machine",
            json={"values": {"paste_dispenser.motion.max_fill_speed": 0.9}},
        )
        board_settings = webui_settings.webui_data_dir / "board_settings"
        board_settings.mkdir(parents=True)
        (board_settings / "0123456789abcdef.json").write_text("{}")
        client.put("/api/pcb-file", json={"path": "boards/sample.kicad_pcb"})
        webui_settings.pcb_upload_dir.mkdir(parents=True)
        (webui_settings.pcb_upload_dir / "board.kicad_pcb").write_text("(kicad_pcb)")
        calibrations = webui_settings.paste_volume_calibration_dir
        calibrations.mkdir(parents=True)
        (calibrations / "a.paste-volume.json").write_text("{}")

        archive = _backup(client)

        names = set(archive.namelist())
        assert {
            "config/machine.toml",
            "data/webui/board_settings/0123456789abcdef.json",
            "data/webui/webui_state.json",
            "uploads/board.kicad_pcb",
            "data/paste-volume-calibrations/a.paste-volume.json",
            "manifest.json",
        } <= names
        assert archive.read("config/printer.cfg") == b"[printer]\n"
        assert any(name.startswith("config/history/machine-") for name in names)

    def test_manifest_lists_legacy_paths(
        self, client: TestClient, webui_settings: Settings
    ):
        legacy = webui_settings.data_dir / "board_settings"
        legacy.mkdir()
        (legacy / "fedcba9876543210.json").write_text("{}")

        manifest = json.loads(_backup(client).read("manifest.json"))

        assert manifest["legacy_paths"] == {
            "data/board_settings": ["fedcba9876543210.json"],
            "data/webui_state.json": None,
        }
