"""未完了datasetを機械を動かさず確定するジョブの契約。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import PROJECT_ROOT
from tests.web.api.jobs.conftest import WaitUntil, answer_next_prompt
from web.api.jobs.manager import JobManager, JobStatus
from web.api.settings import Settings


class TestPasteDatasetFinalize:
    """未完了 dataset の救出（pending.json + 計量質量 → metadata.json）.

    収集の最後に WebUI が落ちて計量質量を入力できなかったセッションを、装置を
    使わずに確定させる。撮影済み画像を作り直さない（rename だけ）のが契約。
    """

    STEM = "plate-20x20-20260908T143052.123+0000"
    MEASURED_MASS_MG = 0.945

    def _incomplete_session(self, settings: Settings) -> Path:
        """pending.json と、それが参照する capture を持つ未完了 session を作る."""
        document = json.loads(
            (
                PROJECT_ROOT / "data/testing/schemas/paste_dataset_pending_v1.json"
            ).read_text(encoding="utf-8")
        )
        session = settings.paste_dataset_dir / f"{self.STEM}.incomplete"
        for phase in ("pre", "post"):
            (session / phase).mkdir(parents=True)
        for target in (*document["samples"], *document["blanks"]):
            for view in target["views"]:
                for phase in ("pre", "post"):
                    (session / view[phase]).write_bytes(b"")
        (session / "pending.json").write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        return session

    def test_finalizes_the_selected_session_without_touching_the_machine(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        incomplete = self._incomplete_session(fake_camera_settings)

        record = manager.start(
            "paste_dataset_finalize", {"measured_mass": self.MEASURED_MASS_MG}
        )
        answer_next_prompt(record, manager, incomplete.name, set())
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.SUCCEEDED, record.error
        session = fake_camera_settings.paste_dataset_dir / self.STEM
        assert not incomplete.exists()
        metadata = json.loads((session / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["schema_version"] == 3
        assert metadata["total"]["measured_mass_mg"] == self.MEASURED_MASS_MG
        # パージが無いので、塗布 sample だけで総体積を分け合う。
        assert metadata["samples"][0]["measured_volume_ul"] == pytest.approx(0.25)
        assert (session / "pre" / "000001.00.png").is_file()
        assert not (session / "pending.json").exists()

    def test_fails_when_no_session_can_be_rescued(
        self, manager: JobManager, wait_until: WaitUntil
    ):
        record = manager.start(
            "paste_dataset_finalize", {"measured_mass": self.MEASURED_MASS_MG}
        )
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert "未完了dataset" in record.error
        assert record.pending_prompt is None

    @pytest.mark.parametrize("mass", [0.0, -1.0])
    def test_rejects_non_positive_mass_before_prompting(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        mass: float,
    ):
        self._incomplete_session(fake_camera_settings)

        record = manager.start("paste_dataset_finalize", {"measured_mass": mass})
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.FAILED
        assert record.pending_prompt is None
