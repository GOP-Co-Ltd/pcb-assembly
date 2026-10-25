"""`web.api.jobs.runtime.hooks`（ジョブの節目に装置へさせること）の仕様テスト.

- 装置を動かすジョブの終了時に、ノズルキャップ（未記録なら PRESENT）へ退避して脱力する。失敗はログだけでジョブの結果を変えない
- 装置を動かすジョブの成功・失敗を、機体スピーカーで 1 回だけ知らせる（中止は鳴らさない）
- オペレータ待ちに入るたび、入力待ち音で作業者を呼び戻す

ジョブは実 JobManager で走らせ、Klipper はテスト用 config の接続拒否、スピーカーは FakeAudioPlayer で確かめる。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest
import tomlkit

from pcbasm.config.audio import AudioConfig
from pcbasm.hal import AudioPlaybackError
from pcbasm.hal.fake import FakeAudioPlayer
from tests.support.polling import wait_until
from tests.support.web import (
    ManagerFactory,
    answer_next_prompt,
    register_gated as _register_gated,
    register_synthetic as _register,
)
from web.api.backend import Backend
from web.api.jobs.runtime.catalog import JobCatalog
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobStatus, PromptSpec


def _configure_audio(config_dir: Path) -> AudioConfig:
    """machine.toml へ `[audio]` を書き、書いた設定を返す."""
    config = AudioConfig(device="test-speaker", volume=0.3)
    path = config_dir / "machine.toml"
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    document["audio"] = {"device": config.device, "volume": config.volume}
    path.write_text(tomlkit.dumps(document), encoding="utf-8")
    return config


@pytest.mark.usefixtures("unreachable_klipper")
class TestPresentOnTermination:
    """ジョブ終了時の PRESENT / M84（relax）ベストエフォート送信."""

    def test_present_failure_is_logged_and_succeeded_status_kept(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        camera_backend: Backend,
    ):
        _register(catalog, lambda ctx: None, uses_machine=True)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)
        wait_until(lambda: camera_backend.lock.owner is None)

        assert record.status == JobStatus.SUCCEEDED
        log_text = "\n".join(record.log_lines)
        assert "PRESENT" in log_text
        assert "M84" in log_text

    def test_present_failure_does_not_change_failed_status(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        camera_backend: Backend,
    ):
        def run(ctx: JobContext) -> None:
            raise RuntimeError("意図的な失敗")

        _register(catalog, run, uses_machine=True)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)
        wait_until(lambda: camera_backend.lock.owner is None)

        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert "意図的な失敗" in record.error
        log_text = "\n".join(record.log_lines)
        assert "PRESENT" in log_text
        assert "M84" in log_text

    def test_recorded_cap_failure_logs_park_message_without_present(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        camera_backend: Backend,
        config_dir: Path,
    ):
        """キャップ記録済み（paste）はキャップ駐機経路になり、失敗ログは退避文言のみ.

        nozzle-cap-parking 計画書「呼び出し 3 箇所の差し替え」節: catch-all 文言は
        「タスク終了時の退避に失敗: {exc}」で "PRESENT" / "M84" の語を含めない
        （フォールバック経路のログ断言との一意性を保つ）。
        """
        path = config_dir / "machine.toml"
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n[paste_dispenser.nozzle_cap]\nx = 10.0\ny = 20.0\nz = 3.5\n",
            encoding="utf-8",
        )
        _register(catalog, lambda ctx: None, uses_machine=True)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)
        wait_until(lambda: camera_backend.lock.owner is None)

        assert record.status == JobStatus.SUCCEEDED
        log_text = "\n".join(record.log_lines)
        assert "退避に失敗" in log_text
        assert "PRESENT" not in log_text
        assert "M84" not in log_text

    def test_no_present_attempt_for_non_machine_job(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        camera_backend: Backend,
    ):
        """uses_machine=False（dev ジョブ相当）では終了処理を試行しない."""
        _register(catalog, lambda ctx: None, uses_machine=False)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)
        wait_until(lambda: camera_backend.lock.owner is None)

        assert record.status == JobStatus.SUCCEEDED
        assert "PRESENT" not in "\n".join(record.log_lines)
        assert "M84" not in "\n".join(record.log_lines)


@pytest.mark.usefixtures("unreachable_klipper")
class TestAudioCompletionNotification:
    """装置を動かすジョブ（`uses_machine=True`）の完了を機体スピーカーで知らせる.

    装置の前を離れた作業者が終了に気付けるようにするための契約:

    - 成功 → success / 失敗 → failure を 1 回だけ鳴らす
    - 手動中止（ABORTED）は鳴らさない
    - `uses_machine=False` の即終了ジョブは成功・失敗とも鳴らさない。ブラウザ完了
      通知の `notify_on_completion` とは独立で、フラグを立てても鳴らない
    - `[audio]` 未設定でも既定値（MachineConfig.audio）で鳴る
    - 再生失敗は warning のみでジョブ status に影響しない
    """

    def test_success_plays_success_sound_once(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        config_dir: Path,
        audio_player: FakeAudioPlayer,
    ):
        config = _configure_audio(config_dir)
        manager = make_manager(catalog)
        _register(catalog, lambda ctx: None, uses_machine=True)

        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)
        wait_until(lambda: len(audio_player.played) == 1)

        assert record.status == JobStatus.SUCCEEDED
        assert audio_player.played == (("success", config.device, config.volume),)

    def test_failure_plays_failure_sound_once(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        config_dir: Path,
        audio_player: FakeAudioPlayer,
    ):
        config = _configure_audio(config_dir)
        manager = make_manager(catalog)

        def fail(ctx: JobContext) -> None:
            raise RuntimeError("意図的な失敗")

        _register(catalog, fail, uses_machine=True)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)
        wait_until(lambda: len(audio_player.played) == 1)

        assert record.status == JobStatus.FAILED
        assert audio_player.played == (("failure", config.device, config.volume),)

    def test_aborted_job_does_not_play(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        config_dir: Path,
        audio_player: FakeAudioPlayer,
    ):
        _configure_audio(config_dir)
        manager = make_manager(catalog)
        _register_gated(catalog, uses_machine=True)
        record = manager.start("gated", {})
        wait_until(lambda: record.status == JobStatus.RUNNING)

        assert manager.request_abort() is True
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.ABORTED
        assert audio_player.played == ()

    def test_non_machine_job_stays_silent(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        config_dir: Path,
        audio_player: FakeAudioPlayer,
    ):
        """`notify_on_completion` はブラウザ通知専用で、完了音を左右しない."""
        _configure_audio(config_dir)
        manager = make_manager(catalog)

        _register(
            catalog, lambda ctx: None, uses_machine=False, notify_on_completion=True
        )
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert audio_player.played == ()

    def test_missing_audio_section_plays_with_defaults(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        audio_player: FakeAudioPlayer,
    ):
        """`[audio]` を書かない fixture でも既定デバイス・既定音量で鳴る."""
        manager = make_manager(catalog)
        _register(catalog, lambda ctx: None, uses_machine=True)

        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)
        wait_until(lambda: len(audio_player.played) == 1)

        assert record.status == JobStatus.SUCCEEDED
        assert audio_player.played == (("success", "default", 0.75),)

    @pytest.mark.parametrize(
        "audio_player",
        [FakeAudioPlayer(playback_error=AudioPlaybackError("speaker disconnected"))],
    )
    def test_playback_failure_warns_without_changing_job_status(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        config_dir: Path,
        caplog: pytest.LogCaptureFixture,
        audio_player: FakeAudioPlayer,
    ):
        _configure_audio(config_dir)
        manager = make_manager(catalog)

        def run(ctx: JobContext) -> None:
            raise RuntimeError("job failure")

        _register(catalog, run, uses_machine=True)
        with caplog.at_level(logging.WARNING):
            record = manager.start("synthetic", {})
            wait_until(lambda: record.status.terminal)
            wait_until(
                lambda: any(
                    item.levelno >= logging.WARNING
                    and "speaker disconnected" in item.getMessage()
                    for item in caplog.records
                )
            )

        assert record.status == JobStatus.FAILED
        assert len(audio_player.played) == 1


class TestAudioOperatorNotification:
    """オペレータ待ちに入るたび、機体スピーカーが作業者を呼び戻す.

    装置の前を離れた作業者が応答待ちに気付けるようにするための契約:

    - 応答待ちに入った時点で `prompt` を鳴らす（応答を待たない）。ジョブ側の
      オプトインは無く、**すべての** prompt が毎回鳴らす
    - `notify_operator()` はコマンド待ちなど prompt 以外のオペレータ待ちで
      同じ音を鳴らす。abort 済みなら鳴らさない

    再生失敗の握り潰しは `TestAudioCompletionNotification` が同じ `_play_sound`
    を通して固定する。
    """

    def test_every_prompt_plays_the_input_sound(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        config_dir: Path,
        audio_player: FakeAudioPlayer,
    ):
        config = _configure_audio(config_dir)
        manager = make_manager(catalog)
        answers: list[object] = []

        def run(ctx: JobContext) -> None:
            answers.append(ctx.prompt(PromptSpec(kind="confirm", message="続行?")))
            answers.append(ctx.prompt(PromptSpec(kind="number", message="質量 [mg]")))

        _register(catalog, run)

        record = manager.start("synthetic", {})
        answered: set[str] = set()
        answer_next_prompt(record, manager, True, answered)
        answer_next_prompt(record, manager, 110.5, answered)
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert answers == [True, 110.5]
        assert audio_player.played == (
            ("prompt", config.device, config.volume),
            ("prompt", config.device, config.volume),
        )

    def test_notify_operator_plays_the_input_sound(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        config_dir: Path,
        audio_player: FakeAudioPlayer,
    ):
        """コマンド待ち（prompt を出さないオペレータ待ち）用の明示通知."""
        config = _configure_audio(config_dir)
        manager = make_manager(catalog)

        def run(ctx: JobContext) -> None:
            ctx.notify_operator()

        _register(catalog, run)

        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)
        wait_until(lambda: len(audio_player.played) == 1)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert audio_player.played == (("prompt", config.device, config.volume),)

    def test_notify_operator_stays_silent_after_abort(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        config_dir: Path,
        audio_player: FakeAudioPlayer,
    ):
        """Abort 済みなら鳴らさない（死んだジョブの前へ呼び戻さないため）."""
        _configure_audio(config_dir)
        manager = make_manager(catalog)
        started = threading.Event()
        abort_requested = threading.Event()

        def run(ctx: JobContext) -> None:
            started.set()
            abort_requested.wait(timeout=10.0)
            ctx.notify_operator()
            ctx.checkpoint()

        _register(catalog, run)

        record = manager.start("synthetic", {})
        started.wait(timeout=10.0)
        assert manager.request_abort() is True
        abort_requested.set()
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.ABORTED
        assert audio_player.played == ()
