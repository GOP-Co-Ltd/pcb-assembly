"""Posctrl/alignment の仕様テスト.

region-local-correction（区ごとの局所補正）の仕様に基づく。

実機 GENS_Power_Section_5（47.5x20mm）のログで、区ごとに測った変位から
**大域アフィン**を最小二乗した結果は `スケール x=+9563ppm / スキュー -1.24deg`
という物理的にありえない値になった。アンカーの y 方向の広がりが 10.1mm しか
無いので、100um 級の局所変動が短いレバー腕で 1 万 ppm 級に増幅されたため。
残差 RMS 114.2um / 最大 225.2um は照合ノイズ（5um 級）の 20〜45 倍で、
3.3mm しか離れていない隣接区の間でも dx が 0.36mm 振れる。つまり**実際の銅箔が
設計から局所的にずれている**（エッチングのレジストレーション誤差・基板の伸び・
反り）。ペーストを乗せる相手は設計 pad ではなく実銅箔なので、大域モデルを
当てはめるのではなく **pad ごとに最も近い区の変位をそのまま使う**のが正しい。

したがって `BoardAlignment` は模型を持たない:

- `correction_for(board_point)` は board 座標で最も近い成功区の `Shift` を返す
- タイルは等サイズの正方格子なので**最近傍の区中心 = その点を含む区**。
  「属する区を使う」と「区が無い / 失敗したら近傍を使う」が距離最小の 1 規則で
  表現でき、実装に包含判定とフォールバックの場合分けがあってはならない
- `mean_displacement` / `displacement_spread` はログと summary のための記述統計で、
  補正そのものには使わない（平均で補正すると局所性が消える）

距離重み付き補間は却下された設計なので、隣接区の値が混ざったら落ちるテストを
置く（`TestBoardAlignmentLocality`）。

RegionAlignmentSession は BoardCalibrationResult から照合の配線
（CopperProjector / CopperEdgeMatcher / CopperEdgeDetector / RegionAligner）を
集約する。plan_regions(pad_centers) は塗布対象 pad を含むタイルだけを返し、
measure() は照合失敗（RuntimeError）を漏らさず None を返す。

カメラは tests/helpers.py の FakeCamera（自前 HAL Camera の test Impl）、
klipper / stage は自前 HAL のため手書き stub、calibration は実
CalibrationResult、machine は実 Machine（data/testing/config/machine.toml）、
pcb は components / pads / copper / outline を返す手書き stub を使う。
"""

import logging
from collections.abc import Callable, Sequence
from datetime import datetime

import cv2
import numpy as np
import pytest
import shapely

from pcbasm import gcode
from pcbasm.config import Machine
from pcbasm.geometry import Identity, Point2d, Point3d, Rotation, Transform
from pcbasm.pcb import Component, Copper, CopperList, Layer, Outline, Pad
from pcbasm.posctrl.aligner import RegionAlignment
from pcbasm.posctrl.alignment import (
    BoardAlignment,
    RegionAlignmentSession,
    corrected_board_transform,
    corrected_pad_targets,
)
from pcbasm.posctrl.copper import EdgeMatch, centered_roi
from pcbasm.posctrl.region import AlignmentRegion
from pcbasm.posctrl.setup import BoardCalibrationResult
from pcbasm.vision import CalibrationResult, Image, Offset
from tests.helpers import TESTING_CONFIG_DIR, FakeCamera

WIDTH, HEIGHT = 1280, 720  # カメラフレームサイズ (px)
PPM = 10.0  # pixel/mm


def _square(cx: float, cy: float, half: float) -> shapely.Polygon:
    """中心 (cx, cy)、半辺 half の正方形ポリゴンを作る."""
    return shapely.Polygon(
        [
            (cx - half, cy - half),
            (cx + half, cy - half),
            (cx + half, cy + half),
            (cx - half, cy + half),
        ]
    )


def _component(designator: str, x: float, y: float) -> Component:
    return Component(
        designator=designator,
        value="10k",
        package="0402",
        position=Point2d(x, y),
        rotation=0.0,
        layer=Layer.TOP,
    )


def _pad(
    designator: str,
    x: float,
    y: float,
    half: float = 0.4,
    layer: Layer = Layer.TOP,
) -> Pad:
    """中心 (x, y) の正方形 pad を作る."""
    return Pad(
        designator=designator,
        pad_number="1",
        net_name="NET",
        layer=layer,
        polygon=_square(x, y, half),
    )


def _match(offset_px: Point2d = Point2d(0.0, 0.0)) -> EdgeMatch:
    """表示用フィールドを埋めるだけの照合結果."""
    return EdgeMatch(
        offset=Offset(px=offset_px, pixel_per_mm=PPM),
        rms_distance_px=0.4,
        sharpness=0.7,
    )


def _region(
    index: int, board_center: Point2d, anchor: Point2d | None = None
) -> AlignmentRegion:
    """指定の board 中心を持つ領域（anchor 既定は board 中心の鏡像写し）."""
    return AlignmentRegion(
        index=index,
        board_center=board_center,
        anchor=anchor if anchor is not None else _mirrored_anchor(board_center),
        roi=(0, 0, 100, 100),
        constraint=120.0,
        edge_point_count=240,
    )


