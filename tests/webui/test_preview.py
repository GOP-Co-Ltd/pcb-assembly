"""`webui.preview.PreviewService` の仕様テスト.

計画書「`src/webui/preview.py`」節 + spec §7 が契約:

- mjpeg_stream は完全な multipart パート（boundary 行 + ヘッダ + JPEG + CRLF）を
  yield する同期ジェネレータ。開始で acquire（hub.start）、close で release
  （参照カウント 0 で hub.stop）
- オーバーレイ: crosshair（緑十字 + crop 枠）/ copper（エッジ緑重畳）/
  circle（検出円の赤描画）
- submit_override はジョブ提供フレームを override_ttl 秒だけ優先配信する
- snapshot は acquire → 1 フレーム → release（hub は停止に戻る）
- カメラ構築失敗は伝播する（ルーター層が 503 化）
"""

import time

import attrs
import numpy as np
import pytest

from pcbasm.vision import Image, ImageArray
from webui.config_store import ConfigStore
from webui.preview import PreviewService
from webui.settings import Settings
from webui.state import AppState

from .conftest import decode_jpeg, jpeg_payload


@pytest.fixture
def state(fake_camera_settings: Settings, configs_root) -> AppState:
    return AppState(fake_camera_settings, ConfigStore(configs_root))


@pytest.fixture
def service(state: AppState) -> PreviewService:
    return PreviewService(state)


def _decoded_frame(part: bytes) -> ImageArray:
    """Multipart パートを検証つきで BGR 配列へ復号する."""
    assert part.startswith(b"--frame\r\n")
    assert b"Content-Type: image/jpeg" in part
    frame = decode_jpeg(jpeg_payload(part))
    assert frame is not None
    return frame


def _count_dominant(frame: ImageArray, channel: int, margin: int = 60) -> int:
    """指定チャネルが他 2 チャネルより margin 以上強い画素数（BGR 順）."""
    planes = [frame[..., i].astype(int) for i in range(3)]
    target = planes.pop(channel)
    return int(((target - planes[0] > margin) & (target - planes[1] > margin)).sum())


class TestMjpegStream:
    """MJPEG 配信と参照カウント."""

    def test_stream_yields_decodable_multipart_jpeg_parts(
        self, service: PreviewService
    ):
        stream = service.mjpeg_stream("none")
        try:
            parts = [next(stream) for _ in range(3)]
        finally:
            stream.close()

        for part in parts:
            assert b"Content-Length:" in part
            assert _decoded_frame(part).shape == (720, 1280, 3)

    def test_reference_count_tracks_stream_lifecycle(
        self, service: PreviewService, state: AppState
    ):
        stream = service.mjpeg_stream("none")
        next(stream)

        assert service.client_count == 1
        assert state.frame_hub().running

        stream.close()

        assert service.client_count == 0
        assert not state.frame_hub().running

    def test_parallel_streams_share_hub_until_last_close(
        self, service: PreviewService, state: AppState
    ):
        stream_a = service.mjpeg_stream("none")
        stream_b = service.mjpeg_stream("none")
        next(stream_a)
        next(stream_b)
        assert service.client_count == 2

        stream_a.close()

        assert service.client_count == 1
        assert state.frame_hub().running

        stream_b.close()

        assert service.client_count == 0
        assert not state.frame_hub().running

    def test_camera_construction_failure_propagates(
        self, fake_camera_settings: Settings, configs_root, tmp_path
    ):
        settings = attrs.evolve(
            fake_camera_settings, fake_camera_image=tmp_path / "missing.png"
        )
        state = AppState(settings, ConfigStore(configs_root))
        service = PreviewService(state)

        stream = service.mjpeg_stream("none")

        with pytest.raises(FileNotFoundError):
            next(stream)


class TestOverlays:
    """オーバーレイ描画（描画有無のスモーク。座標の厳密検証はしない）."""

    def test_crosshair_overlay_draws_green_marks(self, service: PreviewService):
        stream = service.mjpeg_stream("crosshair")
        try:
            frame = _decoded_frame(next(stream))
        finally:
            stream.close()

        assert _count_dominant(frame, channel=1) > 100

    def test_copper_overlay_marks_edges_green(self, service: PreviewService):
        # canny 既定値は machine.toml の pad_align（81 / 192）。固定画像の
        # 矩形・直線パターンのエッジが緑で重畳される
        stream = service.mjpeg_stream("copper")
        try:
            frame = _decoded_frame(next(stream))
        finally:
            stream.close()

        assert _count_dominant(frame, channel=1) > 500

    def test_copper_overlay_accepts_canny_override(self, service: PreviewService):
        stream = service.mjpeg_stream("copper", canny_low=50.0, canny_high=150.0)
        try:
            frame = _decoded_frame(next(stream))
        finally:
            stream.close()

        assert _count_dominant(frame, channel=1) > 500

    def test_circle_overlay_draws_red_circle(self, service: PreviewService):
        # 固定画像の直径 120px 円が検出され、赤の円描画が乗る
        stream = service.mjpeg_stream("circle")
        try:
            frame = _decoded_frame(next(stream))
        finally:
            stream.close()

        assert _count_dominant(frame, channel=2) > 100


class TestOverrideSlot:
    """ジョブ用オーバーライドスロット（Phase 3 の ctx.frame の受け口）."""

    def test_override_frame_takes_priority(self, state: AppState):
        service = PreviewService(state, override_ttl=0.2)
        magenta = Image(np.full((720, 1280, 3), (255, 0, 255), dtype=np.uint8))
        stream = service.mjpeg_stream("none")
        try:
            next(stream)
            service.submit_override(magenta)
            frame = _decoded_frame(next(stream))
        finally:
            stream.close()

        assert frame[..., 0].mean() > 200  # B
        assert frame[..., 1].mean() < 50  # G
        assert frame[..., 2].mean() > 200  # R

    def test_override_expires_after_ttl(self, state: AppState):
        service = PreviewService(state, override_ttl=0.2)
        magenta = Image(np.full((720, 1280, 3), (255, 0, 255), dtype=np.uint8))
        stream = service.mjpeg_stream("none")
        try:
            next(stream)
            service.submit_override(magenta)
            time.sleep(0.25)
            frame = _decoded_frame(next(stream))
        finally:
            stream.close()

        # 生フレーム（明るい無彩色の基板風画像）に戻っている
        assert frame[..., 1].mean() > 100


class TestSnapshot:
    """スポット確認用の 1 枚取得."""

    def test_snapshot_returns_jpeg_and_releases_hub(
        self, service: PreviewService, state: AppState
    ):
        data = service.snapshot("none")

        frame = decode_jpeg(data)
        assert frame is not None
        assert frame.shape == (720, 1280, 3)
        assert service.client_count == 0
        assert not state.frame_hub().running
