"""初回パージの検出とノズルクリーニング（`pcbasm.pasting.purge_check`）の契約.

パージが写らないのは詰まっている状態なので、掃除して 1 回だけやり直す。

それでも写らなければ pad を塗り始めずに落とす。

撮影や計測に失敗したときは落とさない（確かめられないことと写らないことは違う）。

装置側は自前 HAL の fake。
"""

from __future__ import annotations

import re
from collections.abc import Callable

import attrs
import pytest

from pcbasm.config import load_machine
from pcbasm.config.dispenser import NozzleCleanConfig
from pcbasm.geometry import Point2d, Point3d
from pcbasm.hal.fake import FakeCamera, FakeKlipper
from pcbasm.pasting.initial_purge import ResolvedInitialPurge
from pcbasm.pasting.labels import purge_point_label
from pcbasm.pasting.nozzle_clean import plan_nozzle_clean
from pcbasm.pasting.purge_check import purge_with_cleaning
from pcbasm.vision import Image
from tests.support.paste_session import (
    FOCUS_Z,
    RecordingHooks,
    dot_frame,
    full_board_correction,
    paste_session,
    uniform_frame,
)
from tests.support.paths import TESTING_CONFIG_DIR

# クリーニング位置（マシン座標）。fake の可動域 x/y 0-300・z -5..50 の内側
CLEAN_POSITION = Point3d(50.0, 60.0, 1.0)
CLEAN = NozzleCleanConfig(position=CLEAN_POSITION)
# パージ点（board 座標）。board_transform=Shift(100, 50) でマシン (110, 62)
PURGE_POINT = Point2d(10.0, 12.0)
# カメラだけが可動域外になる点。撮影はマシン Y = board Y + 50、塗布はさらに
# ツールヘッドオフセットぶん +Y なので、板の手前側ではカメラだけが届かなくなる
UNREACHABLE_POINT = Point2d(10.0, -60.0)


def _purge_at(point: Point2d) -> ResolvedInitialPurge:
    return ResolvedInitialPurge(
        amount_ul=0.3,
        point=point,
        label=purge_point_label(point),
        source="explicit",
    )


PURGE = _purge_at(PURGE_POINT)
# 検出させるドットの直径 [px]（crop の中央に収まり、min_area_px を超える大きさ）
DOT_PX = 10

# 位置決めを伴わない押し出し（prime / クリーニングのパージ / リトラクト）の抽出用。
# fake の rotation_distance は 1.0 なので、距離 [mm] = 回転数になる
_STEPPER_MOVE_RE = re.compile(r"MANUAL_STEPPER STEPPER=paste_dispenser MOVE=(\S+)(.*)")


def _dispenser():
    return load_machine(TESTING_CONFIG_DIR / "machine.toml").paste_dispenser


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


def _half_size_frame() -> Image:
    """解像度の違うフレーム（crop が取れず、確かめられない状態を作る）."""
    return Image(uniform_frame().numpy()[:240, :320])


def _purge_y(point: Point2d) -> float:
    """パージ時のマシン Y（ツールヘッドオフセットを含む）."""
    session = paste_session(FakeCamera([uniform_frame()]), FakeKlipper())
    return session.point_transform(point, full_board_correction()).apply(point).y


@attrs.frozen
class _Purged:
    """`purge_with_cleaning` 1 回の結果.

    Attributes:
        moves: 送った G1（撮影・パージ・クリーニングの経路を見る）
        loads: 単独の押し出し量 [uL]（prime / パージ / リトラクト）
        error: 送出した例外（無ければ ``None``）
    """

    moves: list[dict[str, float]]
    loads: list[float]
    error: ValueError | None


def _purge(
    frames: list[Image], *, with_clean: bool = True, unreachable: bool = False
) -> _Purged:
    """FakeCamera に ``frames`` を順に返させてパージ確認を通す."""
    klipper = FakeKlipper()
    session = paste_session(FakeCamera(frames), klipper)
    error: ValueError | None = None
    with session.make_applicator() as applicator:
        klipper.clear_sent()
        try:
            purge_with_cleaning(
                RecordingHooks(),
                session,
                full_board_correction(),
                applicator,
                _purge_at(UNREACHABLE_POINT if unreachable else PURGE_POINT),
                nozzle_clean=(
                    plan_nozzle_clean(session.stage, CLEAN_POSITION, CLEAN)
                    if with_clean
                    else None
                ),
            )
        except ValueError as exc:
            error = exc
        return _Purged(klipper.g1_moves(), _blocking_loads_ul(klipper), error)