def _mirrored_anchor(board_center: Point2d) -> Point2d:
    """Board 中心を x 反転 + 平行移動した機械座標.

    ルックアップの距離が **board 座標**で測られていることを見るための細工。
    x を反転すると board 座標での近傍順と anchor での近傍順が食い違うので、
    誤って `anchor` を使った実装は違う区の補正を返す。
    """
    return Point2d(100.0 - board_center.x, 50.0 + board_center.y)


def _alignment(
    index: int,
    board_center: Point2d,
    displacement: Point2d,
    *,
    anchor: Point2d | None = None,
    increment: Point2d = Point2d(0.0, 0.0),
    passes: int = 1,
    converged: bool = True,
) -> RegionAlignment:
    """指定の累積変位を持つ領域計測結果."""
    return RegionAlignment(
        region=_region(index, board_center, anchor),
        match=_match(),
        displacement=displacement,
        increment=increment,
        passes=passes,
        converged=converged,
    )


REGION_MM = 3.3  # 実機の region_size_px=100 @ 30.2px/mm 相当
HALF_MM = REGION_MM / 2

# board 座標 3.3mm 格子の 3x2 区と、そこで測った変位。実機で観測された
# 「3.3mm 離れた隣の区で dx が 0.36mm 違う」局所変動をそのまま持たせている。
# 大域アフィンならこの場は残差 100um 級を残すが、局所補正なら残差は無い。
LOCAL_FIELD: dict[tuple[int, int], Point2d] = {
    (0, 0): Point2d(0.10, -0.20),
    (1, 0): Point2d(0.46, -0.18),  # (0,0) と dx が 0.36mm 違う
    (2, 0): Point2d(0.20, -0.05),
    (0, 1): Point2d(-0.05, 0.12),
    (1, 1): Point2d(0.30, 0.08),
    (2, 1): Point2d(0.02, -0.30),
}


def _center(cell: tuple[int, int]) -> Point2d:
    """区 (i, j) の board 座標中心."""
    return Point2d(cell[0] * REGION_MM, cell[1] * REGION_MM)


def _local_board(cells: Sequence[tuple[int, int]] | None = None) -> BoardAlignment:
    """LOCAL_FIELD の指定区だけを成功区として持つ BoardAlignment."""
    used = list(LOCAL_FIELD) if cells is None else list(cells)
    return BoardAlignment(
        results=tuple(
            _alignment(index, _center(cell), LOCAL_FIELD[cell])
            for index, cell in enumerate(used)
        )
    )


def _shift_of(correction: Transform) -> Point2d:
    """純並進の補正 Transform が全点に与える平行移動ベクトル.

    2 点で同じ値になることを確かめてから返すので、レバー腕（点に依存する補正）が 紛れ込んでいれば assert で落ちる。
    """
    probes = (Point2d(0.0, 0.0), Point2d(180.0, -75.0))
    shifts = [correction.apply(point) - point for point in probes]
    assert shifts[0].x == pytest.approx(shifts[1].x, abs=1e-12)
    assert shifts[0].y == pytest.approx(shifts[1].y, abs=1e-12)
    return shifts[0]


