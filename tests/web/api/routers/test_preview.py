"""`web.api.routers.preview` の仕様テスト.

計画書「`src/webui/routers/preview.py`」節 + spec §7 / §9 が契約:

- overlay 不正値は 422、カメラ構築失敗は 503

注: 本環境の starlette TestClient はレスポンスを完全受信してから返すため、
無限 MJPEG ストリームを TestClient で読むとハングする。ストリーム配信・
preview_clients の実挙動は実 uvicorn の e2e
（tests/e2e/test_api_e2e.py::TestPreviewOverRealHttp）で検証する。
"""

from collections.abc import Iterator

import attrs
import pytest
from fastapi.testclient import TestClient

from web.api.app import create_app
from web.api.settings import Settings


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


class TestPreviewStream:
    """GET /api/preview/stream（エラー経路。配信本体は e2e）."""

    def test_unknown_overlay_returns_422(self, fake_camera_client: TestClient):
        response = fake_camera_client.get("/api/preview/stream?overlay=bogus")

        assert response.status_code == 422

    def test_camera_failure_returns_503(self, broken_camera_client: TestClient):
        response = broken_camera_client.get("/api/preview/stream")

        assert response.status_code == 503
