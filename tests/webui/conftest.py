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

import attrs
import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcbasm.vision import ImageArray
from tests.helpers import PROJECT_ROOT, TESTING_DATA_DIR
from webui.app import create_app
from webui.settings import Settings
from webui.state import AppState

TEST_FIXTURE_DIR = PROJECT_ROOT / "configs" / "test-fixture"

FAKE_CAMERA_IMAGE = TESTING_DATA_DIR / "webui" / "fake_camera.png"

# 5x5 内部コーナー・1 マス約 66.7px のチェッカーボード（400x400）。
# square_size=10mm で pixel_per_mm ≈ 6.67（tests/pcbasm/vision/test_calibration.py
# と同一素材）。camera_calibration ジョブのフル結合テストに使う
CHECKERBOARD_CAMERA_IMAGE = TESTING_DATA_DIR / "checkerboard.png"

REAL_PCB_FIXTURE = TESTING_DATA_DIR / "fill_coverage" / "fill_coverage.kicad_pcb"

# TOP 銅箔ゾーンを持つ実 PCB（fill_coverage は pads のみで銅箔ゾーンが無く、
# height_plane の probe 点サンプリングが成立しない。Phase 5 で追加）
COPPER_PCB_FIXTURE = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"


def jpeg_payload(part: bytes) -> bytes:
    """MJPEG の 1 パート（boundary 行 + ヘッダ + JPEG + CRLF）から JPEG bytes を取り出す."""
    _, _, body = part.partition(b"\r\n\r\n")
    return body.rstrip(b"\r\n")


def decode_jpeg(data: bytes) -> ImageArray | None:
    """JPEG bytes を BGR 配列に復号する（復号できなければ None）."""
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)


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
def real_pcb_path(pcb_root: Path) -> Path:
    """実 KiCAD PCB fixture を pcb_browse_root へコピーし、相対パスを返す.

    PCB を読むジョブ（extract_pcb 等）が実 PcbFile を 読むためのもの。要求されたテストでのみ pcb_root
    に追加されるため、 /api/files の一覧テストには影響しない。
    """
    destination = pcb_root / "real" / "fill_coverage.kicad_pcb"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(REAL_PCB_FIXTURE, destination)
    return Path("real/fill_coverage.kicad_pcb")


@pytest.fixture
def copper_pcb_path(pcb_root: Path) -> Path:
    """TOP 銅箔ゾーンを持つ実 PCB fixture を pcb_browse_root へコピーし、相対パスを返す.

    height_plane ジョブ（probe 点サンプリング）のテスト用（Phase 5）。
    """
    destination = pcb_root / "real" / "led_blinker.kicad_pcb"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(COPPER_PCB_FIXTURE, destination)
    return Path("real/led_blinker.kicad_pcb")


@pytest.fixture
def webui_settings(tmp_path: Path, configs_root: Path, pcb_root: Path) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return Settings(
        configs_root=configs_root,
        data_dir=data_dir,
        pcb_browse_root=pcb_root,
        pcb_browse_start=pcb_root,
        pcb_upload_dir=pcb_root / "uploads",
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
def fake_camera_settings(webui_settings: Settings) -> Settings:
    """FixedImageCamera（固定画像アセット）を使う Settings。preview / FrameHub 結合テスト用."""
    return attrs.evolve(
        webui_settings, fake_camera=True, fake_camera_image=FAKE_CAMERA_IMAGE
    )


@pytest.fixture
def fake_camera_app(fake_camera_settings: Settings) -> FastAPI:
    return create_app(fake_camera_settings)


@pytest.fixture
def fake_camera_client(fake_camera_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(fake_camera_app) as test_client:
        yield test_client


@pytest.fixture
def fake_camera_appstate(
    fake_camera_app: FastAPI, fake_camera_client: TestClient
) -> AppState:
    """Lifespan 起動後の AppState（fake camera 版）."""
    return fake_camera_app.state.appstate


@pytest.fixture
def checkerboard_camera_settings(webui_settings: Settings) -> Settings:
    """チェッカーボード固定画像カメラの Settings。camera_calibration ジョブの結合テスト用."""
    return attrs.evolve(
        webui_settings, fake_camera=True, fake_camera_image=CHECKERBOARD_CAMERA_IMAGE
    )


@pytest.fixture
def checkerboard_camera_app(checkerboard_camera_settings: Settings) -> FastAPI:
    return create_app(checkerboard_camera_settings)


@pytest.fixture
def checkerboard_camera_client(
    checkerboard_camera_app: FastAPI,
) -> Iterator[TestClient]:
    with TestClient(checkerboard_camera_app) as test_client:
        yield test_client


@pytest.fixture
def real_settings(tmp_path: Path) -> Settings:
    """実機（実 Moonraker, kurousagi）向け Settings。`@mark_hardware` テスト専用."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return Settings(
        configs_root=PROJECT_ROOT / "configs",
        data_dir=data_dir,
        pcb_browse_root=PROJECT_ROOT,
        default_machine="kurousagi",
    )


@pytest.fixture
def real_client(real_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(real_settings)) as test_client:
        yield test_client