class TestBoardAlignmentCorrectionFor:
    """correction_for: pad を含む区の補正を、距離最小の 1 規則で引く."""

    @pytest.mark.parametrize("cell", list(LOCAL_FIELD))
    @pytest.mark.parametrize(
        ("fx", "fy"), [(0.0, 0.0), (-0.9, -0.9), (0.9, -0.9), (-0.9, 0.9), (0.9, 0.9)]
    )
    def test_returns_the_correction_of_the_containing_region(
        self, cell: tuple[int, int], fx: float, fy: float
    ):
        """区の内側のどこでも、その区で測った変位そのものが返る.

        等サイズ正方格子では最近傍の区中心 = 包含区なので、区の隅 （中心から 0.9 *
        半辺）でも隣の区の値に切り替わってはならない。
        """
        board = _local_board()
        point = Point2d(_center(cell).x + fx * HALF_MM, _center(cell).y + fy * HALF_MM)

        shift = _shift_of(board.correction_for(point))

        assert shift.x == pytest.approx(LOCAL_FIELD[cell].x, abs=1e-12)
        assert shift.y == pytest.approx(LOCAL_FIELD[cell].y, abs=1e-12)

    def test_distance_is_measured_in_board_coordinates(self):
        """ルックアップは board 座標の距離で行う（機械座標の anchor ではない）.

        anchor は board 中心を x 反転して 100mm 平行移動した位置に置いてあるので、 board 点をそのまま
        anchor と比べた実装は必ず端の区を返す。
        """
        board = _local_board()

        shift = _shift_of(board.correction_for(_center((0, 0))))

        assert shift.x == pytest.approx(LOCAL_FIELD[(0, 0)].x, abs=1e-12)
        assert shift.y == pytest.approx(LOCAL_FIELD[(0, 0)].y, abs=1e-12)

    def test_correction_is_a_pure_shift(self):
        """補正は純並進（アンカーからの距離に依存しない）.

        レバー腕を持つ補正は pad の位置で誤差が線形に伸び、まさに大域アフィンを
        棄却した理由。`_shift_of` の内部 assert がこれを担保するが、
        意図として 1 件独立に置く。
        """
        board = _local_board()
        correction = board.correction_for(_center((1, 1)))

        far = Point2d(_center((1, 1)).x + 30.0, _center((1, 1)).y - 45.0)
        near_shift = correction.apply(_center((1, 1))) - _center((1, 1))
        far_shift = correction.apply(far) - far

        assert far_shift.x == pytest.approx(near_shift.x, abs=1e-12)
        assert far_shift.y == pytest.approx(near_shift.y, abs=1e-12)

    def test_missing_region_falls_back_to_the_nearest_successful_one(self):
        """照合に失敗してスキップされた区の pad は、最も近い成功区の補正を受ける.

        区 (1,0) が results に無い状態。その区の中心から x に −0.6mm 寄った点は (0,0)
        が最近傍（2.7mm）で、次に近いのは (1,1)（3.35mm）。包含判定と 近傍探索を分けた場合分けが要らないことのピン。
        """
        board = _local_board([c for c in LOCAL_FIELD if c != (1, 0)])
        point = Point2d(_center((1, 0)).x - 0.6, _center((1, 0)).y)

        shift = _shift_of(board.correction_for(point))

        assert shift.x == pytest.approx(LOCAL_FIELD[(0, 0)].x, abs=1e-12)
        assert shift.y == pytest.approx(LOCAL_FIELD[(0, 0)].y, abs=1e-12)

    def test_point_far_outside_every_region_uses_the_nearest_region(self):
        """全区の外側の pad でも例外にせず、最近傍の区の補正を使う.

        pad を含む区しか計画しないので通常は起きないが、区が失敗で落ちれば 起こり得る。塗布を止めるのは min_regions
        の役目で、ここではない。
        """
        board = _local_board()
        point = Point2d(_center((2, 1)).x + 40.0, _center((2, 1)).y + 40.0)

        shift = _shift_of(board.correction_for(point))

        assert shift.x == pytest.approx(LOCAL_FIELD[(2, 1)].x, abs=1e-12)
        assert shift.y == pytest.approx(LOCAL_FIELD[(2, 1)].y, abs=1e-12)

    def test_equidistant_regions_resolve_to_the_first_result_deterministically(self):
        """同距離なら results の先頭が勝ち、何度呼んでも同じ値を返す.

        等サイズ格子では起きないが、ルックアップが呼ぶたびに違う区を返す （dict / set の反復順に依存する）実装だと pad
        ごとに補正が揺れる。
        """
        board = _local_board([(0, 0), (2, 0)])
        midpoint = Point2d((_center((0, 0)).x + _center((2, 0)).x) / 2, 0.0)

        shifts = [_shift_of(board.correction_for(midpoint)) for _ in range(5)]

        assert {(s.x, s.y) for s in shifts} == {
            (LOCAL_FIELD[(0, 0)].x, LOCAL_FIELD[(0, 0)].y)
        }


class TestBoardAlignmentLocality:
    """隣接区の値が混ざらないこと（距離重み付き補間は却下された設計）."""

    def test_adjacent_regions_keep_their_own_displacement(self):
        """隣り合う区の pad が、それぞれ自分の区の値を厳密に受け取る.

        実機で観測された 0.36mm / 3.3mm の勾配。中心から 0.8 * 半辺 だけ隣の区へ 寄せた 2
        点でも、補間せず自分の区の値のままでなければならない。
        """
        board = _local_board()
        left = Point2d(_center((0, 0)).x + 0.8 * HALF_MM, 0.0)
        right = Point2d(_center((1, 0)).x - 0.8 * HALF_MM, 0.0)

        left_shift = _shift_of(board.correction_for(left))
        right_shift = _shift_of(board.correction_for(right))

        assert left_shift.x == pytest.approx(LOCAL_FIELD[(0, 0)].x, abs=1e-12)
        assert right_shift.x == pytest.approx(LOCAL_FIELD[(1, 0)].x, abs=1e-12)

    def test_the_correction_steps_discontinuously_across_the_region_boundary(self):
        """区境界をまたぐ 0.2mm で補正が 0.36mm まるごと切り替わる.

        距離重み付き補間・平均・多項式当てはめのいずれでも、0.2mm しか離れて いない 2
        点の補正差はこの段差にならない（連続な場は作れない）。 混ざった実装をこの 1 件で落とす。
        """
        board = _local_board()
        boundary = (_center((0, 0)).x + _center((1, 0)).x) / 2  # 1.65mm
        inside_left = Point2d(boundary - 0.1, 0.0)
        inside_right = Point2d(boundary + 0.1, 0.0)

        left_shift = _shift_of(board.correction_for(inside_left))
        right_shift = _shift_of(board.correction_for(inside_right))

        step = LOCAL_FIELD[(1, 0)].x - LOCAL_FIELD[(0, 0)].x
        assert step == pytest.approx(0.36, abs=1e-12)
        assert left_shift.x == pytest.approx(LOCAL_FIELD[(0, 0)].x, abs=1e-12)
        assert right_shift.x == pytest.approx(LOCAL_FIELD[(1, 0)].x, abs=1e-12)
        assert right_shift.x - left_shift.x == pytest.approx(step, abs=1e-12)

    def test_no_region_receives_the_mean_of_the_field(self):
        """どの区の pad も平均変位を受け取らない（平均補正への退化の否定）.

        大域アフィンを捨てたからといって「平均で 1 回補正する」に戻ると、実機の 局所変動（区ごとに最大
        225um）がそのまま誤差として残る。
        """
        board = _local_board()
        mean = board.mean_displacement

        for cell, displacement in LOCAL_FIELD.items():
            shift = _shift_of(board.correction_for(_center(cell)))
            assert shift.x == pytest.approx(displacement.x, abs=1e-12)
            assert abs(shift.x - mean.x) > 1e-3  # どの区も平均から 1um 以上離れている


