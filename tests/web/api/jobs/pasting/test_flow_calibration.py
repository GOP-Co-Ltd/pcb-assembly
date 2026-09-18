"""運転時流量キャリブレーション（`web.api.jobs.pasting.flow_calibration`）の契約.

最優先は **補正できなくても塗布ジョブを落とさない**こと。この時点で位置合わせと
高さ計測が終わっており、後から足した補正の失敗で塗布を取りやめるのは割に合わない。

はんだ塗布ジョブ本体は装置を要求するので、合成ジョブへ同じ関数を通して確かめる
（`TestPasteDatasetVolumeVerification` と同じ形）。装置側は自前 HAL の fake。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pcbasm.config import FlowCalibration
from pcbasm.geometry import Point2d
from pcbasm.pasting.paste_volume.calibration import (
    CALIBRATION_SUFFIX,
    parse_calibration,
    write_calibration,
)
from pcbasm.pasting.paste_volume.runtime import (
    FlowCalibrationOutcome,
    plan_flow_calibration,
)
from tests.helpers import TESTING_DATA_DIR, FakeCamera, FakeKlipper
from tests.web.api.jobs.conftest import (
    ManagerFactory,
    WaitUntil,
    register_synthetic,
)
from tests.web.api.jobs.pasting.conftest import (
    FOCUS_Z,
    full_board_correction,
    paste_session,
    uniform_frame,
)
from web.api.jobs.catalog import JobCatalog, ParamSpec
from web.api.jobs.context import JobContext, JobResult
from web.api.jobs.manager import JobManager, JobRecord, JobStatus
from web.api.jobs.pasting.flow_calibration import run_flow_calibration, summary_line
from web.api.settings import Settings

CALIBRATION_PIN = TESTING_DATA_DIR / "schemas" / "paste_volume_calibration_v1.json"
POINTS = (Point2d(10.0, 6.0), Point2d(13.0, 6.0), Point2d(16.0, 6.0))


def _outcome(**overrides: object) -> FlowCalibrationOutcome:
    values: dict[str, object] = {
        "commanded_ul": 0.6,
        "estimated_ul": 0.75,
        "ratio": 1.25,
        "previous_rotations_per_ul": 1.0,
        "rotations_per_ul": 0.8,
        "clamped": False,
        "accepted_count": 3,
        "rejections": (),
    }
    values.update(overrides)
    return FlowCalibrationOutcome(**values)  # pyright: ignore[reportArgumentType]


class TestSummaryLine:
    """補正結果の 1 行表示（ログと JobResult.summary で共用する）."""

    def test_carries_the_factor_change_and_the_accepted_count(self):
        line = summary_line(_outcome())

        assert "3 点採用" in line
        assert "1.0000" in line
        assert "0.8000" in line

    def test_marks_a_clamped_correction(self):
        assert "頭打ち" in summary_line(_outcome(clamped=True))


class TestRunFlowCalibration:
    """撮影 → 塗布 → 撮影 → 推定 → 補正の実行."""

    @pytest.fixture
    def moves(self) -> list[float | None]:
        """合成ジョブが記録した G1 の Z（撮影パスと塗布パスの並びを見る）."""
        return []

    @pytest.fixture
    def elapsed(self) -> list[float]:
        """合成ジョブ内で `run_flow_calibration` にかかった秒数."""
        return []

    @pytest.fixture
    def flow_manager(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        moves: list[float | None],
        elapsed: list[float],
    ) -> JobManager:
        """`run_flow_calibration` だけを呼ぶ合成ジョブを積んだ manager."""

        def run(ctx: JobContext) -> JobResult:
            klipper = FakeKlipper()
            session = paste_session(FakeCamera([uniform_frame()]), klipper)
            config = FlowCalibration(
                calibration_file=str(ctx.params["calibration_file"]),
                settle_seconds=float(ctx.params["settle_seconds"]),
            )
            plan, error = plan_flow_calibration(
                config=config, points=POINTS, outline=session.pcb.outline.polygon
            )
            assert error is None, error
            assert plan is not None
            with session.make_applicator() as applicator:
                klipper.clear_sent()
                started = time.monotonic()
                outcome = run_flow_calibration(
                    ctx, session, full_board_correction(), applicator, plan
                )
                elapsed.append(time.monotonic() - started)
                moves.extend(move.get("z") for move in klipper.g1_moves())
            return JobResult(
                summary="補正なし" if outcome is None else summary_line(outcome)
            )

        register_synthetic(
            catalog,
            run,
            name="flow_calibration",
            params=(
                ParamSpec("calibration_file", "校正", "str", default=""),
                ParamSpec("settle_seconds", "静定待ち", "float", default=0.0),
            ),
        )
        return make_manager(catalog)

    @staticmethod
    def _write_calibration(settings: Settings) -> str:
        """検証用のピン済み校正を保存し、ファイル名を返す."""
        calibration, error = parse_calibration(
            json.loads(CALIBRATION_PIN.read_text(encoding="utf-8"))
        )
        assert error is None, error
        assert calibration is not None
        name = f"runtime{CALIBRATION_SUFFIX}"
        write_calibration(settings.paste_volume_calibration_dir / name, calibration)
        return name

    @staticmethod
    def _run(
        manager: JobManager,
        wait_until: WaitUntil,
        name: str,
        *,
        settle_seconds: float = 0.0,
    ) -> JobRecord:
        record = manager.start(
            "flow_calibration",
            {"calibration_file": name, "settle_seconds": settle_seconds},
        )
        wait_until(lambda: record.status.terminal, timeout=60.0)
        return record

    def test_a_missing_calibration_file_does_not_fail_the_job(
        self,
        flow_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        record = self._run(flow_manager, wait_until, "absent.paste-volume.json")

        assert record.status == JobStatus.SUCCEEDED, record.error

    def test_undetectable_dots_do_not_fail_the_job(
        self,
        flow_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        """一様なフレームでは塗布前後の差が無い。補正せず進む."""
        name = self._write_calibration(fake_camera_settings)

        record = self._run(flow_manager, wait_until, name)

        assert record.status == JobStatus.SUCCEEDED, record.error

    def test_captures_every_point_before_dispensing_and_again_after(
        self,
        flow_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        moves: list[float | None],
    ):
        """3 パス構成。塗ってすぐ撮ると点ごとに落ち着き時間が変わる."""
        name = self._write_calibration(fake_camera_settings)

        record = self._run(flow_manager, wait_until, name)

        assert record.status == JobStatus.SUCCEEDED, record.error
        focus = [
            index
            for index, z in enumerate(moves)
            if z is not None and z == pytest.approx(FOCUS_Z)
        ]
        assert len(focus) == 6
        # 先頭 3 つが pre パス、末尾 3 つが post パス。間に塗布の移動が挟まる
        assert focus[:3] == [0, 1, 2]
        assert focus[3:] == [len(moves) - 3, len(moves) - 2, len(moves) - 1]

    def test_waits_for_the_paste_to_settle_before_the_post_pass(
        self,
        flow_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        elapsed: list[float],
    ):
        """塗り終えてすぐ撮ると、広がりきる前の小さい円を測ることになる."""
        name = self._write_calibration(fake_camera_settings)

        record = self._run(flow_manager, wait_until, name, settle_seconds=1.5)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert elapsed
        assert elapsed[0] >= 1.5
