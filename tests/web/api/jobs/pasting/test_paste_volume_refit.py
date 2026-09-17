"""保存済みdatasetから塗布量校正を作り直すジョブの契約。"""

from __future__ import annotations

from pathlib import Path

import cv2

from pcbasm.pasting.paste_volume.calibration import list_calibrations, load_calibration
from tests.helpers import build_paste_volume_session
from tests.web.api.jobs.conftest import WaitUntil, answer_next_prompt
from web.api.jobs.manager import JobManager, JobRecord, JobStatus
from web.api.settings import Settings


class TestPasteVolumeRefit:
    """収集済み session から直径ベース校正を作る（装置不要）.

    判定とフィットは `pcbasm.pasting.paste_volume` にあり、ここが確かめるのは
    session の選択・成果物・保存の可否というジョブ層の契約。
    """

    STEM = "plate-47.5x20-20260909T145923.452+0900"

    def _session(self, settings: Settings, *, blank_material: str = "blank") -> Path:
        return build_paste_volume_session(
            settings.paste_dataset_dir / self.STEM, blank_material=blank_material
        )

    def _start(self, manager: JobManager, **overrides: object) -> JobRecord:
        return manager.start("paste_volume_refit", {**overrides})

    def test_fits_the_selected_session_and_writes_the_calibration(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        self._session(fake_camera_settings)

        record = self._start(manager, save_name="s3x70-n030-h020")
        answer_next_prompt(record, manager, self.STEM, set())
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.SUCCEEDED, record.error
        saved = list_calibrations(fake_camera_settings.paste_volume_calibration_dir)
        assert len(saved) == 1
        calibration, error = load_calibration(saved[0])
        assert error is None, error
        assert calibration is not None
        assert calibration.source.session == self.STEM
        assert calibration.diagnostics.blank_false_positive_count == 0
        assert calibration.diagnostics.detection_failure_count == 0

    def test_an_empty_save_name_still_reports_and_draws_the_diagnostics(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        """保存名なし = ハイパラ探索。図とまとめだけ出し、保存先は汚さない.

        主基準は session 総体積の誤差なので、まとめに必ず出す。
        """
        self._session(fake_camera_settings)

        record = self._start(manager)
        answer_next_prompt(record, manager, self.STEM, set())
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert not fake_camera_settings.paste_volume_calibration_dir.exists()

        assert record.result is not None
        assert record.result.summary is not None
        assert "総体積誤差" in record.result.summary
        # 散布図と検出モンタージュ
        artifacts_root = fake_camera_settings.webui_data_dir
        images = [a for a in record.result.artifacts if a.kind == "image"]
        assert len(images) == 2
        for artifact in images:
            image = cv2.imread(str(artifacts_root / artifact.path))
            assert image is not None
            assert image.size > 0

    def test_fails_when_a_blank_cell_is_detected_as_a_deposit(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        self._session(fake_camera_settings, blank_material="large")

        record = self._start(manager, save_name="broken")
        answer_next_prompt(record, manager, self.STEM, set())
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert "blank" in record.error
        assert not fake_camera_settings.paste_volume_calibration_dir.exists()

    def test_can_continue_past_a_blank_false_positive_on_request(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        self._session(fake_camera_settings, blank_material="large")

        record = self._start(manager, require_blank_zero=False)
        answer_next_prompt(record, manager, self.STEM, set())
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.SUCCEEDED, record.error

    def test_fails_when_no_completed_dataset_exists(
        self, manager: JobManager, wait_until: WaitUntil
    ):
        record = self._start(manager)
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert "完成dataset" in record.error
        assert record.pending_prompt is None

    def test_rejects_an_invalid_detection_hyperparameter_before_prompting(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        self._session(fake_camera_settings)

        record = self._start(manager, open_kernel_px=4)
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.FAILED
        assert record.pending_prompt is None