class TestBorrowedCorrections:
    """借用補正の診断: 自区を持たない pad がどれだけ離れた区の補正を借りたか.

    ROI 全体を基板外形の内側へ収める条件で外周付近のタイルが落ちるので、全区が 成功しても自区を持たない pad が残る（実測
    19/48 pad・中央値 3.8mm）。局所ずれ には勾配があるので（0.36mm /
    3.3mm）、借用距離がそのまま誤差の上限になる。 判定は「最近傍区までの距離が区の半辺以下か」の 1 規則だけ。
    """

    def test_points_inside_their_own_region_are_not_borrowing(self):
        """自区の内側の点は 1 件も借用にならない."""
        board = _local_board()
        points = [
            Point2d(_center(cell).x + 0.4 * HALF_MM, _center(cell).y - 0.4 * HALF_MM)
            for cell in LOCAL_FIELD
        ]

        borrowed = board.borrowed_corrections(points, region_size_mm=REGION_MM)

        assert borrowed.pad_count == len(points)
        assert borrowed.borrowed_count == 0
        assert borrowed.median_distance == pytest.approx(0.0)
        assert borrowed.max_distance == pytest.approx(0.0)

    def test_distance_exactly_at_the_half_edge_is_not_borrowing(self):
        """半辺ちょうどは自区扱い（境界の向きを固定する）.

        判定が `>` か `>=` かで、格子の境界に乗った pad が丸ごと借用側へ倒れる。
        """
        board = _local_board([(0, 0)])
        on_boundary = Point2d(_center((0, 0)).x + HALF_MM, _center((0, 0)).y)
        just_outside = Point2d(_center((0, 0)).x + HALF_MM + 1e-6, _center((0, 0)).y)

        assert (
            board.borrowed_corrections(
                [on_boundary], region_size_mm=REGION_MM
            ).borrowed_count
            == 0
        )
        assert (
            board.borrowed_corrections(
                [just_outside], region_size_mm=REGION_MM
            ).borrowed_count
            == 1
        )

    def test_counts_and_summarises_only_the_borrowing_points(self):
        """中央値・最大の母集団は借用した点だけ（自区がある点は含めない）.

        自区の点（距離ほぼ 0）を母集団に混ぜると中央値が 0 側へ引っ張られ、
        「どれだけ遠くから借りているか」という判断材料にならなくなる。
        """
        board = _local_board([(0, 0)])
        origin = _center((0, 0))
        points = [
            origin,  # 自区（距離 0）
            Point2d(origin.x + 3.0, origin.y),  # 借用 3.0mm
            Point2d(origin.x + 4.0, origin.y),  # 借用 4.0mm
            Point2d(origin.x + 9.0, origin.y),  # 借用 9.0mm
        ]

        borrowed = board.borrowed_corrections(points, region_size_mm=REGION_MM)

        assert borrowed.pad_count == 4
        assert borrowed.borrowed_count == 3
        assert borrowed.median_distance == pytest.approx(4.0, abs=1e-9)
        assert borrowed.max_distance == pytest.approx(9.0, abs=1e-9)

    def test_distance_is_measured_to_the_nearest_successful_region(self):
        """借用距離は最近傍の**成功**区までの距離.

        失敗した区の中心までの距離で測ると、実際に使われる補正の出所と食い違う。
        """
        board = _local_board([(0, 0), (2, 1)])
        # 落ちた区 (1,0) の中心。成功区 (0,0) までは REGION_MM
        point = _center((1, 0))

        borrowed = board.borrowed_corrections([point], region_size_mm=REGION_MM)

        assert borrowed.borrowed_count == 1
        assert borrowed.max_distance == pytest.approx(REGION_MM, abs=1e-9)

    def test_no_points_yields_zero_counts(self):
        """対象点が空でも例外にせず 0 を返す（ログの分母が 0 になるだけ）."""
        borrowed = _local_board().borrowed_corrections([], region_size_mm=REGION_MM)

        assert borrowed.pad_count == 0
        assert borrowed.borrowed_count == 0
        assert borrowed.median_distance == pytest.approx(0.0)


