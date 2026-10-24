"""塗布系ジョブが実機へ送る G-code の列を、分岐とカメラの回転ごとに golden で固定する.

ジョブは実 JobManager で走らせ、カメラは SceneCamera が fake 機体の位置から描く。
シナリオは scenarios.py の表にあり、golden は ``jobs/<シナリオ名>.gcode`` に置く。
送信列には、ジョブの本体に続けて、終了時の駐機（JobManager が行う）も入る。
数値は OpenCV の検出結果そのもので、Pi 5（aarch64）と uv.lock の opencv-python 4.12.0.88 で採った。
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, override

import pytest
import tomlkit
from shapely import Polygon
from tomlkit.items import Table

from pcbasm.gcode import GCodeLike
from pcbasm.hal import Camera
from pcbasm.hal.fake import FakeAudioPlayer, FakeKlipper, StepperMove
from pcbasm.kicad.build import generate_rect_pcb, save_board
from tests.golden.conftest import (
    CAMERA_MODEL,
    DISPENSER,
    JOBS,
    LED_BLINKER,
    enable_flow_calibration,
    finish,
    scene_camera,
    wait_operator,
)
from tests.golden.scenarios import Scenario, scenarios_of
from tests.support.golden import assert_transcript
from tests.web.api.jobs.conftest import answer_next_prompt
from web.api.env import Env
from web.api.jobs.manager import JobManager, JobStatus
from web.api.jobs.pasting.common import LOADING_STAGE
from web.api.machine_config import MachineConfigFile
from web.api.state import AppState


class CloggedKlipper(FakeKlipper):
    """先端が詰まりうる fake 機体（:meth:`clog` で与えた回数の押し出しではペーストが出ない）.

    押し出し 1 回は、manual_stepper を正の向きに進める ``MOVE`` を含む送信 1 回と数える。
    出なかった送信の ``MOVE`` は ``stepper_moves`` から除くので、SceneCamera は塗布痕を描かない。
    """

    def __init__(self) -> None:
        super().__init__()
        # connection() と共有するので、属性を差し替えずに中身を更新する
        self._clogged = [0.0]
        self._swallowed: set[int] = set()

    def clog(self, times: float) -> None:
        """次の times 回の押し出しでペーストを出さない."""
        self._clogged[0] = times

    @property
    @override
    def stepper_moves(self) -> tuple[StepperMove, ...]:
        return tuple(
            move
            for index, move in enumerate(super().stepper_moves)
            if index not in self._swallowed
        )

    @override
    def send_gcode(
        self, gcode: GCodeLike, *, timeout: float | None = None
    ) -> dict[str, Any]:
        start = len(super().stepper_moves)
        result = super().send_gcode(gcode, timeout=timeout)
        moves = super().stepper_moves[start:]
        if self._clogged[0] > 0 and any(move.distance > 0 for move in moves):
            self._clogged[0] -= 1
            self._swallowed.update(range(start, start + len(moves)))
        return result


def _forget_nozzle_clean(settings: Env) -> None:
    """machine.toml のクリーニング節を座標の無い節にする（設定ページから動作値だけを保存した未教示の機体）."""
    path = MachineConfigFile(settings.config_dir).path
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    dispenser = document["paste_dispenser"]
    assert isinstance(dispenser, Table)
    dispenser["nozzle_clean"] = {"passes": 3}
    path.write_text(tomlkit.dumps(document), encoding="utf-8")


class TestPasteSolder:
    @pytest.fixture
    def machine_klipper(self) -> CloggedKlipper:
        return CloggedKlipper()

    @pytest.fixture
    def run(
        self,
        manager: JobManager,
        job_state: AppState,
        machine_klipper: CloggedKlipper,
        mount_camera: Callable[[Camera], None],
        led_blinker_shapes: list[Polygon],
    ) -> Callable[..., None]:
        """詰まりの回数を与えてはんだ塗布を走らせ、送信列を golden と比べる関数を返す."""

        def run(scenario: Scenario, *, clogged: float = 0) -> None:
            machine_klipper.clog(clogged)
            mount_camera(
                scene_camera(
                    machine_klipper, scenario.rotation, shapes=led_blinker_shapes
                )
            )

            record = manager.start("paste_solder", {"tolerance": 0.1})
            status = JobStatus.SUCCEEDED if scenario.succeeds else JobStatus.FAILED
            finish(job_state, record, status)

            assert_transcript(machine_klipper.sent, JOBS / f"{scenario.name}.gcode")

        return run

    @pytest.mark.parametrize(
        "scenario", scenarios_of("paste_solder", "purge_shows"), ids=lambda s: s.name
    )
    def test_purge_shows(self, run: Callable[..., None], scenario: Scenario):
        """初回パージが写れば、クリーニングせずに全 pad を塗る（運転時補正なし）."""
        run(scenario)

    @pytest.mark.parametrize(
        "scenario", scenarios_of("paste_solder", "cleaned"), ids=lambda s: s.name
    )
    def test_cleaned(self, run: Callable[..., None], scenario: Scenario):
        """初回パージが写らなければ、クリーニングしてパージし直し、写れば塗る."""
        run(scenario, clogged=1)

    @pytest.mark.parametrize(
        "scenario", scenarios_of("paste_solder", "still_clogged"), ids=lambda s: s.name
    )
    def test_still_clogged(self, run: Callable[..., None], scenario: Scenario):
        """クリーニングしてパージし直しても写らなければ、pad を塗らずに失敗する."""
        run(scenario, clogged=float("inf"))

    @pytest.mark.parametrize(
        "scenario",
        scenarios_of("paste_solder", "no_nozzle_clean"),
        ids=lambda s: s.name,
    )
    def test_no_nozzle_clean(
        self,
        run: Callable[..., None],
        job_settings: Env,
        scenario: Scenario,
    ):
        """クリーニング位置が未教示なら、こすらずにパージだけやり直す."""
        _forget_nozzle_clean(job_settings)

        run(scenario, clogged=1)

    @pytest.mark.parametrize(
        "scenario", scenarios_of("paste_solder", "flow_corrected"), ids=lambda s: s.name
    )
    def test_flow_corrected(
        self,
        run: Callable[..., None],
        job_settings: Env,
        job_state: AppState,
        scenario: Scenario,
    ):
        """運転時補正の測定点を塗って撮り、補正した rotations_per_ul で pad を塗る."""
        enable_flow_calibration(job_settings, job_state, LED_BLINKER)

        run(scenario)

    @pytest.mark.parametrize(
        "scenario", scenarios_of("paste_solder", "pad_refined"), ids=lambda s: s.name
    )
    def test_pad_refined(
        self, run: Callable[..., None], job_settings: Env, scenario: Scenario
    ):
        """短辺 0.6 mm 以下の pad は、領域の補正を初期値に pad 中心で照合し直し、その結果で塗る."""
        MachineConfigFile(job_settings.config_dir).write(
            {"alignment.refine_max_short_side": 0.6}
        )

        run(scenario)


class TestHeightPlane:
    @pytest.mark.parametrize(
        "scenario", scenarios_of("height_plane"), ids=lambda s: s.name
    )
    def test_success(
        self,
        manager: JobManager,
        job_state: AppState,
        machine_klipper: FakeKlipper,
        mount_camera: Callable[[Camera], None],
        led_blinker_shapes: list[Polygon],
        scenario: Scenario,
    ):
        """計画点を確かめてから、基板計測 → TOP 銅箔の高さ計測."""
        mount_camera(
            scene_camera(machine_klipper, scenario.rotation, shapes=led_blinker_shapes)
        )

        record = manager.start("height_plane", {"tolerance": 0.1})
        answer_next_prompt(record, manager, True, set())
        finish(job_state, record)

        assert_transcript(machine_klipper.sent, JOBS / f"{scenario.name}.gcode")


class TestToolheadOffset:
    @pytest.fixture
    def plate_shapes(
        self,
        tmp_path: Path,
        place_board: Callable[[Path], list[Polygon]],
    ) -> list[Polygon]:
        """未塗布の銅板（外形だけの 30 mm 角）を選び、置いた基板の基準円を返す."""
        path = tmp_path / "plate.kicad_pcb"
        save_board(generate_rect_pcb(30.0, 30.0), path)
        return place_board(path)

    @pytest.mark.parametrize(
        "scenario", scenarios_of("toolhead_offset"), ids=lambda s: s.name
    )
    def test_success(
        self,
        manager: JobManager,
        job_state: AppState,
        machine_klipper: FakeKlipper,
        audio_player: FakeAudioPlayer,
        mount_camera: Callable[[Camera], None],
        plate_shapes: list[Polygon],
        scenario: Scenario,
    ):
        """基板計測 → 全点の高さ計測 → ローディング → 全点に塗布 → 全点を撮って測る."""
        mount_camera(
            scene_camera(machine_klipper, scenario.rotation, shapes=plate_shapes)
        )

        record = manager.start("toolhead_offset", {"point_count": 5})
        wait_operator(record, audio_player, LOADING_STAGE)
        manager.submit_command({"type": "finish"})
        finish(job_state, record)

        # SceneCamera は machine.toml の toolhead だけずらして塗布痕を描くので、1 画素以内でそれを測り返す
        values = manager.apply_payload().values
        pixel = 1.0 / CAMERA_MODEL.pixel_per_mm
        assert values["paste_dispenser.toolhead.x"] == pytest.approx(
            DISPENSER.toolhead.x, abs=pixel
        )
        assert values["paste_dispenser.toolhead.y"] == pytest.approx(
            DISPENSER.toolhead.y, abs=pixel
        )
        assert_transcript(machine_klipper.sent, JOBS / f"{scenario.name}.gcode")


class TestLoading:
    @pytest.mark.parametrize("scenario", scenarios_of("loading"), ids=lambda s: s.name)
    def test_commands(
        self,
        manager: JobManager,
        job_state: AppState,
        machine_klipper: FakeKlipper,
        audio_player: FakeAudioPlayer,
        scenario: Scenario,
    ):
        """ホーミング → 指定 X へ移動 → 押出・吸引・回転（引き戻し付き）・jog・焦点 Z → 終了."""
        record = manager.start("loading", {"position_x": 50.0})
        wait_operator(record, audio_player, LOADING_STAGE)
        for command in (
            {"type": "extrude", "amount": 0.1},
            {"type": "suck", "amount": 0.05},
            {
                "type": "extrude_rotations",
                "rotations": 5.0,
                "rate": 0.5,
                "accel": 0.5,
                "retract_rotations": 1.0,
            },
            {"type": "suck_rotations", "rotations": 2.0, "rate": 0.5, "accel": 0.5},
            {"type": "jog", "axis": "z", "distance": 10.0},
            {"type": "focus_z"},
            {"type": "finish"},
        ):
            manager.submit_command(command)
        finish(job_state, record)

        assert_transcript(machine_klipper.sent, JOBS / f"{scenario.name}.gcode")
