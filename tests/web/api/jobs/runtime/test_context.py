"""`web.api.jobs.runtime.context` の仕様テスト.

計画書 webui-phase3.md「src/webui/jobs/context.py」節が契約。JobContext は
JobManager だけが生成するため、合成ジョブを manager 経由で実行し、worker に
渡された ctx の公開挙動を検証する:

- params は catalog 検証済み（default 充填・型変換済み）
- pcb_path は選択 PCB の絶対パス（未選択なら None）
- board() はファイルが差し替わっていれば読み直す
- machine は選択マシンの設定、artifacts_dir は data/webui/<job_id>/（作成済み）
- log / progress は record へ反映、frame はカメラ（CameraFeed）のオーバーライドスロットへ
- next_command は timeout 超過で None
- confirm / require / choose / ask_positive は prompt の定型（ボタンの文言・中止・正数の再入力）

prompt / abort 経由の挙動は test_manager.py（respond_prompt / request_abort
側の契約）で検証する。
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import attrs
import numpy as np
import pytest

from pcbasm.hal.fake import FakeAudioPlayer
from pcbasm.vision import Image
from tests.support.backend import job_manager
from tests.web.api.conftest import COPPER_PCB_FIXTURE, decode_jpeg, jpeg_payload
from tests.web.api.jobs.conftest import WaitUntil, register_synthetic as _register
from web.api.backend import Backend
from web.api.camera import CameraFeed
from web.api.env import Env
from web.api.jobs.runtime.catalog import JobCatalog, ParamSpec
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobRecord, JobStatus, PromptSpec


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
        camera_backend: Backend,
        real_pcb_path: Path,
        wait_until: WaitUntil,
    ):
        camera_backend.selection.select(real_pcb_path)
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
        fake_camera_settings: Env,
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


class TestSourcePcb:
    """source_pcb: 基板ごとの塗布設定の鍵になる、選択中の PCB の相対パス."""

    def test_source_pcb_reflects_selection(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        camera_backend: Backend,
        real_pcb_path: Path,
        wait_until: WaitUntil,
    ):
        camera_backend.selection.select(real_pcb_path)
        captured: list[str | None] = []

        def run(ctx: JobContext) -> None:
            captured.append(ctx.source_pcb)

        _register(catalog, run, requires_pcb=True)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        # pcb_browse_root からの相対 posix パス（絶対パスではない）
        assert captured == [real_pcb_path.as_posix()]


class TestBoard:
    """ctx.board() はジョブの開始時の選択 PCB を、差し替わっていれば読み直して返す."""

    def test_job_after_replacing_the_file_uses_the_new_pads(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        camera_backend: Backend,
        pcb_root: Path,
        real_pcb_path: Path,
        wait_until: WaitUntil,
    ):
        camera_backend.selection.select(real_pcb_path)
        captured: list[set[str]] = []

        def run(ctx: JobContext) -> None:
            captured.append({pad.designator for pad in ctx.board().pads})

        _register(catalog, run, requires_pcb=True)

        def designators() -> set[str]:
            record = manager.start("synthetic", {})
            wait_until(lambda: record.status.terminal)
            assert record.status == JobStatus.SUCCEEDED
            return captured[-1]

        assert "CONCAVE_L" in designators()

        shutil.copy(COPPER_PCB_FIXTURE, pcb_root / real_pcb_path)

        assert "CONCAVE_L" not in designators()
        assert "U1" in captured[-1]


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
    """Frame → CameraFeed オーバーライドスロット（公開挙動 = MJPEG 配信）."""

    def test_frame_takes_priority_over_camera_in_stream(
        self,
        camera_backend: Backend,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        # TTL を長くした CameraFeed で「直近のジョブ提供フレーム優先」を
        # 決定的に観測する
        camera = CameraFeed(
            camera_backend.devices, camera_backend.machine_config, override_ttl=60.0
        )
        manager = job_manager(attrs.evolve(camera_backend, camera=camera), catalog)
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

            stream = camera.mjpeg_stream("none")
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
    カメラパイプラインを起動保持して FrameSource を貸し、退出で解放する。 CameraFeed
    と参照カウントを共有する（共有側の検証は test_camera.py）。
    """

    def test_open_camera_lends_capturable_camera_and_releases_hub(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        camera_backend: Backend,
        wait_until: WaitUntil,
    ):
        sizes: list[tuple[int, int]] = []
        running_during: list[bool] = []

        def run(ctx: JobContext) -> None:
            with ctx.open_camera() as camera:
                sizes.append(camera.capture().size)
                running_during.append(camera_backend.devices.frame_hub().running)

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert sizes == [(1280, 720)]  # fake_camera.png のフレームが capture できる
        assert running_during == [True]
        assert (
            not camera_backend.devices.frame_hub().running
        )  # 退出で解放（参照 0 で停止）

    def test_open_camera_releases_hub_when_job_fails_inside(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        camera_backend: Backend,
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
        assert not camera_backend.devices.frame_hub().running


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


def _prompts_of(
    manager: JobManager,
    record: JobRecord,
    answers: list[object],
    wait_until: WaitUntil,
) -> list[PromptSpec]:
    """応答待ちを 1 件ずつ待って answers の順に答え、出た PromptSpec を返す."""
    specs: list[PromptSpec] = []
    answered: set[str] = set()
    for answer in answers:
        wait_until(
            lambda: (pending := record.pending_prompt) is not None
            and pending[0] not in answered,
            timeout=60.0,
        )
        pending = record.pending_prompt
        assert pending is not None
        specs.append(pending[1])
        manager.respond_prompt(pending[0], answer)
        answered.add(pending[0])
    wait_until(lambda: record.status.terminal, timeout=60.0)
    return specs


class TestConfirm:
    """confirm: 2 択のボタンで尋ね、true 側なら True."""

    def test_buttons_carry_the_given_labels_and_default_to_true(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        results: list[bool] = []

        def run(ctx: JobContext) -> None:
            results.append(
                ctx.confirm(
                    "Top-Left: 次へ進みます", true_label="次へ", false_label="終了"
                )
            )
            results.append(ctx.confirm("続けますか"))

        _register(catalog, run)
        record = manager.start("synthetic", {})
        specs = _prompts_of(manager, record, [True, False], wait_until)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert results == [True, False]
        assert [(spec.kind, spec.default) for spec in specs] == [
            ("confirm", True),
            ("confirm", True),
        ]
        assert [(spec.true_label, spec.false_label) for spec in specs] == [
            ("次へ", "終了"),
            ("続行", "中止"),
        ]

    def test_while_waiting_callback_runs_during_the_wait(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        """応答待ちの間ライブフレームを流し続けられる（直行性テストの十字線の常時表示）."""
        polls: list[int] = []

        def run(ctx: JobContext) -> None:
            ctx.confirm("Top-Left", while_waiting=lambda: polls.append(1))

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: len(polls) >= 2, timeout=60.0)
        _prompts_of(manager, record, [True], wait_until)

        assert record.status == JobStatus.SUCCEEDED, record.error


class TestRequire:
    """require: false 側を選ぶとジョブを中止する."""

    @pytest.mark.parametrize(
        ("answer", "status"),
        [(True, JobStatus.SUCCEEDED), (False, JobStatus.ABORTED)],
    )
    def test_false_aborts_the_job(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
        answer: bool,
        status: JobStatus,
    ):
        reached: list[bool] = []

        def run(ctx: JobContext) -> None:
            ctx.require("TARE しましたか", true_label="TARE・設置完了")
            reached.append(True)

        _register(catalog, run)
        record = manager.start("synthetic", {})
        [spec] = _prompts_of(manager, record, [answer], wait_until)

        assert record.status == status
        assert reached == ([True] if answer else [])
        assert (spec.true_label, spec.false_label) == ("TARE・設置完了", "中止")


class TestChoose:
    def test_returns_the_chosen_option(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        chosen: list[str] = []

        def run(ctx: JobContext) -> None:
            chosen.append(ctx.choose("層?", ("top", "bottom"), default="bottom"))

        _register(catalog, run)
        record = manager.start("synthetic", {})
        [spec] = _prompts_of(manager, record, ["top"], wait_until)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert chosen == ["top"]
        assert (spec.kind, spec.choices, spec.default) == (
            "choice",
            ("top", "bottom"),
            "bottom",
        )


class TestAskPositive:
    """ask_positive: 正の数が入力されるまで尋ねる."""

    def test_asks_again_until_positive_and_notifies_every_time(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
        audio_player: FakeAudioPlayer,
    ):
        """計量入力のように装置の前を離れた作業者を呼び戻す用途なので、聞き直すたびに鳴らす."""
        answers: list[float] = []

        def run(ctx: JobContext) -> None:
            answers.append(ctx.ask_positive("質量 [mg]"))

        _register(catalog, run)
        record = manager.start("synthetic", {})
        specs = _prompts_of(manager, record, [-1.0, 110.5], wait_until)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert answers == [110.5]
        assert [spec.false_label for spec in specs] == [None, None]
        assert "正の数値を入力してください" in "\n".join(record.log_lines)
        assert [sound for sound, *_ in audio_player.played] == ["prompt", "prompt"]

    def test_cancel_button_returns_none(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        answers: list[float | None] = []

        def run(ctx: JobContext) -> None:
            answers.append(ctx.ask_positive("質量 [mg]", cancel_label="中止"))

        _register(catalog, run)
        record = manager.start("synthetic", {})
        [spec] = _prompts_of(manager, record, [False], wait_until)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert answers == [None]
        assert spec.false_label == "中止"