class TestCorrectedBoardTransform:
    """corrected_board_transform: 「補正は board 変換の直後」というドメイン規則.

    照合で測った変位は**カメラの機械座標系**で定義されている。board 変換の前に
    挿す（= board 空間へ補正を持ち込む共役適用）と、board_transform が回転や
    スケールを持つぶんだけ補正がねじれる。この規則はコード全体でこの関数 1 箇所
    にしか書かれていないので、ここを厚くピンする。
    """

    BOARD = Rotation(30.0)

    def test_applies_the_local_correction_in_machine_coordinates(self):
        """T(p) = board_transform(p) + その区の変位（機械座標での平行移動）."""
        board = _local_board()

        for cell, displacement in LOCAL_FIELD.items():
            point = Point2d(_center(cell).x + 0.8 * HALF_MM, _center(cell).y)
            moved = corrected_board_transform(self.BOARD, board, point).apply(point)

            want = self.BOARD.apply(point)
            assert moved.x == pytest.approx(want.x + displacement.x, abs=1e-9)
            assert moved.y == pytest.approx(want.y + displacement.y, abs=1e-9)

    def test_correction_is_not_conjugated_into_board_space(self):
        """Board 空間で補正してから board 変換する構造とは違う結果になる.

        board_transform が 30° 回転なので、共役適用（先に board 座標を動かす）は 変位を 30°
        回した量だけずらす。ユーザーが明示的に却下した構造。
        """
        board = _local_board()
        point = _center((1, 0))
        displacement = LOCAL_FIELD[(1, 0)]

        moved = corrected_board_transform(self.BOARD, board, point).apply(point)

        conjugated = self.BOARD.apply(point + displacement)
        assert moved.x != pytest.approx(conjugated.x, abs=1e-6)
        assert moved.y != pytest.approx(conjugated.y, abs=1e-6)

    def test_each_point_gets_its_own_region_correction(self):
        """引数の点ごとに違う補正が入る（全点で同じ変換を返さない）.

        呼び出し側が 1 回だけ組んで全 pad に使い回す退行を落とす。
        """
        board = _local_board()
        left = Point2d(_center((0, 0)).x + 0.8 * HALF_MM, 0.0)
        right = Point2d(_center((1, 0)).x - 0.8 * HALF_MM, 0.0)

        left_shift = corrected_board_transform(self.BOARD, board, left).apply(
            left
        ) - self.BOARD.apply(left)
        right_shift = corrected_board_transform(self.BOARD, board, right).apply(
            right
        ) - self.BOARD.apply(right)

        assert left_shift.x == pytest.approx(LOCAL_FIELD[(0, 0)].x, abs=1e-9)
        assert right_shift.x == pytest.approx(LOCAL_FIELD[(1, 0)].x, abs=1e-9)
        assert right_shift.x - left_shift.x == pytest.approx(0.36, abs=1e-9)

    def test_the_correction_is_looked_up_at_the_given_point_not_the_applied_one(self):
        """補正を引く点と、変換を適用する点は独立に選べる.

        pad 中心で引いた補正を pad ポリゴンの全頂点へ適用する（塗布の使い方）。 頂点ごとに引き直すと 1 つの pad
        が区境界をまたいだとき形が割れる。
        """
        board = _local_board()
        center = Point2d(_center((0, 0)).x + 0.8 * HALF_MM, 0.0)
        transform = corrected_board_transform(self.BOARD, board, center)

        # 区境界の向こう側にある頂点も、pad 中心で引いた補正で動く
        vertex = Point2d(center.x + REGION_MM, center.y)
        moved = transform.apply(vertex)

        want = self.BOARD.apply(vertex)
        assert moved.x == pytest.approx(want.x + LOCAL_FIELD[(0, 0)].x, abs=1e-9)
        assert moved.y == pytest.approx(want.y + LOCAL_FIELD[(0, 0)].y, abs=1e-9)


class TestCorrectedPadTargets:
    """corrected_pad_targets: pad と補正後の機械座標の対応を値として返す.

    board_tour は「どの pad にどの補正を当てたか」をこの戻り値から受け取る。
    対応をループの書き方に委ねると、補正を引く点を固定してしまう退行
    （全 pad が同じ補正で巡回する）が job 層で起きても誰も気づけない。
    """

    BOARD = Rotation(30.0)

    @staticmethod
    def _pad_at(designator: str, center: Point2d) -> Pad:
        return _pad(designator, center.x, center.y, half=0.3)

    def _adjacent_pads(self) -> tuple[Pad, Pad]:
        """区 (0,0) と (1,0) にそれぞれ入る 2 pad（変位差 0.36mm）."""
        return (
            self._pad_at("R1", Point2d(_center((0, 0)).x + 0.8 * HALF_MM, 0.0)),
            self._pad_at("R2", Point2d(_center((1, 0)).x - 0.8 * HALF_MM, 0.0)),
        )

    def test_each_pad_target_uses_the_correction_of_its_own_region(self):
        """巡回先 = board 変換 + **その pad の区**の変位.

        補正を引く点を固定した実装では 2 pad のずれが揃ってしまい、0.36mm の 段差が消える。
        """
        board = _local_board()
        pads = self._adjacent_pads()

        targets = corrected_pad_targets(self.BOARD, board, pads)

        shifts = [target - self.BOARD.apply(pad.center) for pad, target in targets]
        assert shifts[0].x == pytest.approx(LOCAL_FIELD[(0, 0)].x, abs=1e-9)
        assert shifts[1].x == pytest.approx(LOCAL_FIELD[(1, 0)].x, abs=1e-9)
        assert shifts[1].x - shifts[0].x == pytest.approx(0.36, abs=1e-9)

    def test_keeps_the_input_pads_in_order(self):
        """戻り値は入力 pad と同順・同数（並べ替えない）.

        呼び出し側は index で pad と巡回先を対応づけるので、順序が変わると 「どの pad
        へ向かっているか」の表示と実際の移動先がずれる。designator の 辞書順とは違う並びで渡し、値と designator
        の対応もあわせて見る。
        """
        board = _local_board()
        left, right = self._adjacent_pads()
        pads = [
            self._pad_at("R9", right.center),
            self._pad_at("R1", left.center),
            self._pad_at("R5", right.center),
        ]

        targets = corrected_pad_targets(self.BOARD, board, pads)

        assert [pad.designator for pad, _ in targets] == ["R9", "R1", "R5"]
        expected = [
            LOCAL_FIELD[(1, 0)].x,
            LOCAL_FIELD[(0, 0)].x,
            LOCAL_FIELD[(1, 0)].x,
        ]
        for (pad, target), want in zip(targets, expected, strict=True):
            shift = target - self.BOARD.apply(pad.center)
            assert shift.x == pytest.approx(want, abs=1e-9)

    def test_no_pads_yields_an_empty_list(self):
        """Pad が空でも例外にしない（塗布対象 0 件は呼び出し側の判断）."""
        assert corrected_pad_targets(self.BOARD, _local_board(), []) == []


