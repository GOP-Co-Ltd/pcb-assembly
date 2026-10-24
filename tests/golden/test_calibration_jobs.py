"""校正系ジョブが実機へ送る G-code の列と、装置を使わない校正ジョブの成果物を golden で固定する.

ジョブは実 JobManager で走らせ、カメラは SceneCamera が fake 機体の位置から描く。
シナリオは scenarios.py の表にあり、golden は ``jobs/<シナリオ名>.gcode`` に置く。
送信列には、ジョブの本体に続けて、終了時の駐機（JobManager が行う）も入る。
装置を使わないジョブは、送信 0 行と成果物の JSON（``artifacts/``）で固定する。
運転時補正は送信列に表れない補正値も JSON（``artifacts/``）で固定する。
数値は OpenCV の検出結果そのもので、Pi 5（aarch64）と uv.lock の opencv-python 4.12.0.88 で採った。
"""

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import attrs
import pytest
from shapely import Polygon

from pcbasm.geometry import Identity
from pcbasm.hal import Camera, XYZStage
from pcbasm.hal.fake import FakeAudioPlayer, FakeKlipper
from pcbasm.pasting.paste_volume.calibration import CALIBRATION_SUFFIX
from pcbasm.pasting.paste_volume.runtime import (
    FlowCalibrationOutcome,
    plan_flow_calibration,
)
from pcbasm.pasting.session import PasteSession
from pcbasm.pcb import PcbFile
from pcbasm.pcb.generate import generate_rect_pcb, save_board
from pcbasm.posctrl import BoardCalibrationResult
from tests.golden.conftest import (
    BOARD_PLACEMENT,
    CALIBRATION,
    FLOW_POINTS,
    GOLDEN_DIR,
    JOBS,
    LED_BLINKER,
    board_shapes,
    enable_flow_calibration,
    finish,
    scene_camera,
    wait_operator,
)
from tests.golden.scenarios import Scenario, scenarios_of
from tests.helpers import build_paste_volume_session
from tests.support import polling
from tests.support.golden import assert_document, assert_transcript
from tests.support.paths import TESTING_DATA_DIR
from tests.web.api.jobs.conftest import answer_next_prompt, register_synthetic
from tests.web.api.jobs.pasting.conftest import full_board_correction
from web.api.jobs.catalog import JobCatalog
from web.api.jobs.context import JobContext
from web.api.jobs.manager import JobManager, JobRecord
from web.api.jobs.pasting.common import LOADING_STAGE
from web.api.jobs.pasting.dispense_calibration import CALIBRATION_MENU_STAGE
from web.api.jobs.pasting.flow_calibration import run_flow_calibration
from web.api.settings import Settings
from web.api.state import AppState

ARTIFACTS = GOLDEN_DIR / "artifacts"
PENDING_PIN = TESTING_DATA_DIR / "schemas" / "paste_dataset_pending_v1.json"
# 保存名に時刻を足させない（suffix まで書いた名前はそのまま保存名になる）
SAVE_NAME = f"golden{CALIBRATION_SUFFIX}"


def _mask_created_at(calibration: dict[str, Any]) -> dict[str, Any]:
    """校正の生成時刻（created_at と、label 末尾のその地方時）を伏せる（実行ごとに変わる）."""
    created_at = datetime.fromisoformat(calibration["created_at"])
    local = created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    label = calibration["label"]
    assert label.endswith(local)
    return {
        **calibration,
        "created_at": "<created_at>",
        "label": label.removesuffix(local) + "<created_at>",
    }


@pytest.fixture
def plate_shapes(
    tmp_path: Path, job_state: AppState
) -> Callable[[float, float], list[Polygon]]:
    """ジョブがその場で作る銅板（外形だけ）を置いたときの基準円を返す関数を返す."""

    def shapes(width: float, height: float) -> list[Polygon]:
        path = tmp_path / f"plate_{width:g}x{height:g}.kicad_pcb"
        save_board(generate_rect_pcb(width, height), path)
        return board_shapes(PcbFile(path), job_state.machine())

    return shapes