class TestPurgeWithCleaning:
    """パージ → 塗布前後の撮影で検出 → 未検出なら掃除して 1 回だけやり直す."""

    @staticmethod
    def _is_clean_approach(move: dict[str, float]) -> bool:
        """クリーニング位置への XY 移動（approach の 1 手）か."""
        return (
            move.get("x") == pytest.approx(CLEAN_POSITION.x)
            and move.get("y") == pytest.approx(CLEAN_POSITION.y)
            and "z" not in move
        )

    @staticmethod
    def _is_purge(move: dict[str, float]) -> bool:
        """パージ点で塗る移動か."""
        return move.get("y") == pytest.approx(_purge_y(PURGE_POINT))

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

    def test_does_not_clean_when_the_purge_shows_up(self):
        """出ているならそのまま塗布へ進む."""
        purged = _purge([uniform_frame(), dot_frame(DOT_PX)])

        assert purged.error is None
        assert self._count(purged.moves, self._is_purge) == 1
        assert self._count(purged.moves, self._is_clean_approach) == 0
        assert purged.loads == []

    def test_captures_the_purge_point_before_and_after_purging(self):
        """塗布前後の差分で見るので、パージの前にも撮る."""
        purged = _purge([uniform_frame(), dot_frame(DOT_PX)])

        assert purged.error is None
        kinds = [
            "capture" if self._is_capture(move) else "purge"
            for move in purged.moves
            if self._is_capture(move) or self._is_purge(move)
        ]
        assert kinds[0] == "capture"
        assert "purge" in kinds
        assert kinds[-1] == "capture"

    def test_cleans_and_purges_again_when_nothing_shows_up(self):
        """1 回目が未検出 → クリーニング → もう一度パージして確かめる."""
        purged = _purge([uniform_frame(), uniform_frame(), dot_frame(DOT_PX)])

        assert purged.error is None
        assert self._count(purged.moves, self._is_clean_approach) == 1
        assert self._count(purged.moves, self._is_purge) == 2
        # 塗布前は 1 回しか撮らない。やり直しも同じ画像と比べないと、1 回目が実は
        # 出ていて検出だけ失敗した場合に増分しか見えず、正常なノズルを詰まりと断じる
        assert self._count(purged.moves, self._is_capture) == 3
        # 直前のパージはリトラクトで終わっている。押し戻してから掃除のパージをしないと
        # 設定した量が先端から出ない（出たあとはまた引き戻す）
        retract = _dispenser().motion.retract_amount
        assert purged.loads == pytest.approx([retract, CLEAN.purge_ul, -retract])

    def test_raises_when_the_second_purge_still_shows_nothing(self):
        """掃除しても出ないなら pad を塗り始めない."""
        purged = _purge([uniform_frame()])

        assert purged.error is not None
        assert "詰まり" in str(purged.error)
        # やり直しは 1 回だけ（無限に掃除し続けない）
        assert self._count(purged.moves, self._is_clean_approach) == 1
        assert self._count(purged.moves, self._is_purge) == 2

    def test_purges_again_without_wiping_when_the_position_is_not_recorded(self):
        """位置未記録ならこすれないが、パージのやり直しには意味がある."""
        purged = _purge(
            [uniform_frame(), uniform_frame(), dot_frame(DOT_PX)], with_clean=False
        )

        assert purged.error is None
        assert self._count(purged.moves, self._is_clean_approach) == 0
        assert self._count(purged.moves, self._is_purge) == 2
        # こすらないなら引き込んだままでよい。余計な prime / retract を挟まない
        assert purged.loads == []

    def test_purges_without_checking_when_the_camera_cannot_reach(self):
        """カメラが撮影位置へ届かない点では、確かめずにパージだけして進む."""
        purged = _purge([uniform_frame()], unreachable=True)

        assert purged.error is None
        assert self._count(purged.moves, self._is_capture) == 0
        assert self._count(purged.moves, self._is_clean_approach) == 0
        assert self._count(purged.moves, lambda move: "x" in move) == 1

    def test_purges_without_checking_when_the_images_cannot_be_measured(self):
        """計測できないことは「出ていない」ことの証拠にならない."""
        purged = _purge([uniform_frame(), _half_size_frame()])

        assert purged.error is None
        assert self._count(purged.moves, self._is_purge) == 1
        assert self._count(purged.moves, self._is_clean_approach) == 0
