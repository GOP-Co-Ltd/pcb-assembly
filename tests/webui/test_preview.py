"""`webui.preview.PreviewService` の仕様テスト.

計画書「`src/webui/preview.py`」節 + spec §7 が契約:

- mjpeg_stream は完全な multipart パート（boundary 行 + ヘッダ + JPEG + CRLF）を
  yield する同期ジェネレータ。開始で acquire（hub.start）、close で release
  （参照カウント 0 で hub.stop）
- オーバーレイ: crosshair（緑十字 + crop 枠）/ copper（エッジ緑重畳）/
  circle（検出円の赤描画）
- submit_override はジョブ提供フレームを override_ttl 秒だけ優先配信する。
  persist=True の場合は clear_override まで優先する
- カメラ構築失敗は伝播する（ルーター層が 503 化）

計画書 memory/agents/implementation-planner/webui-camera-calib.md「設計判断 b」
「公開インターフェース案 2」が追加契約:

- crosshair の crop 枠はストリーム開始時の 1 回読みではなく、フレーム毎に
  machine.toml `[camera.crop]` を読む。ストリーム継続中の crop 変更が
  再接続なしで次フレームの枠位置へ反映される（circle / copper は対象外）
"""

import attrs
import numpy as np
import pytest

from pcbasm.vision import Image, ImageArray
from tests.helpers import TESTING_DATA_DIR
from webui.config_store import ConfigStore
from webui.preview import PreviewService
from webui.settings import Settings
from webui.state import AppState

from .conftest import decode_jpeg, jpeg_payload

REFLECTIVE_BOARD_IMAGE = (
    TESTING_DATA_DIR / "vision" / "copper" / "reflective_board_0.png"
)


@pytest.fixture
def service(state: AppState) -> PreviewService:
    return PreviewService(state)


@pytest.fixture
def reflective_board_service(fake_camera_settings: Settings, store: ConfigStore):
    """実撮像 PNG を FixedImageCamera で配信する PreviewService."""
    settings = attrs.evolve(
        fake_camera_settings,
        fake_camera_image=REFLECTIVE_BOARD_IMAGE,
    )
    state = AppState(settings, store)
    try:
        yield PreviewService(state)
    finally:
        state.close()


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


def _green_dominant_count_near_column(
    frame: ImageArray, x: int, tolerance: int = 3, margin: int = 60
) -> int:
    """X 近傍（±tolerance 列）の緑ドミナント画素数（JPEG ノイズ許容）."""
    band = frame[:, max(0, x - tolerance) : x + tolerance + 1]
    return _count_dominant(band, channel=1, margin=margin)


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

    def test_shutdown_request_ends_stream_and_releases_camera(
        self, service: PreviewService, state: AppState
    ):
        stream = service.mjpeg_stream("none")
        next(stream)

        service.request_shutdown()

        with pytest.raises(StopIteration):
            next(stream)
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
        self, fake_camera_settings: Settings, config_dir, tmp_path
    ):
        settings = attrs.evolve(
            fake_camera_settings, fake_camera_image=tmp_path / "missing.png"
        )
        state = AppState(settings, ConfigStore(config_dir))
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

    def test_crosshair_crop_change_reflects_in_next_frame_without_reconnect(
        self, service: PreviewService, store: ConfigStore
    ):
        """ストリーム継続中の crop 変更が、再接続なしで次フレームの ROI 枠へ反映される.

        計画書「設計判断 b」: crosshair のレンダラはフレーム毎に machine.toml `[camera.crop]`
        を読む。フレーム 1280x720・crop 600 の 既定枠左辺は x=340、crop 200 に変えると x=540
        付近へ移る （テスト観点「crosshair ストリーム継続中の crop 変更が次フレームの ROI 枠へ反映」）。
        """
        stream = service.mjpeg_stream("crosshair")
        try:
            before = _decoded_frame(next(stream))
            assert _green_dominant_count_near_column(before, x=340) > 50

            store.write_machine_settings(
                {"camera.crop.width": 200, "camera.crop.height": 200}
            )

            after = _decoded_frame(next(stream))
            assert _green_dominant_count_near_column(after, x=540) > 50
        finally:
            stream.close()

    def test_copper_overlay_marks_edges_green(self, service: PreviewService):
        # canny 既定値は machine.toml の pad_align（81 / 192）。固定画像の
        # 矩形・直線パターンのエッジが緑で重畳される
        stream = service.mjpeg_stream("copper")
        try:
            frame = _decoded_frame(next(stream))
        finally:
            stream.close()

        assert _count_dominant(frame, channel=1) > 500

    def test_copper_overlay_accepts_preprocessing_overrides(
        self, service: PreviewService
    ):
        stream = service.mjpeg_stream(
            "copper",
            canny_low=50.0,
            canny_high=150.0,
            sharpen_amount=1.2,
        )
        try:
            frame = _decoded_frame(next(stream))
        finally:
            stream.close()

        assert _count_dominant(frame, channel=1) > 500

    def test_copper_overlay_uses_gray_processed_background_and_green_edges(
        self, reflective_board_service: PreviewService
    ):
        stream = reflective_board_service.mjpeg_stream("copper")
        try:
            frame = _decoded_frame(next(stream))
        finally:
            stream.close()

        planes = frame.astype(int)
        green = (planes[..., 1] - planes[..., 0] > 60) & (
            planes[..., 1] - planes[..., 2] > 60
        )
        assert np.any(green)

        # JPEG の色にじみがあるエッジ近傍を含めても、背景の大部分は
        # processed のグレースケール（B/G/R がほぼ同値）である。
        background_chroma = np.ptp(planes[~green], axis=1)
        assert np.quantile(background_chroma, 0.95) < 15

    def test_copper_overlay_reflects_config_and_query_sharpen_amount(
        self,
        reflective_board_service: PreviewService,
        store: ConfigStore,
    ):
        store.write_machine_settings({"paste_dispenser.pad_align.sharpen_amount": 0.0})
        config_zero = reflective_board_service.mjpeg_stream("copper")
        try:
            zero_frame = _decoded_frame(next(config_zero))
        finally:
            config_zero.close()

        query_two = reflective_board_service.mjpeg_stream("copper", sharpen_amount=2.0)
        try:
            query_frame = _decoded_frame(next(query_two))
        finally:
            query_two.close()

        store.write_machine_settings({"paste_dispenser.pad_align.sharpen_amount": 2.0})
        config_two = reflective_board_service.mjpeg_stream("copper")
        try:
            config_frame = _decoded_frame(next(config_two))
        finally:
            config_two.close()

        assert not np.array_equal(zero_frame, query_frame)
        assert np.array_equal(config_frame, query_frame)

    def test_circle_overlay_draws_red_circle(self, service: PreviewService):
        # 固定画像の直径 120px 円が検出され、赤の円描画が乗る
        stream = service.mjpeg_stream("circle")
        try:
            frame = _decoded_frame(next(stream))
        finally:
            stream.close()

        assert _count_dominant(frame, channel=2) > 100


