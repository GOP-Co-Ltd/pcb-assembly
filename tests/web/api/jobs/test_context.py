"""`web.api.jobs.context` の仕様テスト.

計画書 webui-phase3.md「src/webui/jobs/context.py」節が契約。JobContext は
JobManager だけが生成するため、合成ジョブを manager 経由で実行し、worker に
渡された ctx の公開挙動を検証する:

- params は catalog 検証済み（default 充填・型変換済み）
- pcb_path は選択 PCB の絶対パス（未選択なら None）
- machine は選択マシンの設定、artifacts_dir は data/webui/<job_id>/（作成済み）
- log / progress は record へ反映、frame は preview のオーバーライドスロットへ
- next_command は timeout 超過で None

prompt / abort 経由の挙動は test_manager.py（respond_prompt / request_abort
側の契約）で検証する。
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest

from pcbasm.vision import Image
from tests.web.api.conftest import decode_jpeg, jpeg_payload
from web.api.board_settings import BoardSettingsStore
from web.api.jobs.catalog import JobCatalog, ParamSpec
from web.api.jobs.context import JobContext
from web.api.jobs.manager import JobManager, JobStatus
from web.api.preview import PreviewService
from web.api.settings import Settings
from web.api.state import AppState

from .conftest import WaitUntil, register_synthetic as _register


class TestContextProperties:
    """Params / pcb_path / machine / artifacts_dir."""

    def test_params_are_validated_with_defaults_filled(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        captured: list[dict[str, object]] = []

        def run(ctx: JobContext) -> None:
            captured.append(dict(ctx.params))

        _register(
            catalog,
            run,
            params=(
                ParamSpec(name="ratio", label="比率", value_type="float", default=1.0),
                ParamSpec(name="count", label="回数", value_type="int", default=3),
            ),
        )
        record = manager.start("synthetic", {"ratio": 2})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert captured == [{"ratio": 2.0, "count": 3}]

    def test_pcb_path_is_absolute_existing_file_when_selected(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        state: AppState,
        real_pcb_path: Path,
        wait_until: WaitUntil,
    ):
        state.select_pcb(real_pcb_path)
        captured: list[Path | None] = []

        def run(ctx: JobContext) -> None:
            captured.append(ctx.pcb_path)

        _register(catalog, run, requires_pcb=True)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        pcb_path = captured[0]
        assert pcb_path is not None
        assert pcb_path.is_absolute()
        assert pcb_path.is_file()
        assert pcb_path.name == "fill_coverage.kicad_pcb"

    def test_pcb_path_and_source_pcb_are_none_without_selection(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        """PCB 未選択なら絶対パスも相対 source_pcb も None."""
        captured: list[tuple[Path | None, str | None]] = []

        def run(ctx: JobContext) -> None:
            captured.append((ctx.pcb_path, ctx.source_pcb))

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert captured == [(None, None)]

    def test_artifacts_dir_is_created_under_data_webui(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        captured: list[Path] = []

        def run(ctx: JobContext) -> None:
            captured.append(ctx.artifacts_dir)
            assert ctx.artifacts_dir.is_dir()

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert captured[0] == fake_camera_settings.webui_data_dir / record.id


class TestBoardSettingsWiring:
    """source_pcb / board_store の manager → JobContext 配線.

    paste_solder ジョブが基板ごとの塗布設定ストアを引けるよう、manager は 選択中の PCB 相対パス・共有
    BoardSettingsStore を ctx へ渡す （計画書 Phase 5「JobContext への配線」節の契約）。

    MR1 追記: ctx へ渡るのは注入されたインスタンスそのものでなければならない （manager
    が自前生成に戻ると更新ロックが効かない）。
    """

    def test_source_pcb_reflects_selection(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        state: AppState,
        board_store: BoardSettingsStore,
        real_pcb_path: Path,
        wait_until: WaitUntil,
    ):
        state.select_pcb(real_pcb_path)
        captured: list[tuple[str | None, BoardSettingsStore | None]] = []

        def run(ctx: JobContext) -> None:
            captured.append((ctx.source_pcb, ctx.board_store))

        _register(catalog, run, requires_pcb=True)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        source_pcb, ctx_board_store = captured[0]
        # pcb_browse_root からの相対 posix パス（絶対パスではない）
        assert source_pcb == real_pcb_path.as_posix()
        # 注入した同一インスタンス（等価ではなく同一性）
        assert ctx_board_store is board_store


class TestLogAndProgress:
    """Log / progress の record への反映."""

    def test_log_lines_are_appended_to_record(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        def run(ctx: JobContext) -> None:
            ctx.log("1 行目")
            ctx.log("2 行目")

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert "1 行目" in record.log_lines
        assert "2 行目" in record.log_lines

    @pytest.mark.parametrize(("stage", "percent"), [("描画", 42.0), ("読込", None)])
    def test_progress_updates_stage_and_percent(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
        stage: str,
        percent: float | None,
    ):
        """Percent は省略可（進捗率の出ない工程がある）."""

        def run(ctx: JobContext) -> None:
            ctx.progress(stage, percent)

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.progress_stage == stage
        assert record.progress_percent == percent


class TestFrame:
    """Frame → PreviewService オーバーライドスロット（公開挙動 = MJPEG 配信）."""

    def test_frame_takes_priority_over_camera_in_stream(
        self,
        state: AppState,
        catalog: JobCatalog,
        fake_camera_settings: Settings,
        board_store: BoardSettingsStore,
        wait_until: WaitUntil,
    ):
        # TTL を長くした PreviewService で「直近のジョブ提供フレーム優先」を
        # 決定的に観測する
        preview = PreviewService(state, override_ttl=60.0)
        manager = JobManager(state, preview, catalog, fake_camera_settings, board_store)
        frame_sent = threading.Event()
        gate = threading.Event()
        magenta = np.zeros((48, 64, 3), dtype=np.uint8)
        magenta[:] = (255, 0, 255)  # BGR

        def run(ctx: JobContext) -> None:
            ctx.frame(Image(magenta))
            frame_sent.set()
            gate.wait(timeout=10.0)

        _register(catalog, run)
        record = manager.start("synthetic", {})
        try:
            assert frame_sent.wait(timeout=10.0)

            stream = preview.mjpeg_stream("none")
            try:
                part = next(stream)
            finally:
                stream.close()

            image = decode_jpeg(jpeg_payload(part))
            assert image is not None
            center = image[image.shape[0] // 2, image.shape[1] // 2]
            assert abs(int(center[0]) - 255) < 40  # B
            assert abs(int(center[1]) - 0) < 40  # G
            assert abs(int(center[2]) - 255) < 40  # R
        finally:
            gate.set()
            manager.shutdown()
        wait_until(lambda: record.status.terminal)


class TestOpenCamera:
    """open_camera（Phase 4: bridge.hold_camera 経由のカメラ貸し出し）.

    計画書 webui-phase4.md「src/webui/jobs/context.py」節が契約: open_camera は
    カメラパイプラインを起動保持して FrameSource を貸し、退出で解放する。 PreviewService
    と参照カウントを共有する（共有側の検証は test_preview.py）。
    """

    def test_open_camera_lends_capturable_camera_and_releases_hub(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        state: AppState,
        wait_until: WaitUntil,
    ):
        sizes: list[tuple[int, int]] = []
        running_during: list[bool] = []

        def run(ctx: JobContext) -> None:
            with ctx.open_camera() as camera:
                sizes.append(camera.capture().size)
                running_during.append(state.frame_hub().running)

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert sizes == [(1280, 720)]  # fake_camera.png のフレームが capture できる
        assert running_during == [True]
        assert not state.frame_hub().running  # 退出で解放（参照 0 で停止）

    def test_open_camera_releases_hub_when_job_fails_inside(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        state: AppState,
        wait_until: WaitUntil,
    ):
        """With ブロック内の例外でも hub は解放される（FAILED で停止漏れなし）."""

        def run(ctx: JobContext) -> None:
            with ctx.open_camera():
                raise RuntimeError("ジョブ内エラー")

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.FAILED
        assert not state.frame_hub().running


class TestNextCommand:
    """next_command（abort 連動は test_manager.py）."""

    def test_next_command_returns_none_on_timeout(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        captured: list[dict[str, object] | None] = []

        def run(ctx: JobContext) -> None:
            captured.append(ctx.next_command(timeout=0.05))

        _register(catalog, run, accepts_commands=True)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert captured == [None]