class TestBoardAlignmentStatistics:
    """mean_displacement / displacement_spread（ログ・summary 用の記述統計）."""

    def test_mean_displacement_is_the_arithmetic_mean(self):
        board = _local_board()

        mean = board.mean_displacement

        assert mean.x == pytest.approx(
            float(np.mean([d.x for d in LOCAL_FIELD.values()])), abs=1e-12
        )
        assert mean.y == pytest.approx(
            float(np.mean([d.y for d in LOCAL_FIELD.values()])), abs=1e-12
        )

    def test_displacement_spread_is_the_per_axis_population_std(self):
        """ばらつきは軸ごとの母標準偏差（n で割る）.

        ユーザーが実機で「局所変動が照合ノイズ（5um 級）に対してどれだけ大きいか」 を読む値。標本標準偏差（n-1）や peak-
        to-peak では意味が変わる。
        """
        board = _local_board()

        spread = board.displacement_spread

        assert spread.x == pytest.approx(
            float(np.std([d.x for d in LOCAL_FIELD.values()])), abs=1e-12
        )
        assert spread.y == pytest.approx(
            float(np.std([d.y for d in LOCAL_FIELD.values()])), abs=1e-12
        )
        assert spread.x > 0.1  # この場は 100um 級に振れている

    def test_displacement_spread_is_zero_for_a_single_region(self):
        """1 区だけならばらつきは 0（統計として定義できる下限）."""
        board = _local_board([(0, 0)])

        spread = board.displacement_spread

        assert spread.x == pytest.approx(0.0, abs=1e-12)
        assert spread.y == pytest.approx(0.0, abs=1e-12)

    def test_uniform_field_has_zero_spread_and_the_common_mean(self):
        """全区が同じ変位なら平均 = その値・ばらつき 0（純並進の場への退化）."""
        shift = Point2d(0.12, -0.34)
        board = BoardAlignment(
            results=tuple(
                _alignment(index, _center(cell), shift)
                for index, cell in enumerate(LOCAL_FIELD)
            )
        )

        assert board.mean_displacement.x == pytest.approx(shift.x, abs=1e-12)
        assert board.mean_displacement.y == pytest.approx(shift.y, abs=1e-12)
        assert board.displacement_spread.x == pytest.approx(0.0, abs=1e-12)
        assert board.displacement_spread.y == pytest.approx(0.0, abs=1e-12)
        for cell in LOCAL_FIELD:
            assert _shift_of(board.correction_for(_center(cell))).x == pytest.approx(
                shift.x, abs=1e-12
            )

    def test_results_keeps_the_successful_measurements_in_order(self):
        """Results は成功区のみを計測順に保持する（ログの領域番号との対応）."""
        cells = [c for c in LOCAL_FIELD if c != (1, 1)]

        board = _local_board(cells)

        assert len(board.results) == len(cells)
        for result, cell in zip(board.results, cells, strict=True):
            assert result.region.board_center.x == pytest.approx(_center(cell).x)
            assert result.region.board_center.y == pytest.approx(_center(cell).y)

    def test_empty_results_raise_value_error(self):
        """成功領域が 0 件の BoardAlignment は作れない（補正が定義できない）."""
        with pytest.raises(ValueError):
            BoardAlignment(results=())


def _board_image(shift_x: int = 0, shift_y: int = 0) -> Image:
    """黒地に白矩形 (600,320)-(680,400) を指定 px ずらして描いた合成画像.

    銅箔 ±4mm 角（board 原点中心）、board/offset 変換 Identity、stage が anchor (0,0)
    のとき、投影公式 pixel = center + ppm*(s − b) で銅箔は (600,320)-(680,400) px
    に投影される（画像中心 ROI の内側）。塗り潰しの右下端を +1px 伸ばすのは、 Canny が明側の外周 1px
    を落とすことで生じる −0.5px の系統ずれを打ち消すため。
    """
    frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    cv2.rectangle(
        frame,
        (600 + shift_x, 320 + shift_y),
        (681 + shift_x, 401 + shift_y),
        (255, 255, 255),
        thickness=-1,
    )
    return Image(frame)


def _machine_config() -> Machine:
    return Machine(TESTING_CONFIG_DIR / "machine.toml")


