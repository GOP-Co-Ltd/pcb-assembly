"""位置合わせ系ジョブが実機へ送る G-code の列を、カメラの回転ごとに golden で固定する.

ジョブは実 JobManager で走らせ、カメラは SceneCamera が fake 機体の位置から描く。
シナリオは scenarios.py の表にあり、golden は ``jobs/<シナリオ名>.gcode`` に置く。
送信列には、ジョブの本体に続けて、終了時の駐機（JobManager が行う）も入る。
数値は OpenCV の検出結果そのもので、Pi 5（aarch64）と uv.lock の opencv-python 4.12.0.88 で採った。
"""

import json
from collections.abc import Callable

import pytest
from shapely import Point, Polygon

from pcbasm.geometry import Point2d
from pcbasm.hal import Camera
from pcbasm.hal.fake import Checkerboard, FakeKlipper
from pcbasm.vision import CalibrationResult
from tests.golden.conftest import CALIBRATION, JOBS, finish, scene_camera
from tests.golden.scenarios import Scenario, scenarios_of
from tests.support.golden import assert_transcript
from tests.web.api.jobs.conftest import answer_next_prompt
from web.api.jobs.manager import JobManager
from web.api.state import AppState

FOCUS_Z = CALIBRATION.z_position


class TestReferencePointSetup:
    @pytest.mark.parametrize(
        "scenario", scenarios_of("reference_point_setup"), ids=lambda s: s.name
    )
    def test_jog_then_record(
        self,
        manager: JobManager,
        job_state: AppState,
        machine_klipper: FakeKlipper,
        mount_camera: Callable[[Camera], None],
        scenario: Scenario,
    ):
        """ホーミングしてカメラの Z へ上げ、ジョグした位置を基準点として記録する."""
        marker = Point(8.0, 6.0).buffer(1.5, 32)
        mount_camera(scene_camera(machine_klipper, scenario.rotation, shapes=[marker]))

        record = manager.start("reference_point_setup", {})
        for command in (
            {"type": "jog", "axis": "x", "dist": 8.0},
            {"type": "jog", "axis": "y", "dist": 6.0},
            {"type": "record"},
        ):
            manager.submit_command(command)
        finish(job_state, record)

        reference = job_state.machine().reference_point
        assert (reference.x, reference.y) == (8.0, 6.0)
        assert_transcript(machine_klipper.sent, JOBS / f"{scenario.name}.gcode")


class TestCameraCalibration:
    @pytest.mark.parametrize(
        "scenario", scenarios_of("camera_calibration"), ids=lambda s: s.name
    )
    def test_checkerboard(
        self,
        manager: JobManager,
        job_state: AppState,
        machine_klipper: FakeKlipper,
        mount_camera: Callable[[Camera], None],
        scenario: Scenario,
    ):
        """真下のチェッカーボードから pixel/mm を測り、撮った Z と一緒に反映候補にする."""
        machine_klipper.set_status(
            "gcode_move", "gcode_position", [100.0, 80.0, FOCUS_Z, 0.0]
        )
        board = Checkerboard(center=Point2d(100.0, 80.0), square=1.5)
        mount_camera(
            scene_camera(machine_klipper, scenario.rotation, checkerboard=board)
        )

        record = manager.start("camera_calibration", {"square_size": 1.5})
        answer_next_prompt(record, manager, True, set())
        finish(job_state, record)

        (file,) = manager.apply_payload().files
        result = CalibrationResult.from_dict(json.loads(file.content))
        assert result.pixel_per_mm == pytest.approx(CALIBRATION.pixel_per_mm, rel=1e-3)
        assert result.z_position == FOCUS_Z
        assert_transcript(machine_klipper.sent, JOBS / f"{scenario.name}.gcode")


class TestBoardTour:
    @pytest.mark.parametrize(
        "scenario", scenarios_of("board_tour"), ids=lambda s: s.name
    )
    def test_success(
        self,
        manager: JobManager,
        job_state: AppState,
        machine_klipper: FakeKlipper,
        mount_camera: Callable[[Camera], None],
        grid_shapes: list[Polygon],
        scenario: Scenario,
    ):
        """基板計測 → 四隅巡回 → 銅箔照合 → pad 中心照合つきの補正巡回 → 原点へ戻る."""
        # 回転 0 と 0.3° では一部の領域と pad の照合が収束せず、その補正を使わずに進む経路も通る。
        # SceneCamera が縁を AA で描くときに起こり（LINE_8 なら 3 通りとも収束）、実機でも起こるかは未検証
        mount_camera(
            scene_camera(machine_klipper, scenario.rotation, shapes=grid_shapes)
        )

        finish(job_state, manager.start("board_tour", {"tolerance": 0.1}))

        assert_transcript(machine_klipper.sent, JOBS / f"{scenario.name}.gcode")


class TestOrthogonalityTest:
    @pytest.mark.parametrize(
        "scenario", scenarios_of("orthogonality_test"), ids=lambda s: s.name
    )
    def test_one_cycle(
        self,
        manager: JobManager,
        job_state: AppState,
        machine_klipper: FakeKlipper,
        mount_camera: Callable[[Camera], None],
        grid_shapes: list[Polygon],
        scenario: Scenario,
    ):
        """基板計測のあと四隅と 4 pad を 1 周し、2 周目の最初の点で終える."""
        mount_camera(
            scene_camera(machine_klipper, scenario.rotation, shapes=grid_shapes)
        )

        record = manager.start("orthogonality_test", {"tolerance": 0.1})
        answered: set[str] = set()
        for answer in [True] * 8 + [False]:
            answer_next_prompt(record, manager, answer, answered)
        finish(job_state, record)

        assert_transcript(machine_klipper.sent, JOBS / f"{scenario.name}.gcode")
