"""WebUI テスト共有フィクスチャ.

tmp_path に `configs/test-fixture` をコピーした Settings を `create_app` に注入し、
`fastapi.testclient.TestClient` で実 HTTP 経路を検証する。

- 既定選択マシンを計画書どおり "kurousagi" にするため、test-fixture のコピーを
  "kurousagi" という名前でも複製する（どちらも Klipper port 7126 = 非リッスンで、
  テストが誤って実機 Moonraker に接続しない）
- 実機系（`@mark_hardware`）はリポジトリの実 `configs/`（kurousagi, port 7125）を使う
"""

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.helpers import PROJECT_ROOT
from webui.app import create_app
from webui.settings import Settings
from webui.state import AppState

TEST_FIXTURE_DIR = PROJECT_ROOT / "configs" / "test-fixture"


@pytest.fixture
def configs_root(tmp_path: Path) -> Path:
    """Test-fixture を "kurousagi" と "test-fixture" の 2 マシンとしてコピーした configs
    ルート."""
    root = tmp_path / "configs"
    shutil.copytree(TEST_FIXTURE_DIR, root / "kurousagi")
    shutil.copytree(TEST_FIXTURE_DIR, root / "test-fixture")
    return root


@pytest.fixture
def pcb_root(tmp_path: Path) -> Path:
    """PCB ファイルブラウザ用のディレクトリツリー."""
    root = tmp_path / "pcb"
    (root / "boards").mkdir(parents=True)
    (root / "boards" / "sample.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    (root / "boards" / "notes.txt").write_text("not a pcb", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "top.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    (tmp_path / "outside.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    return root


@pytest.fixture
def webui_settings(tmp_path: Path, configs_root: Path, pcb_root: Path) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return Settings(
        configs_root=configs_root,
        data_dir=data_dir,
        pcb_browse_root=pcb_root,
        printer_cfg_link=tmp_path / "printer_data" / "config" / "printer.cfg",
        mainsail_url="http://mainsail.invalid",
        default_machine="kurousagi",
    )


@pytest.fixture
def app(webui_settings: Settings) -> FastAPI:
    return create_app(webui_settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def appstate(app: FastAPI, client: TestClient) -> AppState:
    """Lifespan 起動後の AppState（排他ロックの直接取得などに使う）."""
    return app.state.appstate


@pytest.fixture
def real_settings(tmp_path: Path) -> Settings:
    """実機（実 Moonraker, kurousagi）向け Settings。`@mark_hardware` テスト専用."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return Settings(
        configs_root=PROJECT_ROOT / "configs",
        data_dir=data_dir,
        pcb_browse_root=PROJECT_ROOT,
        printer_cfg_link=Path.home() / "printer_data" / "config" / "printer.cfg",
        default_machine="kurousagi",
    )


@pytest.fixture
def real_client(real_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(real_settings)) as test_client:
        yield test_client
