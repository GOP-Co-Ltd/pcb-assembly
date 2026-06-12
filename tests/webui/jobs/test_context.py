"""`webui.jobs.context` の仕様テスト.

計画書 webui-phase3.md「src/webui/jobs/context.py」節が契約。JobContext は
JobManager だけが生成するため、合成ジョブを manager 経由で実行し、worker に
渡された ctx の公開挙動を検証する:

- params は catalog 検証済み（default 充填・型変換済み）
- pcb_path は選択 PCB の絶対パス（未選択なら None）
- machine は選択マシンの設定、artifacts_dir は data/webui/<job_id>/（作成済み）
- log / progress は record へ反映、frame は preview のオーバーライドスロットへ
- checkpoint は abort 未要求なら no-op
- next_command は timeout 超過で None

prompt / abort 経由の挙動は test_manager.py（respond_prompt / request_abort
側の契約）で検証する。
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest

from pcbasm.config import Machine
from pcbasm.vision import Image
from tests.webui.conftest import decode_jpeg, jpeg_payload
from webui.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from webui.jobs.context import JobContext
from webui.jobs.manager import JobManager, JobResult, JobStatus
from webui.preview import PreviewService
from webui.settings import Settings
from webui.state import AppState

from .conftest import WaitUntil


def _register(
    catalog: JobCatalog,
    run,
    *,
    name: str = "synthetic",
    params: tuple[ParamSpec, ...] = (),
    requires_pcb: bool = False,
    accepts_commands: bool = False,
) -> None:
    catalog.register(
        JobDefinition(
            name=name,
            label="合成ジョブ",
            tab="dev",
            run=run,
            params=params,
            requires_pcb=requires_pcb,
            uses_machine=False,
            accepts_commands=accepts_commands,
        )
    )


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

    def test_pcb_path_is_none_without_selection(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        captured: list[Path | None] = []

        def run(ctx: JobContext) -> None:
            captured.append(ctx.pcb_path)

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert captured == [None]

    def test_machine_is_selected_machine_config(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        captured: list[Machine] = []

        def run(ctx: JobContext) -> None:
            captured.append(ctx.machine)

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        # 既定選択マシン kurousagi（test-fixture コピー）の設定がロードされる
        assert captured[0].klipper.port == 7126

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
        assert captured[0] == fake_camera_settings.data_dir / "webui" / record.id


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

    def test_progress_updates_stage_and_percent(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        def run(ctx: JobContext) -> None:
            ctx.progress("描画", 42.0)

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.progress_stage == "描画"
        assert record.progress_percent == 42.0

    def test_progress_percent_may_be_none(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        def run(ctx: JobContext) -> None:
            ctx.progress("読込")

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.progress_stage == "読込"
        assert record.progress_percent is None


class TestFrame:
    """Frame → PreviewService オーバーライドスロット（公開挙動 = MJPEG 配信）."""

    def test_frame_takes_priority_over_camera_in_stream(
        self,
        state: AppState,
        catalog: JobCatalog,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        # TTL を長くした PreviewService で「直近のジョブ提供フレーム優先」を
        # 決定的に観測する
        preview = PreviewService(state, override_ttl=60.0)
        manager = JobManager(state, preview, catalog, fake_camera_settings)
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


class TestCheckpointAndNextCommand:
    """Checkpoint / next_command（abort 連動は test_manager.py）."""

    def test_checkpoint_is_noop_without_abort_request(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        def run(ctx: JobContext) -> JobResult:
            ctx.checkpoint()
            ctx.checkpoint()
            return JobResult(summary="checkpoint 通過")

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED

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