def _calibration(
    resolution: tuple[int, int] = (WIDTH, HEIGHT),
) -> CalibrationResult:
    return CalibrationResult(
        pixel_per_mm=PPM,
        square_size_mm=1.0,
        mean_distance_px=PPM,
        std_distance_px=0.0,
        resolution=resolution,
        crop_size=(600, 600),
        calibrated_at=datetime.now(),
        z_position=5.0,
    )


class _StubKlipper:
    """send_gcode を数えるだけの Klipper 代替（自前 HAL）."""

    def __init__(self) -> None:
        self.sent: list[gcode.GCode] = []

    def send_gcode(self, code: gcode.GCode) -> None:
        self.sent.append(code)


class _StubStage:
    """Move() の指令位置を get_position() が追跡する XYZStage 代替（自前 HAL）."""

    max_velocity = 100.0

    def __init__(self) -> None:
        self._position = Point3d(0.0, 0.0, 5.0)
        self.moves: list[Point2d] = []

    def move(self, **kwargs) -> gcode.GCode:
        current = self._position
        self._position = Point3d(
            kwargs.get("x", current.x),
            kwargs.get("y", current.y),
            kwargs.get("z", current.z),
        )
        self.moves.append(Point2d(self._position.x, self._position.y))
        return gcode.GCode("G1")

    def get_position(self) -> Point3d:
        return self._position


class _StubPcb:
    """Outline / copper / pads / components だけを持つ PcbFile 代替."""

    def __init__(
        self,
        *,
        outline: Outline,
        copper: CopperList,
        pads: list[Pad],
        components: list[Component],
    ) -> None:
        self.outline = outline
        self.copper = copper
        self.pads = pads
        self.components = components


