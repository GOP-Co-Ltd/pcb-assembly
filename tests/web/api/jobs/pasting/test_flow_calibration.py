"""運転時流量キャリブレーション（`web.api.jobs.pasting.flow_calibration`）の契約.

最優先は **補正できなくても塗布ジョブを落とさない**こと。この時点で位置合わせと
高さ計測が終わっており、後から足した補正の失敗で塗布を取りやめるのは割に合わない。

はんだ塗布ジョブ本体は装置を要求するので、合成ジョブへ同じ関数を通して確かめる
（`TestPasteDatasetVolumeVerification` と同じ形）。装置側は自前 HAL の fake。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
import shapely

from pcbasm.config import FlowCalibration, Machine, NozzleClean
from pcbasm.geometry import Identity, Point2d, Shift
from pcbasm.hal import XYZStage
from pcbasm.pasting.alignment import PasteCorrection
from pcbasm.pasting.initial_purge import ResolvedInitialPurge, purge_point_label
from pcbasm.pasting.paste_volume.calibration import (
    CALIBRATION_SUFFIX,
    parse_calibration,
    write_calibration,
)
from pcbasm.pasting.paste_volume.runtime import (
    FlowCalibrationOutcome,
    plan_flow_calibration,
)
from pcbasm.pasting.session import PasteSession
from pcbasm.pcb import PcbFile
from pcbasm.posctrl import (
    AlignmentRegion,
    BoardAlignment,
    BoardCalibrationResult,
    EdgeMatch,
    RegionAlignment,
)
from pcbasm.vision import Image, Offset
from pcbasm.vision.calibration import CalibrationResult
from tests.helpers import (
    TESTING_CONFIG_DIR,
    TESTING_DATA_DIR,
    FakeCamera,
    FakeKlipper,
)
from tests.web.api.jobs.conftest import (
    ManagerFactory,
    WaitUntil,
    register_synthetic,
)
from web.api.jobs.catalog import JobCatalog, ParamSpec
from web.api.jobs.context import JobContext, JobResult
from web.api.jobs.manager import JobManager, JobRecord, JobStatus
from web.api.jobs.pasting.flow_calibration import (
    run_flow_calibration,
    run_flow_calibration_with_cleaning,
    summary_line,
)
from web.api.settings import Settings

LED_BLINKER = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"
CALIBRATION_PIN = TESTING_DATA_DIR / "schemas" / "paste_volume_calibration_v1.json"
PPM = 10.0
RESOLUTION = (640, 480)
FOCUS_Z = 12.0
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


def _frame() -> Image:
    """一様な銅板色のフレーム（pre と post が同じ = はんだが写らない）."""
    width, height = RESOLUTION
    return Image(np.full((height, width, 3), 180, dtype=np.uint8))


def _session(camera: FakeCamera, klipper: FakeKlipper) -> PasteSession:
    result = BoardCalibrationResult(
        machine=Machine(TESTING_CONFIG_DIR / "machine.toml"),
        klipper=klipper,
        stage=XYZStage(klipper.readonly),
        camera=camera,
        calibration=CalibrationResult(
            pixel_per_mm=PPM,
            square_size_mm=1.0,
            mean_distance_px=PPM,
            std_distance_px=0.0,
            resolution=RESOLUTION,
            crop_size=(400, 400),
            calibrated_at=datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC),
            z_position=FOCUS_Z,
        ),
        offset_transform=Identity(),
        board_transform=Shift(100.0, 50.0),
        pcb=PcbFile(LED_BLINKER),
    )
    return PasteSession.from_calibration(result)


def _correction() -> PasteCorrection:
    """基板全面を覆う成功領域 1 つ（変位なし）の補正."""
    area = shapely.box(-100.0, -100.0, 100.0, 100.0)
    return PasteCorrection(
        alignment=BoardAlignment(
            results=(
                RegionAlignment(
                    region=AlignmentRegion(
                        index=0,
                        board_center=Point2d(0.0, 0.0),
                        anchor=Point2d(0.0, 0.0),
                        roi=(0, 0, 100, 100),
                        board_area=area,
                    ),
                    match=EdgeMatch(
                        offset=Offset(px=Point2d(0.0, 0.0), pixel_per_mm=PPM),
                        rms_distance_px=0.0,
                    ),
                    displacement=Point2d(0.0, 0.0),
                    increment=Point2d(0.0, 0.0),
                    passes=1,
                ),
            )
        ),
        height_plane=Identity(),
    )


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
            session = _session(FakeCamera([_frame()]), klipper)
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
                run_result = run_flow_calibration(
                    ctx, session, _correction(), applicator, plan
                )
                elapsed.append(time.monotonic() - started)
                moves.extend(move.get("z") for move in klipper.g1_moves())
            outcome = run_result.outcome
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


# クリーニング位置（マシン座標）。fake の可動域 x/y 0-300・z -5..50 の内側
CLEAN = NozzleClean(x=50.0, y=60.0, z=1.0)
# 再パージの塗布点（board 座標）。board_transform=Shift(100, 50) でマシン (110, 62)。
# 測定点（マシン Y=56）ともクリーニング位置（マシン Y=60）とも Y で区別できる
PURGE_POINT = Point2d(10.0, 12.0)
PURGE = ResolvedInitialPurge(
    amount_ul=0.3,
    point=PURGE_POINT,
    label=purge_point_label(PURGE_POINT),
    source="explicit",
)


# 位置決めを伴わない押し出し（prime / クリーニングのパージ / リトラクト）の抽出用。
# fake の rotation_distance は 1.0 なので、距離 [mm] = 回転数になる
_STEPPER_MOVE_RE = re.compile(r"MANUAL_STEPPER STEPPER=paste_dispenser MOVE=(\S+)(.*)")


def _blocking_loads_ul(klipper: FakeKlipper) -> list[float]:
    """``load`` / ``retract`` / ``prime`` が送った押し出し量 [uL] を順に返す.

    塗布シーケンス（``SYNC=0`` の非同期 MOVE）は除く。
    """
    dispenser = Machine(TESTING_CONFIG_DIR / "machine.toml").paste_dispenser
    amounts: list[float] = []
    for line in klipper.sent_lines:
        match = _STEPPER_MOVE_RE.match(line)
        if match and "SYNC=0" not in match.group(2):
            amounts.append(float(match.group(1)) / dispenser.rotations_per_ul)
    return amounts


def _retract_amount_ul() -> float:
    return Machine(TESTING_CONFIG_DIR / "machine.toml").paste_dispenser.retract_amount


def _dispense_y(point: Point2d) -> float:
    """塗布時のマシン Y（ツールヘッドオフセットを含む）."""
    session = _session(FakeCamera([_frame()]), FakeKlipper())
    return session.point_transform(point, _correction()).apply(point).y


def _dot_frame(diameter_px: int) -> Image:
    """中心に暗い円を置いたフレーム（塗布後の 1 ドットを模す）."""
    width, height = RESOLUTION
    array = np.full((height, width, 3), 180, dtype=np.uint8)
    rows, columns = np.ogrid[:height, :width]
    radius = diameter_px / 2.0
    disk = (rows - height / 2 + 0.5) ** 2 + (
        columns - width / 2 + 0.5
    ) ** 2 <= radius**2
    array[disk] = 40
    return Image(array)


class TestRunFlowCalibrationWithCleaning:
    """未検出のときだけノズルをクリーニングし、パージしてやり直す."""

    @pytest.fixture
    def moves(self) -> list[dict[str, float]]:
        """合成ジョブが記録した G1（クリーニングとパージの経路を見る）."""
        return []

    @pytest.fixture
    def frames(self) -> list[Image]:
        """FakeCamera へ順に返させるフレーム（テストごとに詰める）."""
        return []

    @pytest.fixture
    def loads(self) -> list[float]:
        """合成ジョブが記録した単独の押し出し量 [uL]（prime / パージ / リトラクト）."""
        return []

    @pytest.fixture
    def cleaning_manager(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        moves: list[dict[str, float]],
        frames: list[Image],
        loads: list[float],
    ) -> JobManager:
        """`run_flow_calibration_with_cleaning` だけを呼ぶ合成ジョブを積んだ manager."""

        def run(ctx: JobContext) -> JobResult:
            klipper = FakeKlipper()
            session = _session(FakeCamera(frames), klipper)
            config = FlowCalibration(
                calibration_file=str(ctx.params["calibration_file"]),
                settle_seconds=0.0,
            )
            plan, error = plan_flow_calibration(
                config=config, points=POINTS, outline=session.pcb.outline.polygon
            )
            assert error is None, error
            assert plan is not None
            with session.make_applicator() as applicator:
                klipper.clear_sent()
                try:
                    outcome = run_flow_calibration_with_cleaning(
                        ctx,
                        session,
                        _correction(),
                        applicator,
                        plan,
                        nozzle_clean=CLEAN if ctx.params["with_clean"] else None,
                        purge=PURGE,
                    )
                finally:
                    moves.extend(klipper.g1_moves())
                    loads.extend(_blocking_loads_ul(klipper))
            return JobResult(
                summary="補正なし" if outcome is None else summary_line(outcome)
            )

        register_synthetic(
            catalog,
            run,
            name="flow_calibration_cleaning",
            params=(
                ParamSpec("calibration_file", "校正", "str", default=""),
                ParamSpec("with_clean", "クリーニング位置", "bool", default=True),
            ),
        )
        return make_manager(catalog)

    @staticmethod
    def _run(
        manager: JobManager,
        wait_until: WaitUntil,
        name: str,
        *,
        with_clean: bool = True,
    ) -> JobRecord:
        record = manager.start(
            "flow_calibration_cleaning",
            {"calibration_file": name, "with_clean": with_clean},
        )
        wait_until(lambda: record.status.terminal, timeout=60.0)
        return record

    @staticmethod
    def _is_clean_approach(move: dict[str, float]) -> bool:
        """クリーニング位置への XY 移動（approach の 1 手）か."""
        return (
            move.get("x") == pytest.approx(CLEAN.x)
            and move.get("y") == pytest.approx(CLEAN.y)
            and "z" not in move
        )

    @staticmethod
    def _is_purge(move: dict[str, float]) -> bool:
        """再パージ点への移動か."""
        return move.get("y") == pytest.approx(_dispense_y(PURGE_POINT))

    @staticmethod
    def _first_index(
        moves: list[dict[str, float]], matches: Callable[[dict[str, float]], bool]
    ) -> int:
        for index, move in enumerate(moves):
            if matches(move):
                return index
        raise AssertionError("該当する移動がありません")

    @classmethod
    def _approaches_to_clean(cls, moves: list[dict[str, float]]) -> int:
        """クリーニング位置への XY 移動（approach の 1 手）の回数."""
        return sum(1 for move in moves if cls._is_clean_approach(move))

    @classmethod
    def _purges(cls, moves: list[dict[str, float]]) -> int:
        """再パージ点で塗った回数（連続する移動のかたまりを 1 回と数える）."""
        at_purge = [cls._is_purge(move) for move in moves]
        return sum(
            1
            for index, here in enumerate(at_purge)
            if here and not (index > 0 and at_purge[index - 1])
        )

    @staticmethod
    def _write_calibration(settings: Settings) -> str:
        calibration, error = parse_calibration(
            json.loads(CALIBRATION_PIN.read_text(encoding="utf-8"))
        )
        assert error is None, error
        assert calibration is not None
        name = f"runtime{CALIBRATION_SUFFIX}"
        write_calibration(settings.paste_volume_calibration_dir / name, calibration)
        return name

    def test_does_not_clean_when_the_first_pass_detects_a_deposit(
        self,
        cleaning_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        frames: list[Image],
        moves: list[dict[str, float]],
    ):
        """吐出できているならクリーニングは要らない."""
        name = self._write_calibration(fake_camera_settings)
        frames.extend([_frame()] * 3 + [_dot_frame(10)] * 3)

        record = self._run(cleaning_manager, wait_until, name)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert self._approaches_to_clean(moves) == 0
        assert self._purges(moves) == 0

    def test_cleans_and_purges_once_then_retries_when_nothing_is_detected(
        self,
        cleaning_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        frames: list[Image],
        moves: list[dict[str, float]],
        loads: list[float],
    ):
        """1 回目が未検出 → クリーニング → パージ → もう 1 回だけ測る."""
        name = self._write_calibration(fake_camera_settings)
        frames.extend([_frame()] * 9 + [_dot_frame(10)] * 3)

        record = self._run(cleaning_manager, wait_until, name)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert self._approaches_to_clean(moves) == 1
        assert self._purges(moves) == 1
        assert record.result is not None
        summary = record.result.summary
        assert summary is not None
        assert "rotations_per_ul" in summary
        # 直前の塗布はリトラクトで終わっている。押し戻してからパージしないと
        # 設定した量が先端から出ない（出たあとはまた引き戻す）
        retract = _retract_amount_ul()
        assert loads == pytest.approx([retract, CLEAN.purge_ul, -retract])
        # 掃除 → パージ の順で、どちらも 1 回目の測定（3 点 pre + 3 点 post）の後
        clean_index = self._first_index(moves, self._is_clean_approach)
        purge_index = self._first_index(moves, self._is_purge)
        captures_before_clean = sum(
            1 for move in moves[:clean_index] if move.get("z") == pytest.approx(FOCUS_Z)
        )
        assert captures_before_clean == 2 * len(POINTS)
        assert clean_index < purge_index

    def test_fails_the_job_when_the_retry_still_detects_nothing(
        self,
        cleaning_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        frames: list[Image],
        moves: list[dict[str, float]],
    ):
        """クリーニングとパージでも吐出が戻らないなら塗布へ進まない."""
        name = self._write_calibration(fake_camera_settings)
        frames.append(_frame())

        record = self._run(cleaning_manager, wait_until, name)

        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert "詰まり" in record.error
        # やり直しは 1 回だけ（無限に掃除し続けない）
        assert self._approaches_to_clean(moves) == 1

    def test_retries_without_cleaning_when_the_position_is_not_recorded(
        self,
        cleaning_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        frames: list[Image],
        moves: list[dict[str, float]],
        loads: list[float],
    ):
        """位置未記録ならこすれないが、パージのやり直しには意味がある."""
        name = self._write_calibration(fake_camera_settings)
        frames.extend([_frame()] * 9 + [_dot_frame(10)] * 3)

        record = self._run(cleaning_manager, wait_until, name, with_clean=False)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert self._approaches_to_clean(moves) == 0
        assert self._purges(moves) == 1
        # こすらないなら引き込んだままでよい。余計な prime / retract を挟まない
        assert loads == []

    def test_does_not_clean_when_the_calibration_file_is_missing(
        self,
        cleaning_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        frames: list[Image],
        moves: list[dict[str, float]],
    ):
        """測れていないことは「塗れていない」ことの証拠にならない."""
        frames.append(_frame())

        record = self._run(cleaning_manager, wait_until, "absent.paste-volume.json")

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert self._approaches_to_clean(moves) == 0
