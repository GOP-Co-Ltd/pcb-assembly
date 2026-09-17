"""高さ計測ジョブのプレビュー・確認・接続エラー処理。"""

from __future__ import annotations

from pathlib import Path

import cv2

from tests.web.api.conftest import decode_jpeg, jpeg_payload
from tests.web.api.jobs.conftest import WaitUntil, answer_next_prompt
from web.api.board_settings import BoardSettingsStore
from web.api.jobs.catalog import JobCatalog
from web.api.jobs.manager import JobManager, JobStatus
from web.api.preview import PreviewService
from web.api.settings import Settings
from web.api.state import AppState


def _preview_frame(preview: PreviewService):
    """プレビュー MJPEG の 1 フレームを公開経路から取得する."""
    stream = preview.mjpeg_stream("none")
    try:
        frame = decode_jpeg(jpeg_payload(next(stream)))
    finally:
        stream.close()
    assert frame is not None
    return frame


class TestHeightPlaneFrontFlow:
    """height_plane の計測前フロー（装置なし・FakeCamera + テスト用 config）.

    fill_coverage は TOP 銅箔ゾーンを持たないため、サンプリング可能な led_blinker
    fixture（copper_pcb_path）を使う。
    """

    def test_planned_points_artifact_and_confirm_false_aborts(
        self,
        manager: JobManager,
        state: AppState,
        fake_camera_settings: Settings,
        copper_pcb_path: Path,
        wait_until: WaitUntil,
    ):
        """計画点 PNG 生成 + diagnostics / /artifacts/ log → confirm False で中止.

        - confirm 待ちの時点で artifacts_dir/planned_points.png が生成済みで
          cv2 で復号可能（実行中配信の前提。計画書「_run_height_plane」節 2-4）
        - prompt は confirm（default True）でメッセージに点数の確認を含む
        """
        state.select_pcb(copper_pcb_path)
        record = manager.start("height_plane", {})
        # matplotlib の初回描画があるため長めに待つ
        wait_until(lambda: record.pending_prompt is not None, timeout=60.0)

        pending = record.pending_prompt
        assert pending is not None
        prompt_id, spec = pending
        assert spec.kind == "confirm"
        assert spec.default is True
        assert "計測します" in spec.message

        png_path = (
            fake_camera_settings.webui_data_dir / record.id / "planned_points.png"
        )
        image = cv2.imread(str(png_path))
        assert image is not None
        assert image.size > 0

        log_text = "\n".join(record.log_lines)
        assert "/artifacts/" in log_text  # 計画点 PNG へのリンク行
        assert "planned_points.png" in log_text
        assert "min_clearance" in log_text  # sampling diagnostics の log

        manager.respond_prompt(prompt_id, False)
        wait_until(lambda: record.status.terminal, timeout=60.0)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.ABORTED
        assert record.apply_available is False

    def test_planned_points_preview_stays_visible_while_confirm_waits(
        self,
        catalog: JobCatalog,
        state: AppState,
        fake_camera_settings: Settings,
        board_store: BoardSettingsStore,
        copper_pcb_path: Path,
        wait_until: WaitUntil,
    ):
        """Confirm 待ち中は planned_points プレビューを TTL で生カメラに戻さない."""
        preview = PreviewService(state, override_ttl=0.0)
        manager = JobManager(state, preview, catalog, fake_camera_settings, board_store)
        state.select_pcb(copper_pcb_path)
        record = manager.start("height_plane", {})
        try:
            wait_until(lambda: record.pending_prompt is not None, timeout=60.0)

            png_path = (
                fake_camera_settings.webui_data_dir / record.id / "planned_points.png"
            )
            planned = cv2.imread(str(png_path))
            assert planned is not None
            frame = _preview_frame(preview)
            assert frame.shape == planned.shape
            assert cv2.absdiff(frame, planned).mean() < 5.0

            pending = record.pending_prompt
            assert pending is not None
            manager.respond_prompt(pending[0], False)
            wait_until(lambda: record.status.terminal, timeout=60.0)

            cleared = _preview_frame(preview)
            assert cleared.shape == (720, 1280, 3)
        finally:
            manager.shutdown()

    def test_confirm_true_fails_gracefully_without_klipper(
        self,
        manager: JobManager,
        state: AppState,
        copper_pcb_path: Path,
        wait_until: WaitUntil,
    ):
        """Confirm True → セットアップで Klipper 不通 → FAILED + ロック解放."""
        state.select_pcb(copper_pcb_path)
        record = manager.start("height_plane", {})
        answered: set[str] = set()
        answer_next_prompt(record, manager, True, answered)
        wait_until(lambda: record.status.terminal, timeout=60.0)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.FAILED
        assert record.error
        assert "M84" in "\n".join(record.log_lines)
        with state.machine_lock("after-failed-job"):
            pass