class TestDispenseCalibration:
    @pytest.mark.parametrize(
        "scenario", scenarios_of("dispense_calibration"), ids=lambda s: s.name
    )
    def test_all(
        self,
        manager: JobManager,
        job_state: AppState,
        machine_klipper: FakeKlipper,
        audio_player: FakeAudioPlayer,
        mount_camera: Callable[[Camera], None],
        plate_shapes: Callable[[float, float], list[Polygon]],
        scenario: Scenario,
    ):
        """メニューの「すべて」で ① → ② → ③ を続けて回し、採った値を machine.toml へ反映して終える."""
        mount_camera(
            scene_camera(
                machine_klipper, scenario.rotation, shapes=plate_shapes(40.0, 40.0)
            )
        )

        record = manager.start("dispense_calibration", {})
        wait_operator(record, audio_player, CALIBRATION_MENU_STAGE)
        manager.submit_command({"type": "run_calib", "which": "all"})
        answered: set[str] = set()
        # ① プライムしてからタール → 10 本の線 → 計量 → 採用して ② へ
        wait_operator(record, audio_player, LOADING_STAGE, calls=2)
        manager.submit_command({"type": "extrude", "amount": 0.1})
        manager.submit_command({"type": "finish"})
        for answer in (True, 18.0, "採用して他のキャリブレーションへ進む"):
            answer_next_prompt(record, manager, answer, answered)
        # ② レートごとにタール → 線 → 計量。速いほど出なくなる質量を与え、採る値は手で入れる
        for mass in (1.0, 1.9, 2.8, 3.4, 3.6, 3.7):
            for answer in (True, mass):
                answer_next_prompt(record, manager, answer, answered)
        answer_next_prompt(record, manager, 3.0, answered)
        # ③ 速度を振った線を引き、きれいに引けた最大の番号を選ぶ
        answer_next_prompt(record, manager, "3: 6.40 mm/s", answered)
        manager.submit_command({"type": "run_calib", "which": "finish"})
        finish(job_state, record)

        dispenser = job_state.machine().paste_dispenser
        assert dispenser.rotations_per_ul == pytest.approx(48.07229)
        assert dispenser.motion.max_dispense_rate == 3.0
        assert dispenser.motion.max_fill_speed == pytest.approx(6.4)
        assert_transcript(machine_klipper.sent, JOBS / f"{scenario.name}.gcode")


class TestPasteVolumeCalibration:
    @pytest.mark.parametrize(
        "scenario",
        scenarios_of("paste_volume_calibration"),
        ids=lambda s: s.name,
    )
    def test_small_grid(
        self,
        manager: JobManager,
        job_state: AppState,
        job_settings: Settings,
        machine_klipper: FakeKlipper,
        audio_player: FakeAudioPlayer,
        mount_camera: Callable[[Camera], None],
        plate_shapes: Callable[[float, float], list[Polygon]],
        scenario: Scenario,
    ):
        """塗布前撮影 → ローディング → 点塗布 → 塗布後撮影 → 計量 → 校正の生成."""
        mount_camera(
            scene_camera(
                machine_klipper, scenario.rotation, shapes=plate_shapes(20.0, 20.0)
            )
        )

        record = manager.start(
            "paste_volume_calibration",
            {
                "plate_width": 20.0,
                "plate_height": 20.0,
                "volume_divisions": 3,
                "samples_per_volume": 2,
                "blank_count": 1,
                "shuffle_seed": 7,
                "paste_id": "golden",
                "save_name": SAVE_NAME,
            },
        )
        answered: set[str] = set()
        for answer in (True, True):
            answer_next_prompt(record, manager, answer, answered)
        wait_operator(record, audio_player, LOADING_STAGE, calls=3)
        manager.submit_command({"type": "extrude", "amount": 0.1})
        manager.submit_command({"type": "finish"})
        answer_next_prompt(record, manager, 2.4, answered, timeout=120.0)
        finish(job_state, record)

        assert (job_settings.paste_volume_calibration_dir / SAVE_NAME).is_file()
        assert_transcript(machine_klipper.sent, JOBS / f"{scenario.name}.gcode")