class TestRegionAlignmentSession:
    """RegionAlignmentSession の配線・領域計画・反復計測・失敗の握りつぶし."""

    @pytest.fixture
    def klipper(self) -> _StubKlipper:
        return _StubKlipper()

    @pytest.fixture
    def stage(self) -> _StubStage:
        return _StubStage()

    @staticmethod
    def _region_mm() -> float:
        return _machine_config().paste_dispenser.pad_align.region_size_px / PPM

    @classmethod
    def _pcb(cls, outline_half: float | None = None) -> _StubPcb:
        """Board 原点中心 ±4mm の TOP 銅箔と、原点に 1 個の TOP pad を持つ PCB.

        外形は既定で「ROI の半辺 + 外周マージン + 1mm」角。原点のタイルだけが ROI ごと safe_area
        に収まり、隣のタイル（region_mm 離れ）は必ず外へ出る。 BOTTOM 層にも銅箔と pad を置き、TOP
        だけが照合対象になることを見る。
        """
        pad_align = _machine_config().paste_dispenser.pad_align
        if outline_half is None:
            outline_half = cls._region_mm() / 2 + pad_align.board_edge_margin + 1.0
        return _StubPcb(
            outline=Outline(_square(0.0, 0.0, outline_half)),
            copper=CopperList(
                [
                    Copper(layer=Layer.TOP, polygon=_square(0.0, 0.0, 4.0)),
                    Copper(layer=Layer.BOTTOM, polygon=_square(300.0, 300.0, 4.0)),
                ]
            ),
            pads=[
                _pad("R1", 0.0, 0.0, half=0.5),
                _pad("B1", 300.0, 300.0, half=0.5, layer=Layer.BOTTOM),
            ],
            components=[_component("R1", 0.0, 0.0)],
        )

    @staticmethod
    def _session(
        camera: FakeCamera,
        klipper: _StubKlipper,
        stage: _StubStage,
        pcb: _StubPcb,
        board_transform: Transform | None = None,
        frame_sink: Callable[[Image], None] | None = None,
        resolution: tuple[int, int] = (WIDTH, HEIGHT),
    ) -> RegionAlignmentSession:
        result = BoardCalibrationResult(
            machine=_machine_config(),
            klipper=klipper,  # type: ignore[arg-type]
            stage=stage,  # type: ignore[arg-type]
            camera=camera,
            calibration=_calibration(resolution),
            offset_transform=Identity(),
            board_transform=(
                board_transform if board_transform is not None else Identity()
            ),
            pcb=pcb,  # type: ignore[arg-type]
        )
        return RegionAlignmentSession.from_calibration(result, frame_sink=frame_sink)

    def test_region_roi_is_the_image_centered_square(self, klipper, stage):
        """region_roi = centered_roi(キャリブレーション解像度, region_size_px).

        overlay の描画範囲と照合 ROI が同一であることの根拠。
        """
        session = self._session(
            FakeCamera([_board_image()]), klipper, stage, self._pcb()
        )

        size = _machine_config().paste_dispenser.pad_align.region_size_px
        assert session.region_roi == centered_roi((WIDTH, HEIGHT), size)

    def test_region_plus_search_window_must_fit_in_the_resolution(self, klipper, stage):
        """region_size_px + 2*window_px が解像度に収まらなければ構築時 ValueError.

        収まらない設定では探索窓が切り詰められ、ずれの計測範囲が黙って狭くなる。 設定ミスを実行時ではなく配線時に落とす。
        """
        # 解像度を region_size_px そのものにすると探索窓の余地が必ず無くなる
        size = _machine_config().paste_dispenser.pad_align.region_size_px
        with pytest.raises(ValueError, match="region_size_px"):
            self._session(
                FakeCamera([_board_image()]),
                klipper,
                stage,
                self._pcb(),
                resolution=(size, size),
            )

    def test_plan_regions_uses_the_pad_centers_and_the_shared_roi(self, klipper, stage):
        """塗布対象 pad を含むタイルを計画し、roi は region_roi と同一.

        BOTTOM 層の銅箔（300mm 離れた位置）は外形の外なので候補にならない。 タイルの位相は pad 重心なので、pad 1
        個ならその pad 中心がアンカーになる。
        """
        session = self._session(
            FakeCamera([_board_image()]), klipper, stage, self._pcb()
        )

        regions = session.plan_regions([Point2d(0.0, 0.0)])

        assert len(regions) == 1
        region = regions[0]
        assert region.index == 0
        assert region.roi == session.region_roi
        assert region.anchor.x == pytest.approx(0.0, abs=1e-6)
        assert region.anchor.y == pytest.approx(0.0, abs=1e-6)
        # board_center は correction_for のルックアップ鍵（board 座標）
        assert region.board_center.x == pytest.approx(0.0, abs=1e-6)
        assert region.board_center.y == pytest.approx(0.0, abs=1e-6)
        assert region.constraint > 0.0

    def test_region_size_mm_is_the_tile_edge_in_board_millimetres(self, klipper, stage):
        """region_size_mm = region_size_px / pixel_per_mm（借用距離の判定に使う）.

        borrowed_corrections の「半辺以下なら自区」判定はこの値が正しいことに 依存する。px と mm
        を取り違えると借用件数が桁で狂う。
        """
        session = self._session(
            FakeCamera([_board_image()]), klipper, stage, self._pcb()
        )

        size_px = _machine_config().paste_dispenser.pad_align.region_size_px
        assert session.region_size_mm == pytest.approx(size_px / PPM)

    def test_plan_regions_without_pads_returns_no_regions(self, klipper, stage):
        """塗布対象 pad が無ければ領域も 0 個（例外は投げない）."""
        session = self._session(
            FakeCamera([_board_image()]), klipper, stage, self._pcb()
        )

        assert session.plan_regions([]) == []

    def test_plan_regions_shrinks_the_outline_by_board_edge_margin(
        self, klipper, stage
    ):
        """照合領域は outline.buffer(-board_edge_margin) の内側からしか選ばない.

        ROI の半辺 + マージン より 0.5mm 小さい外形にすると、pad 中心のタイルでも ROI が safe_area
        に収まらず領域 0 個になる。外形をそのまま使っていれば 領域が選ばれてしまうので、マージンが実際に効いていることのピンになる
        （外周部でマッチしないというユーザー要求）。
        """
        pad_align = _machine_config().paste_dispenser.pad_align
        half = self._region_mm() / 2 + pad_align.board_edge_margin
        camera = FakeCamera([_board_image()])
        too_small = self._session(camera, klipper, stage, self._pcb(half - 0.5))
        just_enough = self._session(camera, klipper, stage, self._pcb(half + 0.5))

        assert too_small.plan_regions([Point2d(0.0, 0.0)]) == []
        assert len(just_enough.plan_regions([Point2d(0.0, 0.0)])) == 1

    def test_measure_accumulates_the_displacement_over_passes(self, klipper, stage):
        """既知ずれ (+2,−2)px の観測 → displacement ≈ (−0.2, +0.2) mm.

        2 パス目は累積変位を board 変換の後段に挿した投影で測るので、同じ変位を 二重に足さない（2
        パス目の観測は補正後の想定と一致するので増分 0）。
        """
        camera = FakeCamera([_board_image(2, -2), _board_image()])
        session = self._session(camera, klipper, stage, self._pcb())
        region = session.plan_regions([Point2d(0.0, 0.0)])[0]

        alignment = session.measure(region)

        assert alignment is not None
        assert alignment.displacement.x == pytest.approx(-0.2, abs=0.05)
        assert alignment.displacement.y == pytest.approx(0.2, abs=0.05)
        assert alignment.passes == 2
        assert alignment.converged is True
        assert camera.capture_count == 2

    def test_measure_returns_none_and_warns_when_matching_fails(
        self, klipper, stage, caplog
    ):
        """照合失敗（RuntimeError）は漏らさず警告 log の後 None を返す.

        ループ側（measure_regions）が失敗領域を数えて続行できるようにする。
        """
        camera = FakeCamera([Image(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))])
        session = self._session(camera, klipper, stage, self._pcb())
        region = session.plan_regions([Point2d(0.0, 0.0)])[0]

        with caplog.at_level(logging.WARNING):
            alignment = session.measure(region)

        assert alignment is None
        assert "照合" in caplog.text

    def test_measure_delivers_one_frame_per_pass_to_frame_sink(self, klipper, stage):
        """frame_sink 指定時、パスごとに照合状況の合成フレームが届く（webui プレビュー）."""
        frames: list[Image] = []
        camera = FakeCamera([_board_image(2, -2), _board_image()])
        session = self._session(
            camera, klipper, stage, self._pcb(), frame_sink=frames.append
        )

        alignment = session.measure(session.plan_regions([Point2d(0.0, 0.0)])[0])

        assert alignment is not None
        assert len(frames) == alignment.passes
        assert frames[0].size == (WIDTH, HEIGHT)
