"""WebUI テスト共有フィクスチャ.

tmp_path に tests/data/config をコピーした Env を `create_app` に注入し、
`fastapi.testclient.TestClient` で実 HTTP 経路を検証する。

- `open_klipper` は root conftest が塞いでいる。Moonraker 不通の経路を確かめるテストは
  `unreachable_klipper` を要求する
- 通知音は `FakeAudioPlayer` を持つ装置を注入する。既定の `AlsaAudioPlayer` だと完了通知付き
  ジョブが実 `aplay` を起動して実スピーカーが鳴るため
- Env は env ではなく直接構築して注入する（env はプロセスグローバルで leak するため）
"""

import shutil
from collections.abc import Iterator
from pathlib import Path

import attrs
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcbasm.hal.fake import FakeAudioPlayer
from tests.support.paths import (
    CHECKERBOARD_IMAGE,
    FAKE_CAMERA_IMAGE,
    FILL_COVERAGE_PCB,
    LED_BLINKER_PCB,
)
from tests.support.web import copy_test_config, devices_with_audio
from web.api.app import create_app
from web.api.backend import Backend, open_backend
from web.api.env import Env
from web.api.machine_config import MachineConfigFile


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """`tests/data/config` を tmp_path へコピーした単一 config ディレクトリ."""
    return copy_test_config(tmp_path)


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
    # 許可サブツリーの prefix 兄弟（".../pcb" に対する ".../pcb-evil"）。許可判定を
    # 前方一致で書くと脱出できてしまう最も起こりやすい誤実装を留めるための素材
    sibling = root.parent / f"{root.name}-evil"
    sibling.mkdir()
    (sibling / "secret.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    return root


@pytest.fixture
def real_pcb_path(pcb_root: Path) -> Path:
    """実 KiCAD PCB fixture を pcb_browse_root へコピーし、相対パスを返す.

    PCB を読むジョブ（extract_pcb 等）が実 load_board で読むためのもの。要求されたテストでのみ pcb_root
    に追加されるため、 /api/selection/files の一覧テストには影響しない。
    """
    destination = pcb_root / "real" / "fill_coverage.kicad_pcb"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FILL_COVERAGE_PCB, destination)
    return Path("real/fill_coverage.kicad_pcb")


@pytest.fixture
def copper_pcb_path(pcb_root: Path) -> Path:
    """TOP 銅箔ゾーンを持つ実 PCB fixture を pcb_browse_root へコピーし、相対パスを返す.

    height_plane ジョブ（probe 点サンプリング）のテスト用（Phase 5）。
    """
    destination = pcb_root / "real" / "led_blinker.kicad_pcb"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(LED_BLINKER_PCB, destination)
    return Path("real/led_blinker.kicad_pcb")


@pytest.fixture
def webui_settings(tmp_path: Path, config_dir: Path, pcb_root: Path) -> Env:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return Env(
        config_dir=config_dir,
        data_dir=data_dir,
        pcb_browse_root=pcb_root,
        # 公開範囲はセキュリティ境界なので既定（リポジトリ + /media + /mnt）から
        # 暗黙に広がらない。tmp の pcb root を使うテストは明示的に許可する
        pcb_browse_allowed=(pcb_root,),
        pcb_browse_start=pcb_root,
        pcb_upload_dir=pcb_root / "uploads",
        # 実 LAN へ mDNS を撒かない（探索・広告はこの層のテスト対象ではない）
        discovery_enabled=False,
        # 自己更新の report / ロックをリポジトリの data/ に落とさない
        update_state_dir=tmp_path / "selfupdate",
    )


@pytest.fixture
def audio_player() -> FakeAudioPlayer:
    """実 ALSA に触らない AudioPlayer（再生要求とデバイス一覧の検証用）.

    テストごとに新しいインスタンスなので、`played` を検証するテストが他テストの
    再生要求に汚染されることはない。
    """
    return FakeAudioPlayer()


@pytest.fixture
def app(
    webui_settings: Env,
    audio_player: FakeAudioPlayer,
    paste_test_board_footprint_root: Path,
) -> FastAPI:
    return create_app(
        webui_settings,
        devices=devices_with_audio(webui_settings, audio_player),
        paste_test_board_footprint_root=paste_test_board_footprint_root,
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def backend(app: FastAPI, client: TestClient) -> Backend:
    """Lifespan 起動後の Backend（排他ロックの直接取得などに使う）."""
    return app.state.backend


@pytest.fixture
def machine_config(config_dir: Path) -> MachineConfigFile:
    """`tmp_path` にコピーした config ディレクトリの machine.toml を読み書きする."""
    return MachineConfigFile(config_dir)


@pytest.fixture
def camera_backend(
    fake_camera_settings: Env, audio_player: FakeAudioPlayer
) -> Iterator[Backend]:
    """FastAPI を組まずに作った、FakeCamera を使う実 Backend（frame 経路の検証に必要）."""
    backend = open_backend(
        fake_camera_settings,
        devices=devices_with_audio(fake_camera_settings, audio_player),
    )
    yield backend
    backend.close()


@pytest.fixture
def fake_camera_settings(webui_settings: Env) -> Env:
    """FakeCamera（固定画像アセット）を使う Env。preview / FrameHub 結合テスト用."""
    return attrs.evolve(webui_settings, fake_camera_image=FAKE_CAMERA_IMAGE)


@pytest.fixture
def fake_camera_app(
    fake_camera_settings: Env,
    audio_player: FakeAudioPlayer,
    paste_test_board_footprint_root: Path,
) -> FastAPI:
    return create_app(
        fake_camera_settings,
        devices=devices_with_audio(fake_camera_settings, audio_player),
        paste_test_board_footprint_root=paste_test_board_footprint_root,
    )


@pytest.fixture
def fake_camera_client(fake_camera_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(fake_camera_app) as test_client:
        yield test_client


@pytest.fixture
def fake_camera_backend(
    fake_camera_app: FastAPI, fake_camera_client: TestClient
) -> Backend:
    """Lifespan 起動後の Backend（fake camera 版）."""
    return fake_camera_app.state.backend


@pytest.fixture
def checkerboard_camera_settings(webui_settings: Env) -> Env:
    """チェッカーボード固定画像カメラの Env。camera_calibration ジョブの結合テスト用."""
    return attrs.evolve(webui_settings, fake_camera_image=CHECKERBOARD_IMAGE)


@pytest.fixture
def checkerboard_camera_app(
    checkerboard_camera_settings: Env,
    audio_player: FakeAudioPlayer,
    paste_test_board_footprint_root: Path,
) -> FastAPI:
    return create_app(
        checkerboard_camera_settings,
        devices=devices_with_audio(checkerboard_camera_settings, audio_player),
        paste_test_board_footprint_root=paste_test_board_footprint_root,
    )


@pytest.fixture
def checkerboard_camera_client(
    checkerboard_camera_app: FastAPI,
) -> Iterator[TestClient]:
    with TestClient(checkerboard_camera_app) as test_client:
        yield test_client
