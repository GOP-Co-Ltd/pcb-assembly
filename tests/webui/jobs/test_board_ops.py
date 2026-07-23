"""`webui.jobs.board_ops` のpad照合executor仕様テスト.

paste-align-max-failures 計画書「公開 IF」節が契約:

- max_failures is None → None（既存Component用APIの無制限モード）
- 失敗数 <= 許容数 → None（境界: 失敗数 == 許容数は許容）
- 超過 → 失敗数・許容数・全 designator を含む日本語メッセージ文字列
  （メッセージは部分一致で検証する。完全一致は禁止）

銅箔padサンプリング計画のexecutorは、実 ``PadAlignmentSession`` と実OpenCV
matcherへ合成画像を通し、HALだけをtest実装・mockへ差し替えて検証する。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import attrs
import numpy as np
import pytest
from pytest_mock import MockerFixture

from pcbasm import gcode
from pcbasm.config import Machine
from pcbasm.geometry import Identity, Point3d
from pcbasm.pcb import Layer, PcbFile
from pcbasm.posctrl import (
    BoardCalibrationResult,
    CopperProjector,
    PadAlignmentCandidates,
    PadAlignmentSession,
    PadAlignmentTarget,
)
from pcbasm.vision import CalibrationResult, Image
from tests.helpers import PROJECT_ROOT, FakeCamera
from webui.jobs.board_ops import (
    PadAlignmentExecution,
    align_pad_targets,
    pad_align_abort_message,
)
from webui.jobs.catalog import JobCatalog
from webui.jobs.context import JobResult
from webui.jobs.manager import JobManager, JobRecord, JobStatus

from .conftest import ManagerFactory, WaitUntil, register_synthetic

PPM = 10.0
IMAGE_SIZE = (640, 480)


@attrs.frozen
class _AlignmentResource:
    pcb: PcbFile
    target: PadAlignmentTarget
    matching_image: Image


@pytest.fixture(scope="module")
def alignment_resource() -> _AlignmentResource:
    """実KiCad PCBを投影した、matcherと幾何が一致する合成画像."""
    pcb = PcbFile(
        PROJECT_ROOT / "data" / "testing" / "led_blinker" / "led_blinker.kicad_pcb"
    )
    pad = next(pad for pad in pcb.pads if pad.layer == Layer.TOP)
    target = PadAlignmentTarget(identifier="sample", pad=pad)
    projector = CopperProjector(
        polygons=[copper.polygon for copper in pcb.copper if copper.layer == Layer.TOP],
        board_transform=Identity(),
        offset_transform=Identity(),
        pixel_per_mm=PPM,
        image_size=IMAGE_SIZE,
    )
    projection = projector.project(target.position)
    return _AlignmentResource(
        pcb=pcb,
        target=target,
        matching_image=Image(projection.fill_mask),
    )


def _machine() -> Machine:
    return Machine(PROJECT_ROOT / "configs" / "test-fixture" / "machine.toml")


def _calibration() -> CalibrationResult:
    return CalibrationResult(
        pixel_per_mm=PPM,
        square_size_mm=1.0,
        mean_distance_px=PPM,
        std_distance_px=0.0,
        resolution=IMAGE_SIZE,
        crop_size=IMAGE_SIZE,
        calibrated_at=datetime.now(),
        z_position=5.0,
    )


def _session(
    resource: _AlignmentResource,
    images: list[Image],
    mocker: MockerFixture,
) -> PadAlignmentSession:
    """Camera/klipper/stageだけをHAL test doubleにした実照合session."""
    state = {"position": Point3d(0.0, 0.0, 5.0)}
    stage = mocker.Mock()
    stage.max_velocity = 100.0

    def move(**kwargs) -> gcode.GCode:
        current = state["position"]
        state["position"] = Point3d(
            kwargs.get("x", current.x),
            kwargs.get("y", current.y),
            kwargs.get("z", current.z),
        )
        return gcode.GCode("G1")

    stage.move.side_effect = move
    stage.get_position.side_effect = lambda: state["position"]
    result = BoardCalibrationResult(
        machine=_machine(),
        klipper=mocker.Mock(),
        stage=stage,
        camera=FakeCamera(images),
        calibration=_calibration(),
        offset_transform=Identity(),
        board_transform=Identity(),
        pcb=resource.pcb,
    )
    return PadAlignmentSession.from_calibration(result)


def _candidates(target: PadAlignmentTarget, count: int) -> PadAlignmentCandidates:
    return PadAlignmentCandidates(
        targets=tuple(
            attrs.evolve(target, identifier=f"sample-{index}")
            for index in range(1, count + 1)
        ),
        preferred_component_count=min(count, 1),
        rejected_count=0,
    )


def _start_executor_job(
    *,
    make_manager: ManagerFactory,
    catalog: JobCatalog,
    wait_until: WaitUntil,
    session: PadAlignmentSession,
    candidates: PadAlignmentCandidates,
    sample_count: int,
    max_failures: int = 3,
) -> tuple[JobRecord, list[PadAlignmentExecution]]:
    executions: list[PadAlignmentExecution] = []

    def run(ctx) -> JobResult:
        execution = align_pad_targets(
            ctx,
            session,
            candidates,
            board_transform=Identity(),
            sample_count=sample_count,
            max_failures=max_failures,
        )
        executions.append(execution)
        return JobResult(summary=f"成功 {execution.success_count}")

    register_synthetic(catalog, run, name="pad_alignment_executor")
    manager = make_manager(catalog)
    record = manager.start("pad_alignment_executor", {})
    wait_until(lambda: record.status.terminal, timeout=60.0)
    return record, executions


def _frames(resource: _AlignmentResource, outcomes: list[bool]) -> list[Image]:
    black = Image(np.zeros((IMAGE_SIZE[1], IMAGE_SIZE[0], 3), dtype=np.uint8))
    return [resource.matching_image if succeeds else black for succeeds in outcomes]


class TestAlignPadTargets:
    """目標成功数型executorの補充・停止条件."""

    def test_failure_is_replenished_by_next_candidate(
        self,
        alignment_resource: _AlignmentResource,
        mocker: MockerFixture,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        session = _session(
            alignment_resource,
            _frames(alignment_resource, [False, True, True]),
            mocker,
        )

        record, executions = _start_executor_job(
            make_manager=make_manager,
            catalog=catalog,
            wait_until=wait_until,
            session=session,
            candidates=_candidates(alignment_resource.target, 3),
            sample_count=2,
        )

        assert record.status == JobStatus.SUCCEEDED, record.error
        execution = executions[0]
        assert execution.success_count == 2
        assert execution.failure_count == 1
        assert execution.attempt_count == 3
        assert execution.failed_identifiers == ("sample-1",)

    def test_stops_without_trying_remaining_candidates_after_target_reached(
        self,
        alignment_resource: _AlignmentResource,
        mocker: MockerFixture,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        session = _session(
            alignment_resource,
            _frames(alignment_resource, [True, True, False]),
            mocker,
        )

        record, executions = _start_executor_job(
            make_manager=make_manager,
            catalog=catalog,
            wait_until=wait_until,
            session=session,
            candidates=_candidates(alignment_resource.target, 3),
            sample_count=2,
        )

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert executions[0].attempt_count == 2
        log = "\n".join(record.log_lines)
        assert "sample-1:" in log
        assert "sample-2:" in log
        assert "sample-3" not in log

    def test_three_failures_continue_and_fourth_failure_aborts(
        self,
        alignment_resource: _AlignmentResource,
        mocker: MockerFixture,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        allowed_session = _session(
            alignment_resource,
            _frames(alignment_resource, [False, False, False, True]),
            mocker,
        )
        allowed_record, executions = _start_executor_job(
            make_manager=make_manager,
            catalog=catalog,
            wait_until=wait_until,
            session=allowed_session,
            candidates=_candidates(alignment_resource.target, 4),
            sample_count=1,
            max_failures=3,
        )

        assert allowed_record.status == JobStatus.SUCCEEDED, allowed_record.error
        assert executions[0].failure_count == 3
        assert executions[0].attempt_count == 4

        abort_catalog = JobCatalog()
        abort_session = _session(
            alignment_resource,
            _frames(alignment_resource, [False, False, False, False, False]),
            mocker,
        )
        aborted, aborted_executions = _start_executor_job(
            make_manager=make_manager,
            catalog=abort_catalog,
            wait_until=wait_until,
            session=abort_session,
            candidates=_candidates(alignment_resource.target, 5),
            sample_count=1,
            max_failures=3,
        )

        assert aborted.status == JobStatus.FAILED
        assert aborted_executions == []
        assert aborted.error is not None
        assert "失敗 4" in aborted.error
        assert "許容 3" in aborted.error
        log = "\n".join(aborted.log_lines)
        assert "sample-4" in log
        assert "sample-5" not in log

    def test_candidate_exhaustion_below_runtime_target_aborts(
        self,
        alignment_resource: _AlignmentResource,
        mocker: MockerFixture,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        session = _session(
            alignment_resource,
            _frames(alignment_resource, [True, False]),
            mocker,
        )

        record, executions = _start_executor_job(
            make_manager=make_manager,
            catalog=catalog,
            wait_until=wait_until,
            session=session,
            candidates=_candidates(alignment_resource.target, 2),
            sample_count=2,
        )

        assert record.status == JobStatus.FAILED
        assert executions == []
        assert record.error is not None
        assert "候補を使い切り" in record.error
        assert "成功 1" in record.error
        assert "目標 2" in record.error

    def test_static_candidate_shortage_lowers_runtime_target(
        self,
        alignment_resource: _AlignmentResource,
        mocker: MockerFixture,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        session = _session(
            alignment_resource,
            _frames(alignment_resource, [True, True]),
            mocker,
        )

        record, executions = _start_executor_job(
            make_manager=make_manager,
            catalog=catalog,
            wait_until=wait_until,
            session=session,
            candidates=_candidates(alignment_resource.target, 2),
            sample_count=10,
        )

        assert record.status == JobStatus.SUCCEEDED, record.error
        execution = executions[0]
        assert execution.configured_target_count == 10
        assert execution.target_count == 2
        assert execution.success_count == 2
        assert any("10 から安全候補数 2" in line for line in record.log_lines)


class TestPadAlignAbortMessage:
    """pad_align_abort_message: 失敗数が許容数を超えたときだけ中止メッセージを返す."""

    @pytest.mark.parametrize(
        ("failed", "max_failures"),
        [
            ([], 0),
            (["R1"], 1),
            (["R1", "R2"], 2),  # 境界: 失敗数 == 許容数は許容
        ],
    )
    def test_within_limit_returns_none(self, failed: list[str], max_failures: int):
        assert pad_align_abort_message(failed, max_failures) is None

    def test_none_max_failures_means_unlimited(self):
        assert pad_align_abort_message(["R1", "R2", "R3"], None) is None

    def test_exceeding_limit_returns_message_with_counts_and_designator(self):
        message = pad_align_abort_message(["R1"], 0)

        assert message is not None
        assert "失敗 1" in message
        assert "許容 0" in message
        assert "R1" in message

    def test_message_lists_every_failed_designator(self):
        message = pad_align_abort_message(["R1", "C3", "U2"], 2)

        assert message is not None
        assert "失敗 3" in message
        assert "許容 2" in message
        assert "R1" in message
        assert "C3" in message
        assert "U2" in message