class TestFlowCalibration:
    @pytest.mark.parametrize(
        "scenario",
        scenarios_of("paste_solder", "flow_calibration_only"),
        ids=lambda s: s.name,
    )
    def test_standalone(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        job_settings: Settings,
        job_state: AppState,
        machine_klipper: FakeKlipper,
        mount_camera: Callable[[Camera], None],
        led_blinker_shapes: list[Polygon],
        scenario: Scenario,
    ):
        """paste_solder の運転時補正だけを合成ジョブで走らせ、rotations_per_ul の補正値を出す."""
        enable_flow_calibration(job_settings, job_state, LED_BLINKER)
        mount_camera(
            scene_camera(machine_klipper, scenario.rotation, shapes=led_blinker_shapes)
        )
        outcomes: list[FlowCalibrationOutcome | None] = []

        def run(ctx: JobContext) -> None:
            pcb = PcbFile(LED_BLINKER)
            plan, error = plan_flow_calibration(
                config=ctx.machine.paste_dispenser.volume_correction,
                points=FLOW_POINTS,
                outline=pcb.outline.polygon,
            )
            assert plan is not None, error
            with ctx.open_camera() as camera:
                session = PasteSession.from_calibration(
                    BoardCalibrationResult(
                        machine=ctx.machine,
                        klipper=machine_klipper,
                        stage=XYZStage(machine_klipper.readonly),
                        camera=camera,
                        calibration=CALIBRATION,
                        offset_transform=Identity(),
                        board_transform=BOARD_PLACEMENT,
                        pcb=pcb,
                    )
                )
                with session.make_applicator() as applicator:
                    outcomes.append(
                        run_flow_calibration(
                            ctx, session, full_board_correction(), applicator, plan
                        )
                    )

        register_synthetic(catalog, run, name="flow_calibration")
        finish(job_state, manager.start("flow_calibration", {}))

        (outcome,) = outcomes
        assert outcome is not None
        assert_document(
            attrs.asdict(outcome), ARTIFACTS / "flow_calibration_outcome.json"
        )
        assert_transcript(machine_klipper.sent, JOBS / f"{scenario.name}.gcode")


class TestPasteVolumeRefit:
    def test_sends_nothing_and_writes_calibration(
        self,
        manager: JobManager,
        job_state: AppState,
        job_settings: Settings,
        machine_klipper: FakeKlipper,
    ):
        """実画像の session を選んで計測・フィットし、校正ファイルを書く（装置は動かさない）."""
        session = build_paste_volume_session(
            job_settings.paste_dataset_dir / "plate-20x20-20260908T143052.123+0000"
        )

        record = manager.start("paste_volume_refit", {"save_name": SAVE_NAME})
        answer_next_prompt(record, manager, session.name, set())
        finish(job_state, record)

        assert machine_klipper.sent == ()
        path = job_settings.paste_volume_calibration_dir / SAVE_NAME
        assert_document(
            _mask_created_at(json.loads(path.read_text(encoding="utf-8"))),
            ARTIFACTS / "paste_volume_refit_calibration.json",
        )


class TestPasteDatasetFinalize:
    STEM = "plate-20x20-20260908T143052.123+0000"

    def test_sends_nothing_and_writes_metadata(
        self,
        manager: JobManager,
        job_state: AppState,
        job_settings: Settings,
        machine_klipper: FakeKlipper,
    ):
        """未完了 session の pending.json と計量質量から metadata.json を書いて確定する."""
        document = json.loads(PENDING_PIN.read_text(encoding="utf-8"))
        incomplete = job_settings.paste_dataset_dir / f"{self.STEM}.incomplete"
        for target in (*document["samples"], *document["blanks"]):
            for view in target["views"]:
                for phase in ("pre", "post"):
                    (incomplete / view[phase]).parent.mkdir(parents=True, exist_ok=True)
                    (incomplete / view[phase]).write_bytes(b"")
        (incomplete / "pending.json").write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )

        record = manager.start("paste_dataset_finalize", {"measured_mass": 0.945})
        answer_next_prompt(record, manager, incomplete.name, set())
        finish(job_state, record)

        assert machine_klipper.sent == ()
        metadata = job_settings.paste_dataset_dir / self.STEM / "metadata.json"
        assert_document(
            json.loads(metadata.read_text(encoding="utf-8")),
            ARTIFACTS / "paste_dataset_finalize_metadata.json",
        )
