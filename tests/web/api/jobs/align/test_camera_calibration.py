"""camera_calibration ジョブの仕様テスト.

- チェッカーボード FakeCamera でのフル結合（prompt 往復、artifacts、Apply payload、Klipper 不通での Z best-effort = z_position None）
- crop は params ではなく machine.toml `[camera.crop]`（``ctx.machine.camera.crop.size``）を読む
- 実機通し（実カメラ + 実 Klipper）は受け入れ試験（docs/acceptance.md の A4）

cv2 / Moonraker のモックは使わない（skill `testing-strategy`）。Klipper 不通は
テスト用 config の実ポートへの接続拒否で検証する。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pcbasm.hal.fake import FakeAudioPlayer
from pcbasm.vision import CameraCalibration, Image
from tests.support.backend import devices_with_audio, job_manager
from tests.web.api.jobs.conftest import WaitUntil, answer_next_prompt
from web.api.backend import Backend, open_backend
from web.api.env import Env
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobStatus
from web.api.machine_config import MachineConfigFile

# checkerboard.png は 400x400・1 マス約 66.7px。square_size=10mm で
# pixel_per_mm ≈ 400/6/10 ≈ 6.67（tests/pcbasm/vision/test_calibration.py と
# 同一素材）。crop は machine.toml `[camera.crop]` 由来（checkerboard_backend
# fixture が 400x400 に書き換える。既定 600 は 400px 画像をはみ出す）
CHECKERBOARD_PARAMS = {"square_size": 10.0}
CHECKERBOARD_CROP_SIZE = (400, 400)
CHECKERBOARD_PIXEL_PER_MM = 400 / 6 / 10


@pytest.fixture
def checkerboard_backend(
    checkerboard_camera_settings: Env,
    machine_config: MachineConfigFile,
    audio_player: FakeAudioPlayer,
) -> Iterator[Backend]:
    # checkerboard.png は 400x400。既定 crop 600 は画像をはみ出すため、
    # tmp コピーの machine.toml へ 400x400 を書いてから Backend を作る
    # （crop は machine.toml `[camera.crop]` から読まれる契約）
    machine_config.write({"camera.crop.width": 400, "camera.crop.height": 400})
    backend = open_backend(
        checkerboard_camera_settings,
        devices=devices_with_audio(checkerboard_camera_settings, audio_player),
    )
    yield backend
    backend.close()


@pytest.fixture
def checkerboard_manager(checkerboard_backend: Backend) -> Iterator[JobManager]:
    manager = job_manager(checkerboard_backend, checkerboard_backend.catalog)
    yield manager
    manager.shutdown()


@pytest.mark.usefixtures("unreachable_klipper")
class TestCameraCalibrationJob:
    """camera_calibration のフル結合（FakeCamera + テスト用 config、装置なし）."""

    def test_full_run_with_checkerboard_yields_apply_payload(
        self,
        checkerboard_manager: JobManager,
        checkerboard_camera_settings: Env,
        wait_until: WaitUntil,
    ):
        """チェッカーボード画像で SUCCEEDED まで完走し Apply payload を返す.

        - prompt(confirm) に True 応答で撮影 → 検出成功
        - Z は best-effort: Klipper 不通（port 7126）でも続行し z_position=None、
          summary に「未取得」（ユーザー決定 2026-06-12）。
          焦点 Z の無い校正ではジョブが開始時に止まるので、そのことも summary で伝える
        - artifacts: コーナー描画 PNG（image）+ calibration JSON（file）。
          JSON は CameraCalibration.load で読め、数値が素材と整合する
        - apply: values は camera.calibration_file のみ、files に JSON 1 件
        """
        record = checkerboard_manager.start("camera_calibration", CHECKERBOARD_PARAMS)
        answered: set[str] = set()
        answer_next_prompt(record, checkerboard_manager, True, answered)
        wait_until(lambda: record.status.terminal, timeout=30.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "未取得" in result.summary  # Z best-effort 失敗の明示
        assert "校正を使うジョブは開始時に止まります" in result.summary

        kinds = {artifact.kind for artifact in result.artifacts}
        assert kinds == {"image", "file"}
        artifacts_root = checkerboard_camera_settings.webui_data_dir
        png_artifact = next(a for a in result.artifacts if a.kind == "image")
        json_artifact = next(a for a in result.artifacts if a.kind == "file")
        Image.load(artifacts_root / png_artifact.path)  # 読めなければ例外
        loaded = CameraCalibration.load(artifacts_root / json_artifact.path)
        assert loaded.pixel_per_mm == pytest.approx(CHECKERBOARD_PIXEL_PER_MM, rel=0.01)
        assert loaded.z_position is None  # Klipper 不通 → 記録なし
        # crop は machine.toml 由来（checkerboard_backend fixture が書いた 400x400）
        assert loaded.crop_size == CHECKERBOARD_CROP_SIZE

        assert record.apply_available is True
        payload = checkerboard_manager.apply_payload()
        filename = payload.values["camera.calibration_file"]
        assert isinstance(filename, str)
        assert filename.endswith(".json")
        assert set(payload.values) == {"camera.calibration_file"}
        assert len(payload.files) == 1
        assert payload.files[0].filename == filename

    def test_decline_first_prompt_aborts_without_apply(
        self, checkerboard_manager: JobManager, wait_until: WaitUntil
    ):
        """撮影確認に「いいえ」→ ABORTED（Apply なし）."""
        record = checkerboard_manager.start("camera_calibration", CHECKERBOARD_PARAMS)
        answered: set[str] = set()
        answer_next_prompt(record, checkerboard_manager, False, answered)
        wait_until(lambda: record.status.terminal, timeout=30.0)

        assert record.status == JobStatus.ABORTED
        assert record.apply_available is False

    def test_undetectable_image_logs_warning_and_reprompts(
        self, manager: JobManager, wait_until: WaitUntil
    ):
        """検出不能画像（fake_camera.png）→ 警告 log 後に再 prompt で継続できる.

        jobs/conftest の manager は既定の fake_camera.png（チェッカーボード なし）を使う。再
        prompt に「いいえ」で中止できる。
        """
        record = manager.start("camera_calibration", CHECKERBOARD_PARAMS)
        answered: set[str] = set()
        answer_next_prompt(record, manager, True, answered)

        # 検出失敗 → 警告 log → 2 回目の prompt（別 id）が来る
        answer_next_prompt(record, manager, False, answered)
        wait_until(lambda: record.status.terminal, timeout=30.0)

        assert len(answered) == 2
        assert "検出できませんでした" in "\n".join(record.log_lines)
        assert record.status == JobStatus.ABORTED
