"""装置ジョブの接続拒否時の終了・解放と、実機通しの契約。

実機テストは @mark_hardware で分離し、ユーザーが手元で実行する。
"""

from __future__ import annotations

import time
from pathlib import Path

import attrs
import cv2
import pytest

from pcbasm.gcode import GCode
from pcbasm.hal import XYZStage
from pcbasm.pasting.applicator import build_applicator
from pcbasm.pasting.nozzle_clean import TRAVEL_Z, clean_nozzle
from tests.helpers import mark_hardware
from tests.web.api.jobs.conftest import WaitUntil, answer_next_prompt
from web.api.jobs.machine_commands import create_command_klipper
from web.api.jobs.manager import JobManager, JobRecord, JobStatus
from web.api.jobs.pasting import LOADING_STAGE
from web.api.settings import Settings
from web.api.state import AppState


class TestMachineJobsWithoutKlipper:
    """装置ジョブの graceful FAILED（テスト用 config: port 7126 = 接続拒否）.

    実動作（押出・塗布・SUCCEEDED 到達）は実機区分でカバーする分担（計画書 §4）。
    """

    @pytest.mark.parametrize(
        ("name", "needs_pcb"),
        [
            ("paste_solder", True),
            ("toolhead_offset", True),
            ("loading", False),
        ],
    )
    def test_job_fails_gracefully_and_releases_lock(
        self,
        manager: JobManager,
        state: AppState,
        real_pcb_path: Path,
        wait_until: WaitUntil,
        name: str,
        needs_pcb: bool,
    ):
        if needs_pcb:
            state.select_pcb(real_pcb_path)
        record = manager.start(name, {})
        wait_until(lambda: record.status.terminal, timeout=60.0)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.FAILED
        assert record.error  # 接続エラーが error に載る
        assert "M84" in "\n".join(record.log_lines)  # relax 失敗警告
        with state.machine_lock("after-failed-job"):  # ロックは解放済み
            pass

    def test_loading_starts_with_homing_before_enabling_dispenser(
        self,
        manager: JobManager,
        state: AppState,
        wait_until: WaitUntil,
    ):
        record = manager.start("loading", {"position_x": 10.0})
        wait_until(lambda: record.status.terminal, timeout=60.0)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.FAILED
        assert record.progress_stage == "ホーミング", record.error
        assert "全軸ホーミングを実行します" in "\n".join(record.log_lines)

    def test_dispense_calibration_fails_gracefully_and_releases_lock(
        self,
        manager: JobManager,
        state: AppState,
        wait_until: WaitUntil,
    ):
        """共通土台でその場 PCB 生成 → カメラ確保 → ボード計測で Klipper 不通 → FAILED.

        PCB 選択不要（``requires_pcb=False``）。実 pcbnew での銅板生成と FakeCamera
        確保を越えてから接続拒否で落ちるため、長めに待つ。
        """
        record = manager.start("dispense_calibration", {})
        # 実 pcbnew 読込・FakeCamera 起動・接続リトライを含むため長め
        wait_until(lambda: record.status.terminal, timeout=180.0)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.FAILED
        assert record.error  # 接続エラーが error に載る
        assert "M84" in "\n".join(record.log_lines)  # relax 失敗警告
        with state.machine_lock("after-failed-job"):  # ロックは解放済み
            pass

    @pytest.mark.parametrize(
        "name", ["paste_solder", "height_plane", "toolhead_offset"]
    )
    def test_pcb_job_without_selection_raises_value_error(
        self, manager: JobManager, name: str
    ):
        """requires_pcb=True: PCB 未選択は開始前に ValueError（→ 400）."""
        with pytest.raises(ValueError):
            manager.start(name, {})


def _wait_loading_stage_and_settle(
    record: JobRecord, wait_until: WaitUntil, *, timeout: float = 120.0
) -> None:
    """ローディング段階入りを待ち、入口の滞留コマンド drain を確実に越える.

    計画書「_run_loading_loop」節: progress(LOADING_STAGE) の直後に滞留コマンドを drain
    するため、stage 表示直後の submit は破棄され得る。実機テストでは短い settle を挟んでから submit
    する（アサートには使わない）。
    """
    wait_until(lambda: record.progress_stage == LOADING_STAGE, timeout=timeout)
    time.sleep(1.0)


