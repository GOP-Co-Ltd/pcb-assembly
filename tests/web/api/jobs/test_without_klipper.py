"""装置ジョブの接続拒否時の終了・解放の契約.

成功系のステージ移動・照合・押出は golden と pcbasm のテスト、実機での通しは docs/acceptance.md
の受け入れ試験で確かめる。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.polling import wait_until
from web.api.backend import Backend
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobStatus


@pytest.mark.usefixtures("unreachable_klipper")
class TestMachineJobsWithoutKlipper:
    """装置ジョブの graceful FAILED（テスト用 config: port 7126 = 接続拒否）.

    実動作（押出・塗布・SUCCEEDED 到達）は受け入れ試験（A6、A8）で確かめる。
    """

    @pytest.mark.parametrize(
        ("name", "needs_pcb"),
        [
            ("paste_solder", True),
            ("toolhead_offset", True),
            ("loading", False),
            ("board_tour", True),
            ("orthogonality_test", True),
            # ホーミング（G28）で先に落ちる。PCB 選択は不要
            ("reference_point_setup", False),
        ],
    )
    def test_job_fails_gracefully_and_releases_lock(
        self,
        manager: JobManager,
        camera_backend: Backend,
        real_pcb_path: Path,
        name: str,
        needs_pcb: bool,
    ):
        if needs_pcb:
            camera_backend.selection.select(real_pcb_path)
        record = manager.start(name, {})
        wait_until(lambda: record.status.terminal, timeout=60.0)
        wait_until(lambda: camera_backend.lock.owner is None)

        assert record.status == JobStatus.FAILED
        assert record.error  # 接続エラーが error に載る
        assert "M84" in "\n".join(record.log_lines)  # relax 失敗警告
        with camera_backend.lock.hold("after-failed-job"):  # ロックは解放済み
            pass

    def test_loading_starts_with_homing_before_enabling_dispenser(
        self,
        manager: JobManager,
        camera_backend: Backend,
    ):
        record = manager.start("loading", {"position_x": 10.0})
        wait_until(lambda: record.status.terminal, timeout=60.0)
        wait_until(lambda: camera_backend.lock.owner is None)

        assert record.status == JobStatus.FAILED
        assert record.progress_stage == "ホーミング", record.error
        assert "全軸ホーミングを実行します" in "\n".join(record.log_lines)

    def test_dispense_calibration_fails_gracefully_and_releases_lock(
        self,
        manager: JobManager,
        camera_backend: Backend,
    ):
        """共通土台でその場 PCB 生成 → カメラ確保 → ボード計測で Klipper 不通 → FAILED.

        PCB 選択不要（``requires_pcb=False``）。実 pcbnew での銅板生成と FakeCamera
        確保を越えてから接続拒否で落ちるため、長めに待つ。
        """
        record = manager.start("dispense_calibration", {})
        # 実 pcbnew 読込・FakeCamera 起動・接続リトライを含むため長め
        wait_until(lambda: record.status.terminal, timeout=180.0)
        wait_until(lambda: camera_backend.lock.owner is None)

        assert record.status == JobStatus.FAILED
        assert record.error  # 接続エラーが error に載る
        assert "M84" in "\n".join(record.log_lines)  # relax 失敗警告
        with camera_backend.lock.hold("after-failed-job"):  # ロックは解放済み
            pass

    @pytest.mark.parametrize(
        "name",
        [
            "paste_solder",
            "height_plane",
            "toolhead_offset",
            "board_tour",
            "orthogonality_test",
        ],
    )
    def test_pcb_job_without_selection_raises_value_error(
        self, manager: JobManager, name: str
    ):
        """requires_pcb=True: PCB 未選択は開始前に ValueError（→ 400）."""
        with pytest.raises(ValueError):
            manager.start(name, {})
