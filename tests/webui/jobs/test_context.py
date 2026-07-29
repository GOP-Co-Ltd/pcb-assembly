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
- open_camera は hub を起動保持して FrameSource を貸す。`raw=True` で歪み補正前の
  フレームを配信する（レンズ歪み補正計画 §3・§6）

prompt / abort 経由の挙動は test_manager.py（respond_prompt / request_abort
側の契約）で検証する。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from pcbasm.config import Machine
from pcbasm.vision import (
    CalibrationQuality,
    CalibrationResult,
    CameraIntrinsics,
    Image,
    ResidualReport,
)
from tests.webui.conftest import FAKE_CAMERA_IMAGE, decode_jpeg, jpeg_payload
from webui.board_settings import BoardSettingsStore
from webui.config_store import ConfigStore
from webui.jobs.catalog import JobCatalog, ParamSpec
from webui.jobs.context import JobContext, JobResult
from webui.jobs.manager import JobManager, JobStatus
from webui.preview import PreviewService
from webui.settings import Settings
from webui.state import AppState

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

    def test_machine_is_config_dir_machine(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        captured: list[Machine] = []

        def run(ctx: JobContext) -> None:
            captured.append(ctx.machine)

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        # tmp コピーした config/machine.toml の設定がロードされる
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
        assert captured[0] == fake_camera_settings.webui_data_dir / record.id


class TestBoardSettingsWiring:
    """source_pcb / board_store の manager → JobContext 配線.

    paste_solder ジョブが基板ごとの塗布設定ストアを引けるよう、manager は 選択中の PCB 相対パス・共有
    BoardSettingsStore を ctx へ渡す （計画書 Phase 5「JobContext への配線」節の契約）。
    """

    def test_source_pcb_reflects_selection(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        state: AppState,
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
        source_pcb, board_store = captured[0]
        # pcb_browse_root からの相対 posix パス（絶対パスではない）
        assert source_pcb == real_pcb_path.as_posix()
        assert isinstance(board_store, BoardSettingsStore)

    def test_source_pcb_is_none_without_selection(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        captured: list[str | None] = []

        def run(ctx: JobContext) -> None:
            captured.append(ctx.source_pcb)

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert captured == [None]


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


def _distorted_calibration(resolution: tuple[int, int]) -> CalibrationResult:
    """樽型歪み（k1=-0.2）を持つ CalibrationResult（歪み補正の有無を見分ける題材）."""
    width, height = resolution
    empty_report = ResidualReport(
        pixel_per_mm=30.0,
        rotation_deg=0.0,
        rms_um=0.0,
        max_um=0.0,
        buckets=(),
        view_residuals=(),
        corner_count=0,
    )
    return CalibrationResult(
        intrinsics=CameraIntrinsics(
            camera_matrix=(
                (float(width), 0.0, width / 2.0),
                (0.0, float(width), height / 2.0),
                (0.0, 0.0, 1.0),
            ),
            distortion=(-0.2, 0.0, 0.0, 0.0, 0.0),
            resolution=resolution,
        ),
        pixel_per_mm=30.0,
        square_size_mm=1.5,
        quality=CalibrationQuality(
            reprojection_rms_px=0.0,
            before=empty_report,
            after=empty_report,
            pixel_per_mm_std=0.0,
            view_count=0,
        ),
        calibrated_at=datetime.now(),
        z_position=-25.0,
    )


class TestOpenCameraRawFrames:
    """open_camera(raw=True) が歪み補正前のフレームを配信する（レンズ歪み補正計画 §3）.

    カメラ校正ジョブが補正済みフレームで再校正すると「残差歪みモデル」が 得られ、Apply
    でそれに置き換わって元の補正が静かに失われる（2 回目の実行で 機械が劣化する）。`raw=True` はこれを塞ぐための経路で、
    JobContext → JobBridge → PreviewService → FrameHub.subscribe(raw=)
    まで 伝播しなければ意味を持たない。

    fake camera は fake_camera.png（1280x720）をそのまま返すので、 「生フレーム ==
    元画像」「補正済みフレーム != 元画像」で伝播を判定できる。
    """

    @pytest.fixture
    def distorted_state(
        self, fake_camera_settings: Settings, store: ConfigStore
    ) -> Iterator[AppState]:
        """歪み係数入りの校正を config へ置いた AppState（frame_hub は遅延構築）."""
        state = AppState(fake_camera_settings, store)
        _distorted_calibration((1280, 720)).save(
            state.machine().camera.calibration_file
        )
        yield state
        state.close()

    @pytest.fixture
    def distorted_manager(
        self,
        distorted_state: AppState,
        fake_camera_settings: Settings,
        catalog: JobCatalog,
    ) -> Iterator[JobManager]:
        manager = JobManager(
            distorted_state,
            PreviewService(distorted_state),
            catalog,
            fake_camera_settings,
        )
        yield manager
        manager.shutdown()

    def test_raw_frames_bypass_undistortion_while_default_frames_are_corrected(
        self,
        distorted_manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        captured: dict[str, np.ndarray] = {}

        def run(ctx: JobContext) -> None:
            with ctx.open_camera(raw=True) as camera:
                captured["raw"] = camera.capture().numpy()
            with ctx.open_camera() as camera:
                captured["corrected"] = camera.capture().numpy()

        _register(catalog, run)
        record = distorted_manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED, record.error
        source = Image.load(FAKE_CAMERA_IMAGE).numpy()
        assert np.array_equal(captured["raw"], source)
        assert not np.array_equal(captured["corrected"], source)
        assert captured["corrected"].shape == source.shape


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
