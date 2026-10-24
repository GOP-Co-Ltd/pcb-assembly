"""WebUI テスト共有フィクスチャ.

tmp_path に `data/testing/config` をコピーした Env を `create_app` に注入し、
`fastapi.testclient.TestClient` で実 HTTP 経路を検証する。

- `open_klipper` は root conftest が塞いでいる。Moonraker 不通の経路を確かめるテストは
  `unreachable_klipper` を要求する
- `data/testing/config/machine.toml` は WebUI / E2E 用（コメント保持・欠落キーの
  テスト素材を含む）。pcbasm コア層の単体テストが使う `data/testing/machine.toml` とは別物
- 通知音は `FakeAudioPlayer` を持つ装置を注入する。既定の `AlsaAudioPlayer` だと完了通知付き
  ジョブが実 `aplay` を起動して実スピーカーが鳴るため
- Env は env ではなく直接構築して注入する（env はプロセスグローバルで leak するため）
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

from pcbasm.hal.fake import FakeAudioPlayer
from pcbasm.vision import ImageArray
from tests.helpers import copy_testing_config
from tests.support.backend import devices_with_audio
from tests.support.paths import TESTING_DATA_DIR
from web.api.app import create_app
from web.api.backend import Backend, open_backend
from web.api.env import Env
from web.api.machine_config import MachineConfigFile

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


def _camera_settings(webui_settings: Env, image: Path) -> Env:
    """固定画像カメラ（FakeCamera）を有効化した Env を作る."""
    return attrs.evolve(webui_settings, fake_camera_image=image)


def _lifespan_client(app: FastAPI) -> Iterator[TestClient]:
    """Lifespan 起動込みの TestClient（client 系 fixture の共通形）."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """`data/testing/config` を tmp_path へコピーした単一 config ディレクトリ."""
    return copy_testing_config(tmp_path)


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
        mainsail_url="http://mainsail.invalid",
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
    yield from _lifespan_client(app)


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
    return _camera_settings(webui_settings, FAKE_CAMERA_IMAGE)


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
    yield from _lifespan_client(fake_camera_app)


@pytest.fixture
def fake_camera_backend(
    fake_camera_app: FastAPI, fake_camera_client: TestClient
) -> Backend:
    """Lifespan 起動後の Backend（fake camera 版）."""
    return fake_camera_app.state.backend


@pytest.fixture
def checkerboard_camera_settings(webui_settings: Env) -> Env:
    """チェッカーボード固定画像カメラの Env。camera_calibration ジョブの結合テスト用."""
    return _camera_settings(webui_settings, CHECKERBOARD_CAMERA_IMAGE)


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
    yield from _lifespan_client(checkerboard_camera_app)
