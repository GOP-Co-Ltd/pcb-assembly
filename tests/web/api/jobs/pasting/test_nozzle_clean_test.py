"""ノズルクリーニングのテストジョブ（nozzle_clean_test）が、クリーニングせずに終わる経路.

クリーニングする経路の送信列は golden（tests/golden/test_paste_jobs.py）が固定する。
試験用の設定はキャップ位置が未記録なので、どの経路でも終了時の駐機は M84 だけになる。
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, override

import pytest

from pcbasm.gcode import GCodeLike
from pcbasm.hal.fake import FakeKlipper
from tests.support.polling import wait_until
from web.api.backend import Backend
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobRecord, JobStatus

# 終了時の駐機（キャップ未記録で PRESENT マクロも無い）
END_PARKING = ["M84"]


class AbortOnHoming(FakeKlipper):
    """ホーミングを送った直後に、ジョブの中止を要求する fake 機体."""

    def __init__(self, abort: Callable[[], object]) -> None:
        super().__init__()
        self._abort = abort

    @override
    def send_gcode(
        self, gcode: GCodeLike, *, timeout: float | None = None
    ) -> dict[str, Any]:
        result = super().send_gcode(gcode, timeout=timeout)
        if str(gcode).startswith("G28"):
            self._abort()
        return result


def _record_clean_position(config_dir: Path, x: float) -> None:
    with (config_dir / "machine.toml").open("a", encoding="utf-8") as machine_toml:
        machine_toml.write(f"\n[solder.nozzle.clean]\nx = {x}\ny = 20.0\nz = -3.0\n")


class TestNozzleCleanTest:
    @pytest.fixture
    def run(
        self, manager: JobManager, camera_backend: Backend
    ) -> Callable[[], JobRecord]:
        """ジョブを始め、終了時の駐機が済む（装置のロックが外れる）まで待つ関数を返す."""

        def run() -> JobRecord:
            record = manager.start("nozzle_clean_test", {})
            wait_until(
                lambda: record.status.terminal and camera_backend.lock.owner is None,
                timeout=30.0,
            )
            return record

        return run

    @pytest.fixture
    def klipper(self, mount_klipper: Callable[[FakeKlipper], None]) -> FakeKlipper:
        klipper = FakeKlipper()
        klipper.set_status("toolhead", "homed_axes", "xyz")
        mount_klipper(klipper)
        return klipper

    def test_unrecorded_position_fails_without_moving(
        self, klipper: FakeKlipper, run: Callable[[], JobRecord]
    ):
        record = run()

        assert record.status == JobStatus.FAILED
        assert record.error == "ノズルクリーニング位置が未記録です"
        assert klipper.sent_lines == END_PARKING

    @pytest.mark.usefixtures("partial_nozzle_clean")
    def test_position_less_table_is_unrecorded(
        self, klipper: FakeKlipper, run: Callable[[], JobRecord]
    ):
        """設定ページで動作値だけを保存した座標の無い節も、未記録として断る."""
        record = run()

        assert record.status == JobStatus.FAILED
        assert record.error == "ノズルクリーニング位置が未記録です"
        assert klipper.sent_lines == END_PARKING

    def test_out_of_reach_fails_before_homing_or_purging(
        self,
        config_dir: Path,
        mount_klipper: Callable[[FakeKlipper], None],
        run: Callable[[], JobRecord],
    ):
        """可動域はクリーニングの G-code を組む段で確かめるので、未ホーミングの機体でもホーミングすら送らない."""
        klipper = FakeKlipper()
        mount_klipper(klipper)
        _record_clean_position(config_dir, 299.0)

        record = run()

        assert record.status == JobStatus.FAILED
        assert "可動域外" in (record.error or "")
        assert klipper.sent_lines == END_PARKING

    def test_abort_during_homing_skips_cleaning(
        self,
        config_dir: Path,
        manager: JobManager,
        mount_klipper: Callable[[FakeKlipper], None],
        run: Callable[[], JobRecord],
    ):
        """中止はクリーニングの前で効き、パージせずに駐機して終わる."""
        klipper = AbortOnHoming(manager.request_abort)
        mount_klipper(klipper)
        _record_clean_position(config_dir, 10.0)

        record = run()

        assert record.status == JobStatus.ABORTED
        assert klipper.sent_lines == ["G28 X Y Z", "M400", *END_PARKING]