class _ManualClock:
    """テストが明示的に進める単調クロック（実時間 sleep への依存を排除する）."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class TestOverrideSlot:
    """ジョブ用オーバーライドスロット（Phase 3 の ctx.frame の受け口）."""

    def test_override_frame_takes_priority(self, state: AppState):
        clock = _ManualClock()
        service = PreviewService(state, override_ttl=0.2, clock=clock)
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
        clock = _ManualClock()
        service = PreviewService(state, override_ttl=0.2, clock=clock)
        magenta = Image(np.full((720, 1280, 3), (255, 0, 255), dtype=np.uint8))
        stream = service.mjpeg_stream("none")
        try:
            next(stream)
            service.submit_override(magenta)
            clock.advance(0.21)
            frame = _decoded_frame(next(stream))
        finally:
            stream.close()

        # 生フレーム（明るい無彩色の基板風画像）に戻っている
        assert frame[..., 1].mean() > 100

    def test_persistent_override_stays_until_cleared(self, state: AppState):
        clock = _ManualClock()
        service = PreviewService(state, override_ttl=0.05, clock=clock)
        magenta = Image(np.full((720, 1280, 3), (255, 0, 255), dtype=np.uint8))
        stream = service.mjpeg_stream("none")
        try:
            next(stream)
            service.submit_override(magenta, persist=True)
            clock.advance(1.0)  # TTL を大きく跨いでも persist が優先される
            persisted = _decoded_frame(next(stream))

            service.clear_override()
            cleared = _decoded_frame(next(stream))
        finally:
            stream.close()

        assert persisted[..., 0].mean() > 200
        assert persisted[..., 1].mean() < 50
        assert persisted[..., 2].mean() > 200
        assert cleared[..., 1].mean() > 100


class TestHoldCamera:
    """ジョブ用のカメラ保持（Phase 4: JobContext.open_camera の受け口）.

    計画書 webui-phase4.md「src/webui/preview.py」節が契約: hold_camera は
    参照カウントを保持して FrameHub を貸し出し（0→1 で start、1→0 で stop）、 MJPEG
    ストリームと同一カウントを共有する。
    """

    def test_hold_starts_hub_and_stops_on_exit(
        self, service: PreviewService, state: AppState
    ):
        with service.hold_camera() as hub:
            assert hub.running
            assert state.frame_hub().running

        assert not state.frame_hub().running

    def test_hold_keeps_hub_running_after_stream_closes(
        self, service: PreviewService, state: AppState
    ):
        """Preview クライアントの切断でジョブ使用中の hub は止まらない."""
        stream = service.mjpeg_stream("none")
        next(stream)

        with service.hold_camera():
            stream.close()
            assert state.frame_hub().running  # ジョブが保持している間は稼働

        assert not state.frame_hub().running

    def test_stream_keeps_hub_running_after_hold_exits(
        self, service: PreviewService, state: AppState
    ):
        """逆方向: ジョブ解放後も preview クライアントが残れば hub は止まらない."""
        stream = service.mjpeg_stream("none")
        next(stream)

        with service.hold_camera():
            pass

        assert state.frame_hub().running
        stream.close()
        assert not state.frame_hub().running

    def test_nested_holds_release_only_at_zero(
        self, service: PreviewService, state: AppState
    ):
        with service.hold_camera():
            with service.hold_camera():
                assert state.frame_hub().running
            assert state.frame_hub().running

        assert not state.frame_hub().running

    def test_camera_construction_failure_propagates(
        self, fake_camera_settings: Settings, config_dir, tmp_path
    ):
        """カメラ初期化失敗は伝播する（ジョブ側で FAILED 化される）."""
        settings = attrs.evolve(
            fake_camera_settings, fake_camera_image=tmp_path / "missing.png"
        )
        state = AppState(settings, ConfigStore(config_dir))
        service = PreviewService(state)

        with pytest.raises(FileNotFoundError):
            with service.hold_camera():
                pass
