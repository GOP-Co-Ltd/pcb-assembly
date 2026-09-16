"""塗布量校正の事前検証、収集後のフィット・保存・検証。"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import pytest

from pcbasm.pasting.dataset.reader import DatasetSession
from pcbasm.pasting.paste_volume.calibration import (
    CALIBRATION_SUFFIX,
    list_calibrations,
    load_calibration,
    write_calibration,
)
from pcbasm.pasting.paste_volume.fit import fit_session
from tests.helpers import build_paste_volume_session
from tests.web.api.jobs.conftest import ManagerFactory, WaitUntil, register_synthetic
from web.api.jobs.catalog import JobCatalog, ParamSpec
from web.api.jobs.context import JobContext, JobResult
from web.api.jobs.manager import JobManager, JobRecord, JobStatus
from web.api.jobs.pasting.paste_volume_calibration import (
    fit_calibration,
    verify_with_calibration,
)
from web.api.jobs.pasting.paste_volume_common import (
    COLLECTED_SAVE_NAME_PARAM,
    DETECTION_PARAMS,
    REQUIRE_BLANK_ZERO_PARAM,
)
from web.api.settings import Settings


class TestPasteDatasetCollectionPreflight:
    """Dataset収集は装置を開く前に設定・配置・撮影窓を確定する（PCB非依存）."""

    @pytest.mark.parametrize(
        ("params", "expected"),
        [
            # 盤面に載らない本数（容量不足）
            (
                {
                    "plate_width": 12.0,
                    "plate_height": 12.0,
                    "volume_divisions": 5,
                    "samples_per_volume": 5,
                    "blank_count": 4,
                },
                "サンプル",
            ),
            # 同じ session で作った校正を同じ session へ当てると汎化を測れない
            (
                {
                    "save_name": f"self-reference{CALIBRATION_SUFFIX}",
                    "volume_calibration": f"self-reference{CALIBRATION_SUFFIX}",
                },
                "汎化",
            ),
            ({"paste_height": 0.0}, "塗布高さ"),
            ({"view_count": 4, "view_offset": 0.0}, "移動距離"),
            # crop がセルピッチを超えると隣のセルが写り込む
            ({"cell_size": 2.0, "cell_gap": 1.0, "crop_size": 4.0}, "crop"),
            # open_kernel_px は k×k バイトを確保する。下限・上限とも弾く
            ({"open_kernel_px": 4}, "openカーネル"),
            ({"open_kernel_px": 1_048_577}, "openカーネル"),
        ],
    )
    def test_invalid_settings_fail_before_prompt_or_machine(
        self,
        manager: JobManager,
        wait_until: WaitUntil,
        params: dict[str, object],
        expected: str,
    ):
        """1 時間の収集を終えてから設定の不正を知らされては遅い."""
        record = manager.start(
            "paste_volume_calibration", {"paste_id": "paste-1", **params}
        )
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert expected in record.error
        assert record.pending_prompt is None

    def test_a_zero_min_area_is_refused_at_start(self, manager: JobManager):
        """ParamSpec.minimum が受理前に弾く（ジョブは始まりもしない）."""
        with pytest.raises(ValueError, match="min_area_px"):
            manager.start(
                "paste_volume_calibration",
                {"paste_id": "paste-1", "min_area_px": 0},
            )

    def test_valid_settings_reach_confirmations_without_pcb(
        self, manager: JobManager, wait_until: WaitUntil
    ):
        record = manager.start(
            "paste_volume_calibration",
            {
                "paste_id": "paste-1",
                "paste_lot": "lot-1",
                "volume_divisions": 2,
                "samples_per_volume": 1,
                "blank_count": 1,
                "view_count": 1,
            },
        )

        wait_until(lambda: record.pending_prompt is not None, timeout=60.0)
        first = record.pending_prompt
        assert first is not None
        assert "吐出量キャリブレーション" in first[1].message
        manager.respond_prompt(first[0], True)

        wait_until(
            lambda: record.pending_prompt is not None
            and record.pending_prompt[0] != first[0],
            timeout=60.0,
        )
        second = record.pending_prompt
        assert second is not None
        assert "TARE" in second[1].message
        manager.respond_prompt(second[0], False)
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.ABORTED
        assert any("収集計画" in line for line in record.log_lines)


class TestPasteVolumeCalibrationFitStep:
    """収集ジョブの最後で校正まで作る（`fit_calibration`）.

    運転者の目的は校正を作ることなので、収集の終わりに別ページへ移らせない。

    収集本体は装置を要求するので、合成ジョブへ同じ関数を通して確かめる。
    """

    STEM = "plate-47.5x20-20260909T145923.452+0900"

    @pytest.fixture
    def fit_manager(
        self, make_manager: ManagerFactory, catalog: JobCatalog
    ) -> JobManager:
        """`fit_calibration` だけを呼ぶ合成ジョブを積んだ manager."""

        def run(ctx: JobContext) -> JobResult:
            if bool(ctx.params["block_scatter"]):
                # savefig を失敗させる（path が directory なら書けない）
                (ctx.artifacts_dir / "paste_volume_calibration.png").mkdir(
                    parents=True, exist_ok=True
                )
            summary, artifacts = fit_calibration(
                ctx, Path(str(ctx.params["session_path"]))
            )
            return JobResult(summary=summary, artifacts=artifacts)

        register_synthetic(
            catalog,
            run,
            name="fit_calibration",
            params=(
                ParamSpec("session_path", "session", "str"),
                ParamSpec("block_scatter", "図を壊す", "bool", default=False),
                *DETECTION_PARAMS,
                REQUIRE_BLANK_ZERO_PARAM,
                COLLECTED_SAVE_NAME_PARAM,
            ),
        )
        return make_manager(catalog)

    def _run(
        self,
        manager: JobManager,
        wait_until: WaitUntil,
        session: Path,
        **overrides: object,
    ) -> JobRecord:
        record = manager.start(
            "fit_calibration", {"session_path": str(session), **overrides}
        )
        wait_until(lambda: record.status.terminal, timeout=60.0)
        return record

    def test_saves_a_calibration_without_a_second_page(
        self,
        fit_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        """校正ファイルと診断（総体積誤差・図）が収集ジョブの中で揃う."""
        session = build_paste_volume_session(
            fake_camera_settings.paste_dataset_dir / self.STEM
        )

        record = self._run(fit_manager, wait_until, session, save_name="collected")

        assert record.status == JobStatus.SUCCEEDED, record.error
        saved = list_calibrations(fake_camera_settings.paste_volume_calibration_dir)
        assert len(saved) == 1
        calibration, error = load_calibration(saved[0])
        assert error is None, error
        assert calibration is not None
        assert calibration.source.session == self.STEM

        assert record.result is not None
        assert "総体積誤差" in str(record.result.summary)
        assert {a.label for a in record.result.artifacts} >= {
            "直径と体積の散布図",
            "検出モンタージュ",
            "校正ファイル",
        }

    def test_an_empty_save_name_still_saves_under_an_auto_name(
        self,
        fit_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        """1 時間かけた実行の主成果物なので「保存しない」は選ばせない.

        名前を空にしたときはペースト・ノズル径・塗布高さと時刻から自動命名する。
        保存しない選択が要るのは `paste_volume_refit`（ハイパラ探索）の方。
        """
        session = build_paste_volume_session(
            fake_camera_settings.paste_dataset_dir / self.STEM
        )

        record = self._run(fit_manager, wait_until, session)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.result is not None
        assert "保存先" in str(record.result.summary)
        saved = list_calibrations(fake_camera_settings.paste_volume_calibration_dir)
        assert len(saved) == 1
        # metadata のペースト・ノズル径・塗布高さから名前を組む
        assert saved[0].name.startswith("paste-1-n0.34-h0.20-")

    def test_a_blank_false_positive_does_not_lose_the_collection(
        self,
        fit_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        """校正づくりが目的でも、1 時間の収集を FAILED で捨てない.

        ハイパラを変えて作り直すのは `paste_volume_refit` の役目。
        """
        session = build_paste_volume_session(
            fake_camera_settings.paste_dataset_dir / self.STEM,
            blank_material="large",
        )

        record = self._run(fit_manager, wait_until, session, save_name="broken")

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.result is not None
        assert record.result.summary == " / 校正生成失敗"
        assert record.result.artifacts == ()
        assert not fake_camera_settings.paste_volume_calibration_dir.exists()

    def test_a_figure_failure_still_saves_the_calibration(
        self,
        fit_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        """図は診断の補助。描けなくても校正は成果として残す.

        散布図の出力先を directory にして savefig を失敗させる。
        """
        session = build_paste_volume_session(
            fake_camera_settings.paste_dataset_dir / self.STEM
        )
        (fake_camera_settings.webui_data_dir / "jobs").mkdir(
            parents=True, exist_ok=True
        )

        record = self._run(
            fit_manager,
            wait_until,
            session,
            save_name="figures-broken",
            block_scatter=True,
        )

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.result is not None
        assert "診断図なし" in str(record.result.summary)
        assert "保存先" in str(record.result.summary)
        saved = list_calibrations(fake_camera_settings.paste_volume_calibration_dir)
        assert len(saved) == 1

    def test_an_unreadable_session_does_not_lose_the_collection(
        self,
        fit_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        record = self._run(
            fit_manager,
            wait_until,
            fake_camera_settings.paste_dataset_dir / "absent",
            save_name="broken",
        )

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.result is not None
        assert record.result.summary == " / 校正生成失敗"


class TestPasteDatasetVolumeVerification:
    """収集ジョブへ組み込んだ検証（`verify_with_calibration`）.

    検証の失敗で収集結果を失わないことが最優先の契約。

    この時点で 1 時間の収集と計量が終わっている。

    誤差の値そのものは test_evaluate.py が担保する。

    収集ジョブ本体は装置を要求するので、合成ジョブへ同じ関数を通して確かめる。
    """

    STEM = "plate-47.5x20-20260909T145923.452+0900"

    @pytest.fixture
    def verify_manager(
        self, make_manager: ManagerFactory, catalog: JobCatalog
    ) -> JobManager:
        """`verify_with_calibration` だけを呼ぶ合成ジョブを積んだ manager."""

        def run(ctx: JobContext) -> JobResult:
            summary, artifacts = verify_with_calibration(
                ctx, Path(str(ctx.params["session_path"]))
            )
            return JobResult(summary=summary, artifacts=artifacts)

        register_synthetic(
            catalog,
            run,
            name="verify_volume",
            params=(
                ParamSpec("session_path", "session", "str"),
                ParamSpec("volume_calibration", "校正", "str", default=""),
            ),
        )
        return make_manager(catalog)

    def _session(self, settings: Settings) -> Path:
        return build_paste_volume_session(settings.paste_dataset_dir / self.STEM)

    def _calibration(self, settings: Settings) -> str:
        """収集した session から校正を作って保存し、ファイル名を返す."""
        session, error = DatasetSession.load(settings.paste_dataset_dir / self.STEM)
        assert error is None, error
        assert session is not None
        fit, error = fit_session(session)
        assert error is None, error
        assert fit is not None
        name = f"verify{CALIBRATION_SUFFIX}"
        write_calibration(settings.paste_volume_calibration_dir / name, fit.calibration)
        return name

    def _run(
        self,
        manager: JobManager,
        wait_until: WaitUntil,
        session: Path,
        calibration: str,
    ) -> JobRecord:
        record = manager.start(
            "verify_volume",
            {"session_path": str(session), "volume_calibration": calibration},
        )
        wait_until(lambda: record.status.terminal, timeout=60.0)
        assert record.status == JobStatus.SUCCEEDED, record.error
        return record

    def test_verifies_the_collected_session_and_writes_the_report(
        self,
        verify_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        """総体積誤差の summary + JSON レポート + 散布図が成果物として揃う.

        blank と検出失敗はレポート上で別の数として持つ（同じ袋に入れると回帰が見えない）。
        """
        session = self._session(fake_camera_settings)
        name = self._calibration(fake_camera_settings)

        record = self._run(verify_manager, wait_until, session, name)

        assert record.result is not None
        assert "総体積誤差" in str(record.result.summary)
        by_label = {a.label: a for a in record.result.artifacts}
        assert set(by_label) == {"塗布量の検証", "実測と推定の散布図"}

        root = fake_camera_settings.webui_data_dir
        report = json.loads(
            (root / by_label["塗布量の検証"].path).read_text(encoding="utf-8")
        )
        assert report["session"] == self.STEM
        assert report["accepted_count"] > 0
        assert report["detection_failure_count"] == 0
        assert report["blank_count"] == 1
        assert report["dispensed_count"] == report["cell_count"] - 1
        assert cv2.imread(str(root / by_label["実測と推定の散布図"].path)) is not None

    def test_does_nothing_when_no_calibration_is_selected(
        self,
        verify_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        session = self._session(fake_camera_settings)

        record = self._run(verify_manager, wait_until, session, "")

        assert record.result is not None
        assert record.result.summary == ""
        assert record.result.artifacts == ()

    @pytest.mark.parametrize("broken", [False, True])
    def test_an_unusable_calibration_does_not_lose_the_collection(
        self,
        verify_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        broken: bool,
    ):
        """収集は終わっている。後付けの検証で例外を投げてはならない.

        校正が見つからない経路と、読めても parse に失敗する経路の両方。
        """
        session = self._session(fake_camera_settings)
        name = f"{'broken' if broken else 'absent'}{CALIBRATION_SUFFIX}"
        if broken:
            root = fake_camera_settings.paste_volume_calibration_dir
            root.mkdir(parents=True, exist_ok=True)
            (root / name).write_text("{ not json", encoding="utf-8")

        record = self._run(verify_manager, wait_until, session, name)

        assert record.result is not None
        assert record.result.summary == " / 検証失敗"
        assert record.result.artifacts == ()