@mark_hardware
class TestPastingHardware:
    """実機通し（実 Moonraker、実機 config/）。ユーザー実行.

    前提（計画書 §5「ユーザーへ引き継ぐ実機確認項目」1・2・4・5）:

    - Moonraker が localhost:7125 で稼働し、各軸がホーミング可能であること
    - ペーストディスペンサーが装着・ペースト充填可能であること
    - height_plane: data/testing/led_blinker の基板がステージにセットされ、
      実カメラがキャリブレーション済みであること
    - paste_solder の probe → XY 照合までは自動検証し、塗布開始前に中止する
    - paste_solder / toolhead_offset の完全な通しはプレビュー目視を伴うため
      WebUI 手動 E2E（計画書 §5 引き継ぎ 3・6）で確認する
    """

    def test_loading_extrude_then_finish_succeeds(
        self, real_manager: JobManager, wait_until: WaitUntil
    ):
        """押出 → 終了で SUCCEEDED、summary に押出合計 [uL] が載る."""
        record = real_manager.start("loading", {"amount": 0.1})
        _wait_loading_stage_and_settle(record, wait_until)

        real_manager.submit_command({"type": "extrude", "amount": 0.05})
        real_manager.submit_command({"type": "finish"})
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "押出合計" in result.summary
        assert "uL" in result.summary
        assert result.apply is None  # loading に Apply はない

    def test_loading_homes_and_moves_to_specified_x_position(
        self,
        real_manager: JobManager,
        real_state: AppState,
        wait_until: WaitUntil,
    ):
        """ローディング開始で全軸 homing 後、指定した X へ移動する."""
        klipper = create_command_klipper(real_state.machine())
        stage = XYZStage(klipper.readonly)
        target_x = min(stage.limits.x.min + 1.0, stage.limits.x.max)

        record = real_manager.start("loading", {"position_x": target_x})
        _wait_loading_stage_and_settle(record, wait_until)
        position = stage.get_position()
        real_manager.submit_command({"type": "finish"})
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED
        assert position.x == pytest.approx(target_x)

    def test_nozzle_clean_motion_returns_to_travel_z(self, real_state: AppState):
        """ペーストを出さずにクリーニングのモーションと可動域だけを確認する.

        こすり効果（先端にペーストが残らないか）の確認は WebUI の手動 E2E に委ねる。
        """
        machine = real_state.machine()
        clean = machine.nozzle_clean
        if clean is None:
            pytest.skip("[paste_dispenser.nozzle_clean] が未記録のため実行しない")
        klipper = create_command_klipper(machine)
        stage = XYZStage(klipper.readonly)
        klipper.send_gcode(GCode.homing() + GCode.wait_for_done())

        with build_applicator(klipper, stage, machine.paste_dispenser) as applicator:
            clean_nozzle(klipper, stage, applicator, attrs.evolve(clean, purge_ul=0.0))

        assert stage.get_position().z == pytest.approx(TRAVEL_Z, abs=0.01)

    def test_loading_invalid_value_logs_reason_and_continues(
        self, real_manager: JobManager, wait_until: WaitUntil
    ):
        """不正値コマンドは押し出さず理由をログし、ループは継続する.

        検証（値不正 → InvalidLoadingCommand → ctx.log(reason)）はサーバに 一本化した（JS
        の正値チェックは削除済み）。ループ到達に実 Moonraker 接続（AirPump ON）が必要なため e2e
        ではなく実機区分でピンする。
        """
        record = real_manager.start("loading", {"amount": 0.1})
        _wait_loading_stage_and_settle(record, wait_until)

        real_manager.submit_command({"type": "extrude", "amount": -1.0})
        wait_until(
            lambda: any(
                "extrude の量には正の数値が必要です" in line
                for line in record.log_lines
            ),
            timeout=60.0,
        )
        real_manager.submit_command({"type": "finish"})
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "押出合計 +0.000 uL" in result.summary  # 不正値は押し出していない

    def test_loading_accepts_machine_commands_in_loop(
        self, real_manager: JobManager, wait_until: WaitUntil
    ):
        """ローディング中もマシン操作（jog）を受け付ける（machine_commands 共有）."""
        record = real_manager.start("loading", {"amount": 0.1})
        _wait_loading_stage_and_settle(record, wait_until)

        real_manager.submit_command({"type": "jog", "axis": "z", "dist": 0.1})
        real_manager.submit_command({"type": "finish"})
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED

    def test_loading_rotation_extrude_then_finish_succeeds(
        self, real_manager: JobManager, wait_until: WaitUntil
    ):
        """回転押出 → 終了で SUCCEEDED、summary に回転合計 [rev] が載る."""
        record = real_manager.start(
            "loading", {"rotations": 0.1, "rate": 0.5, "accel": 0.5}
        )
        _wait_loading_stage_and_settle(record, wait_until)

        real_manager.submit_command(
            {
                "type": "extrude_rotations",
                "rotations": 0.1,
                "rate": 0.5,
                "accel": 0.5,
            }
        )
        real_manager.submit_command({"type": "finish"})
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "回転合計" in result.summary
        assert "rev" in result.summary

    def test_loading_rotation_extrude_with_retract_nets_difference(
        self, real_manager: JobManager, wait_until: WaitUntil
    ):
        """回転押出に引き戻しが付随し、回転合計が押出−引き戻しの純増になる."""
        record = real_manager.start(
            "loading", {"rotations": 0.1, "rate": 0.5, "accel": 0.5}
        )
        _wait_loading_stage_and_settle(record, wait_until)

        real_manager.submit_command(
            {
                "type": "extrude_rotations",
                "rotations": 0.1,
                "rate": 0.5,
                "accel": 0.5,
                "retract_rotations": 0.05,
            }
        )
        real_manager.submit_command({"type": "finish"})
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        # 0.1 押出 − 0.05 引き戻し = 0.05 rev の純増
        assert "回転合計 +0.050 rev" in result.summary

    # 吐出量キャリブレーション統合ジョブ（dispense_calibration）の ①②③ 実測は
    # 実 Moonraker + 実カメラ + 実ペースト + 物理銅板の装着・計量を要する。手順が
    # 対話的（ボード計測 → 線引き → 質量/番号入力）でブラウザ目視を伴うため、
    # WebUI 手動 E2E（make api-fake）でユーザーが検証する分担（large-refactor-workflow）。

    def test_paste_solder_probes_then_aligns_at_camera_focus_z(
        self,
        real_manager: JobManager,
        real_state: AppState,
        wait_until: WaitUntil,
    ):
        """Probe 後に camera focus Z で領域照合し、塗布前に中止する."""
        focus_z = real_state.focus_z()
        assert focus_z is not None
        stage = XYZStage(create_command_klipper(real_state.machine()).readonly)
        real_state.select_pcb(Path("data/testing/led_blinker/led_blinker.kicad_pcb"))
        record = real_manager.start("paste_solder", {})
        calibration_stages = {"高さ計測", "銅箔照合", "pad照合"}
        try:
            wait_until(
                lambda: record.progress_stage in calibration_stages,
                timeout=300.0,
            )
            assert record.progress_stage == "高さ計測"
            wait_until(lambda: record.progress_stage == "銅箔照合", timeout=900.0)
            wait_until(
                lambda: abs(stage.get_position().z - focus_z) <= 0.01,
                timeout=60.0,
            )
        finally:
            real_manager.request_abort()
            wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.ABORTED

    def test_height_plane_full_run_yields_heatmap_artifacts(
        self,
        real_manager: JobManager,
        real_state: AppState,
        real_settings: Settings,
        wait_until: WaitUntil,
    ):
        """Confirm True → 実計測 → planned_points + height_plane の PNG
        artifacts."""
        real_state.select_pcb(Path("data/testing/led_blinker/led_blinker.kicad_pcb"))
        record = real_manager.start("height_plane", {})
        answered: set[str] = set()
        answer_next_prompt(record, real_manager, True, answered)
        wait_until(lambda: record.status.terminal, timeout=900.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "点計測" in result.summary
        labels = {artifact.label for artifact in result.artifacts}
        assert labels == {"計測予定点", "ヒートマップ"}
        artifacts_root = real_settings.webui_data_dir
        for artifact in result.artifacts:
            assert artifact.kind == "image"
            image = cv2.imread(str(artifacts_root / artifact.path))
            assert image is not None
            assert image.size > 0
        assert result.apply is None  # height_plane に Apply はない
