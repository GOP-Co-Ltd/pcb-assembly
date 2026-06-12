"""`webui.routers.preview` の仕様テスト.

計画書「`src/webui/routers/preview.py`」節 + spec §7 / §9 が契約:

- GET /api/preview/stream → MJPEG（multipart/x-mixed-replace; boundary=frame）。
  無限ストリームなので必ず上限付きで読む
- GET /api/preview/snapshot → image/jpeg 1 枚
- overlay 不正値は 422、カメラ構築失敗は 503
- GET /api/state の preview_clients がストリーム接続数を反映する

注: 本環境の starlette TestClient はレスポンスを完全受信してから返すため、
無限 MJPEG ストリームを TestClient で読むとハングする。ストリーム系のテストは
実 uvicorn サーバー（エフェメラルポート）+ httpx で検証する。
"""

import threading
import time
from collections.abc import Iterator

import attrs
import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from tests.webui.conftest import decode_jpeg, jpeg_payload
from webui.app import create_app
from webui.settings import Settings


@pytest.fixture
def broken_camera_client(
    fake_camera_settings: Settings, tmp_path
) -> Iterator[TestClient]:
    """fake_camera_image が不存在の Settings で構築したクライアント（503 検証用）."""
    settings = attrs.evolve(
        fake_camera_settings, fake_camera_image=tmp_path / "missing.png"
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def live_server_url(fake_camera_settings: Settings) -> Iterator[str]:
    """実 uvicorn サーバーのベース URL（MJPEG ストリーミング検証用）."""
    config = uvicorn.Config(
        create_app(fake_camera_settings),
        host="127.0.0.1",
        port=0,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn が時間内に起動しなかった")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10.0)


def _read_stream(
    base_url: str, path: str, boundary_count: int = 2
) -> tuple[str, bytes]:
    """MJPEG ストリームを boundary_count 個の boundary まで読んで切断する."""
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        with client.stream("GET", path) as response:
            assert response.status_code == 200
            content_type = response.headers["content-type"]
            data = b""
            for chunk in response.iter_bytes():
                data += chunk
                if data.count(b"--frame") >= boundary_count:
                    break
    return content_type, data


def _first_jpeg(data: bytes) -> bytes:
    """ストリームデータから最初の完全なパートの JPEG bytes を取り出す."""
    part = data.split(b"--frame")[1]
    return jpeg_payload(part)


class TestPreviewStream:
    """GET /api/preview/stream."""

    def test_stream_returns_decodable_mjpeg_multipart(self, live_server_url: str):
        content_type, data = _read_stream(
            live_server_url, "/api/preview/stream?overlay=crosshair"
        )

        assert content_type == "multipart/x-mixed-replace; boundary=frame"
        assert b"Content-Type: image/jpeg" in data
        frame = decode_jpeg(_first_jpeg(data))
        assert frame is not None
        assert frame.shape == (720, 1280, 3)

    def test_copper_stream_accepts_canny_query(self, live_server_url: str):
        _, data = _read_stream(
            live_server_url,
            "/api/preview/stream?overlay=copper&canny_low=50&canny_high=150",
        )

        assert decode_jpeg(_first_jpeg(data)) is not None

    def test_unknown_overlay_returns_422(self, fake_camera_client: TestClient):
        response = fake_camera_client.get("/api/preview/stream?overlay=bogus")

        assert response.status_code == 422

    def test_camera_failure_returns_503(self, broken_camera_client: TestClient):
        response = broken_camera_client.get("/api/preview/stream")

        assert response.status_code == 503


class TestSnapshot:
    """GET /api/preview/snapshot."""

    def test_snapshot_returns_decodable_jpeg(self, fake_camera_client: TestClient):
        response = fake_camera_client.get("/api/preview/snapshot")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        frame = decode_jpeg(response.content)
        assert frame is not None
        assert frame.shape == (720, 1280, 3)

    def test_snapshot_accepts_overlay_and_canny_query(
        self, fake_camera_client: TestClient
    ):
        response = fake_camera_client.get(
            "/api/preview/snapshot?overlay=copper&canny_low=50&canny_high=150"
        )

        assert response.status_code == 200
        assert decode_jpeg(response.content) is not None

    def test_unknown_overlay_returns_422(self, fake_camera_client: TestClient):
        response = fake_camera_client.get("/api/preview/snapshot?overlay=bogus")

        assert response.status_code == 422

    def test_camera_failure_returns_503(self, broken_camera_client: TestClient):
        response = broken_camera_client.get("/api/preview/snapshot")

        assert response.status_code == 503


class TestPreviewClients:
    """GET /api/state の preview_clients（spec §9）."""

    def test_state_reports_streaming_client_count(self, live_server_url: str):
        with httpx.Client(base_url=live_server_url, timeout=10.0) as client:
            assert client.get("/api/state").json()["preview_clients"] == 0

            with client.stream("GET", "/api/preview/stream") as response:
                next(response.iter_bytes())  # 配信開始を確実にする
                assert client.get("/api/state").json()["preview_clients"] == 1

            # 切断後のサーバー側クリーンアップは非同期に走るため短時間ポーリングする
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if client.get("/api/state").json()["preview_clients"] == 0:
                    break
                time.sleep(0.05)
            assert client.get("/api/state").json()["preview_clients"] == 0
