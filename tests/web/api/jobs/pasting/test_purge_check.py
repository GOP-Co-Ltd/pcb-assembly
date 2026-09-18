"""初回パージの検出とノズルクリーニング（`web.api.jobs.pasting.purge_check`）の契約.

パージが写らないのは詰まっている状態なので、掃除して 1 回だけやり直す。

それでも写らなければ pad を塗り始めずに落とす。

撮影や計測に失敗したときは落とさない（確かめられないことと写らないことは違う）。

塗布ジョブ本体は装置を要求するので、合成ジョブへ同じ関数を通す。装置側は自前 HAL の fake。
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from pcbasm.config import Machine, NozzleClean
from pcbasm.geometry import Point2d
from pcbasm.pasting.initial_purge import ResolvedInitialPurge, purge_point_label
from pcbasm.vision import Image
from tests.helpers import TESTING_CONFIG_DIR, FakeCamera, FakeKlipper
from tests.web.api.jobs.conftest import (
    ManagerFactory,
    WaitUntil,
    register_synthetic,
)
from tests.web.api.jobs.pasting.conftest import (
    FOCUS_Z,
    dot_frame,
    full_board_correction,
    paste_session,
    uniform_frame,
)
from web.api.jobs.catalog import JobCatalog, ParamSpec
from web.api.jobs.context import JobContext, JobResult
from web.api.jobs.manager import JobManager, JobRecord, JobStatus
from web.api.jobs.pasting.purge_check import PURGE_CROP_SIZE_MM, purge_with_cleaning
from web.api.settings import Settings

# クリーニング位置（マシン座標）。fake の可動域 x/y 0-300・z -5..50 の内側
CLEAN = NozzleClean(x=50.0, y=60.0, z=1.0)
# パージ点（board 座標）。board_transform=Shift(100, 50) でマシン (110, 62)
PURGE_POINT = Point2d(10.0, 12.0)
PURGE = ResolvedInitialPurge(
    amount_ul=0.3,
    point=PURGE_POINT,
    label=purge_point_label(PURGE_POINT),
    source="explicit",
)
# 検出させるドットの直径 [px]（crop の中央に収まり、min_area_px を超える大きさ）
DOT_PX = 10

# 位置決めを伴わない押し出し（prime / クリーニングのパージ / リトラクト）の抽出用。
# fake の rotation_distance は 1.0 なので、距離 [mm] = 回転数になる
_STEPPER_MOVE_RE = re.compile(r"MANUAL_STEPPER STEPPER=paste_dispenser MOVE=(\S+)(.*)")


def _dispenser():
    return Machine(TESTING_CONFIG_DIR / "machine.toml").paste_dispenser


def _blocking_loads_ul(klipper: FakeKlipper) -> list[float]:
    """`load` / `retract` / `prime` が送った押し出し量 [uL] を順に返す.

    塗布シーケンス（`SYNC=0` の非同期 MOVE）は除く。
    """
    rotations_per_ul = _dispenser().rotations_per_ul
    return [
        float(match.group(1)) / rotations_per_ul
        for match in (_STEPPER_MOVE_RE.match(line) for line in klipper.sent_lines)
        if match and "SYNC=0" not in match.group(2)
    ]


def _purge_y() -> float:
    """パージ時のマシン Y（ツールヘッドオフセットを含む）."""
    session = paste_session(FakeCamera([uniform_frame()]), FakeKlipper())
    return (
        session.point_transform(PURGE_POINT, full_board_correction())
        .apply(PURGE_POINT)
        .y
    )


class TestPurgeWithCleaning:
    """パージ → 塗布前後の撮影で検出 → 未検出なら掃除して 1 回だけやり直す."""

    @pytest.fixture
    def moves(self) -> list[dict[str, float]]:
        """合成ジョブが記録した G1（撮影・パージ・クリーニングの経路を見る）."""
        return []

    @pytest.fixture
    def loads(self) -> list[float]:
        """合成ジョブが記録した単独の押し出し量 [uL]（prime / パージ / リトラクト）."""
        return []

    @pytest.fixture
    def frames(self) -> list[Image]:
        """FakeCamera へ順に返させるフレーム（テストごとに詰める）."""
        return []

    @pytest.fixture
    def purge_manager(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        moves: list[dict[str, float]],
        loads: list[float],
        frames: list[Image],
    ) -> JobManager:
        """`purge_with_cleaning` だけを呼ぶ合成ジョブを積んだ manager."""

        def run(ctx: JobContext) -> JobResult:
            klipper = FakeKlipper()
            session = paste_session(FakeCamera(frames), klipper)
            with session.make_applicator() as applicator:
                klipper.clear_sent()
                try:
                    purge_with_cleaning(
                        ctx,
                        session,
                        full_board_correction(),
                        applicator,
                        PURGE,
                        nozzle_clean=CLEAN if ctx.params["with_clean"] else None,
                    )
                finally:
                    moves.extend(klipper.g1_moves())
                    loads.extend(_blocking_loads_ul(klipper))
            return JobResult(summary="パージ完了")

        register_synthetic(
            catalog,
            run,
            name="purge_check",
            params=(ParamSpec("with_clean", "クリーニング位置", "bool", default=True),),
        )
        return make_manager(catalog)

    @staticmethod
    def _run(
        manager: JobManager, wait_until: WaitUntil, *, with_clean: bool = True
    ) -> JobRecord:
        record = manager.start("purge_check", {"with_clean": with_clean})
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
        """パージ点で塗る移動か."""
        return move.get("y") == pytest.approx(_purge_y())

    @staticmethod
    def _is_capture(move: dict[str, float]) -> bool:
        """撮影のためにピント高さへ動いたか."""
        return move.get("z") == pytest.approx(FOCUS_Z)

    @classmethod
    def _count(
        cls,
        moves: list[dict[str, float]],
        matches: Callable[[dict[str, float]], bool],
    ) -> int:
        """連続する移動のかたまりを 1 回と数える."""
        hits = [matches(move) for move in moves]
        return sum(
            1
            for index, here in enumerate(hits)
            if here and not (index > 0 and hits[index - 1])
        )

    def test_does_not_clean_when_the_purge_shows_up(
        self,
        purge_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        frames: list[Image],
        moves: list[dict[str, float]],
        loads: list[float],
    ):
        """出ているならそのまま塗布へ進む."""
        frames.extend([uniform_frame(), dot_frame(DOT_PX)])

        record = self._run(purge_manager, wait_until)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert self._count(moves, self._is_purge) == 1
        assert self._count(moves, self._is_clean_approach) == 0
        assert loads == []

    def test_captures_the_purge_point_before_and_after_purging(
        self,
        purge_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        frames: list[Image],
        moves: list[dict[str, float]],
    ):
        """塗布前後の差分で見るので、パージの前にも撮る."""
        frames.extend([uniform_frame(), dot_frame(DOT_PX)])

        record = self._run(purge_manager, wait_until)

        assert record.status == JobStatus.SUCCEEDED, record.error
        kinds = [
            "capture" if self._is_capture(move) else "purge"
            for move in moves
            if self._is_capture(move) or self._is_purge(move)
        ]
        assert kinds[0] == "capture"
        assert "purge" in kinds
        assert kinds[-1] == "capture"

    def test_cleans_and_purges_again_when_nothing_shows_up(
        self,
        purge_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        frames: list[Image],
        moves: list[dict[str, float]],
        loads: list[float],
    ):
        """1 回目が未検出 → クリーニング → もう一度パージして確かめる."""
        frames.extend([uniform_frame(), uniform_frame(), uniform_frame()])
        frames.append(dot_frame(DOT_PX))

        record = self._run(purge_manager, wait_until)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert self._count(moves, self._is_clean_approach) == 1
        assert self._count(moves, self._is_purge) == 2
        # 直前のパージはリトラクトで終わっている。押し戻してから掃除のパージをしないと
        # 設定した量が先端から出ない（出たあとはまた引き戻す）
        retract = _dispenser().retract_amount
        assert loads == pytest.approx([retract, CLEAN.purge_ul, -retract])

    def test_fails_the_job_when_the_second_purge_still_shows_nothing(
        self,
        purge_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        frames: list[Image],
        moves: list[dict[str, float]],
    ):
        """掃除しても出ないなら pad を塗り始めない."""
        frames.append(uniform_frame())

        record = self._run(purge_manager, wait_until)

        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert "詰まり" in record.error
        # やり直しは 1 回だけ（無限に掃除し続けない）
        assert self._count(moves, self._is_clean_approach) == 1
        assert self._count(moves, self._is_purge) == 2

    def test_purges_again_without_wiping_when_the_position_is_not_recorded(
        self,
        purge_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        frames: list[Image],
        moves: list[dict[str, float]],
        loads: list[float],
    ):
        """位置未記録ならこすれないが、パージのやり直しには意味がある."""
        frames.extend([uniform_frame(), uniform_frame(), uniform_frame()])
        frames.append(dot_frame(DOT_PX))

        record = self._run(purge_manager, wait_until, with_clean=False)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert self._count(moves, self._is_clean_approach) == 0
        assert self._count(moves, self._is_purge) == 2
        # こすらないなら引き込んだままでよい。余計な prime / retract を挟まない
        assert loads == []


class TestPurgeCropSize:
    """パージ点を切り出す寸法."""

    def test_is_wider_than_a_flow_calibration_crop(self):
        """パージのドットは測定点のドットより大きく広がる."""
        assert PURGE_CROP_SIZE_MM > _dispenser().flow_calibration.crop_size_mm
