"""Posctrl/pad の仕様テスト.

計画書 memory/agents/orchestrator/region-pad-align-plan.md「凍結する公開 IF」
「posctrl/pad.py」に基づく。CopperPadObserver は capture → detect_edges →
match_rigid(roi) → camera_transform の observer 契約（observe() -> Transform、
カメラ mm・画像中心原点・想定→観測）。PadAlignmentResult は machine_transform
から表示用の並進・回転を導出する。PadAligner の収束ループ自体は
XYPositionAdjustor のテストと実機検証でカバーする（計画書テスト計画）。

plan_pad_regions は board 原点 (0,0) 固定グリッド（セル = region_size）へ
pad.center の floor 除算で割り当てる純関数。銅箔（copper_polygon）が
中心セルの内部に完全に沈み境界と交差しない巨大 pad（サーマルパッドなど）は
「exterior とセル矩形の交差長が最大のセル」（タイブレーク (col,row) 昇順）へ
再割当てされる（エッジ皆無セルでの照合失敗回避）。

カメラは tests/helpers.py の FakeCamera（自前 HAL Camera の test Impl）、
エッジ検出は実 CopperEdgeDetector（Canny）を使う。
"""

import cv2
import numpy as np
import pytest
import shapely

from pcbasm.geometry import Compose, Point2d, Rotation, Scale, Shift, Transform
from pcbasm.pcb import Layer, Pad
from pcbasm.posctrl.copper import CopperEdgeMatcher, CopperProjection, RigidEdgeMatch
from pcbasm.posctrl.pad import CopperPadObserver, PadAlignmentResult, plan_pad_regions
from pcbasm.vision import CopperEdgeDetector, Image, Offset
from tests.helpers import FakeCamera

PPM = 10.0  # pixel/mm


def _board_image(shift_x: int = 0, shift_y: int = 0) -> Image:
    """黒地に白矩形 (60,60)-(140,140) を指定 px ずらして描いた 200x200 画像.

    実 CopperEdgeDetector の Canny で矩形境界がエッジ化される合成入力。
    """
    frame = np.zeros((200, 200, 3), dtype=np.uint8)
    cv2.rectangle(
        frame,
        (60 + shift_x, 60 + shift_y),
        (140 + shift_x, 140 + shift_y),
        (255, 255, 255),
        thickness=-1,
    )
    return Image(frame)


def _projection() -> CopperProjection:
    """(60,60)-(140,140) の矩形を想定銅箔とする CopperProjection."""
    fill = np.zeros((200, 200), dtype=np.uint8)
    cv2.rectangle(fill, (60, 60), (140, 140), 255, thickness=-1)
    edge = np.zeros((200, 200), dtype=np.uint8)
    cv2.rectangle(edge, (60, 60), (140, 140), 255, thickness=1)
    return CopperProjection(fill_mask=fill, edge_mask=edge)


def _observer(
    camera: FakeCamera, max_offset_mm: float | None = None
) -> CopperPadObserver:
    """実 detector / matcher と固定投影で CopperPadObserver を組み立てる."""
    return CopperPadObserver(
        camera=camera,
        edge_detector=CopperEdgeDetector(),
        matcher=CopperEdgeMatcher(pixel_per_mm=PPM),
        projection=_projection(),
        roi=(40, 40, 160, 160),
        max_offset_mm=max_offset_mm,
    )


class TestCopperPadObserver:
    """CopperPadObserver の observe() 契約のテスト."""

    def test_observe_returns_translation_matching_known_shift(self):
        """既知ずれ (+6,−4)px の合成画像 → observe() の並進 ≈ (0.6,−0.4)mm.

        observe() -> Transform（想定→観測）。ROI 中心=画像中心なので apply(Point2d(0,0))
        が並進 d.mm に一致する。
        """
        camera = FakeCamera([_board_image(6, -4)])
        observer = _observer(camera)
        assert observer.last_match is None  # observe 前は照合結果なし

        transform = observer.observe()

        shift = transform.apply(Point2d(0.0, 0.0))
        assert shift.x == pytest.approx(0.6, abs=0.2)
        assert shift.y == pytest.approx(-0.4, abs=0.2)
        match = observer.last_match
        assert isinstance(match, RigidEdgeMatch)
        assert match.offset.px.x == pytest.approx(6.0, abs=2.0)
        assert match.offset.px.y == pytest.approx(-4.0, abs=2.0)

    def test_observe_raises_runtime_error_when_nothing_detected(self):
        """真っ黒な画像（エッジなし）では照合に失敗し RuntimeError."""
        camera = FakeCamera([Image(np.zeros((200, 200, 3), dtype=np.uint8))])
        observer = _observer(camera)

        with pytest.raises(RuntimeError):
            observer.observe()

    def test_observe_rejects_offset_beyond_max_offset(self):
        """照合ずれが max_offset_mm を超えると誤マッチとして RuntimeError."""
        # +15px = 1.5mm > 上限 1.0mm
        camera = FakeCamera([_board_image(15, 0)])
        observer = _observer(camera, max_offset_mm=1.0)

        with pytest.raises(RuntimeError, match="超過"):
            observer.observe()

        assert observer.last_match is None  # 棄却された照合は保持しない

    def test_observe_accepts_offset_within_max_offset(self):
        """上限内のずれは通常どおり照合される."""
        camera = FakeCamera([_board_image(6, -4)])  # 0.72mm < 1.0mm
        observer = _observer(camera, max_offset_mm=1.0)

        transform = observer.observe()

        shift = transform.apply(Point2d(0.0, 0.0))
        assert shift.x == pytest.approx(0.6, abs=0.2)


def _dummy_match() -> RigidEdgeMatch:
    """表示用フィールドを埋めるだけの照合結果."""
    return RigidEdgeMatch(
        offset=Offset(px=Point2d(0.0, 0.0), pixel_per_mm=PPM),
        rotation=Rotation(0.0),
        center_mm=Point2d(0.0, 0.0),
        mean_distance_px=0.0,
    )


def _result(machine_transform: Transform, anchor: Point2d) -> PadAlignmentResult:
    """machine_transform と anchor のみ可変の PadAlignmentResult を作る."""
    return PadAlignmentResult(
        machine_transform=machine_transform,
        match=_dummy_match(),
        anchor=anchor,
        adjusted_position=anchor,
        roi=(0, 0, 10, 10),
    )


class TestPadAlignmentResult:
    """PadAlignmentResult の導出プロパティのテスト."""

    def test_translation_of_pure_shift_is_the_shift(self):
        """純並進 M=Shift(d) → translation = d（anchor に依らない）."""
        result = _result(Shift(0.3, -0.2), anchor=Point2d(12.0, 34.0))

        translation = result.translation

        assert translation.x == pytest.approx(0.3, abs=1e-9)
        assert translation.y == pytest.approx(-0.2, abs=1e-9)

    def test_translation_is_anchor_displacement_under_rotation(self):
        """Translation = M(anchor) − anchor（anchor 回り回転+並進の M）.

        M = anchor 回りの回転 30° + (0.5, 0) の並進 → anchor の変位は (0.5, 0)。
        """
        anchor = Point2d(12.0, 34.0)
        machine = Compose(
            [
                Shift(-anchor.x, -anchor.y),
                Rotation(30.0),
                Shift(anchor.x + 0.5, anchor.y),
            ]
        )
        result = _result(machine, anchor=anchor)

        translation = result.translation

        assert translation.x == pytest.approx(0.5, abs=1e-9)
        assert translation.y == pytest.approx(0.0, abs=1e-9)

    def test_rotation_recovers_machine_angle(self):
        """Rotation = from_points(ex, M(anchor+ex) − M(anchor)) → 回転角 30°."""
        anchor = Point2d(12.0, 34.0)
        machine = Compose(
            [
                Shift(-anchor.x, -anchor.y),
                Rotation(30.0),
                Shift(anchor.x + 0.5, anchor.y),
            ]
        )
        result = _result(machine, anchor=anchor)

        assert result.rotation.degrees == pytest.approx(30.0, abs=1e-9)

    def test_rotation_flips_sign_under_mirror_transform(self):
        """鏡映を含む M（det<0）でも from_points 導出で自動処理される.

        M = Rotation(10) → flip_y: ex の像は (cos10, −sin10) → 角度 −10°。
        """
        machine = Compose([Rotation(10.0), Scale.flip(y=True)])
        result = _result(machine, anchor=Point2d(2.0, 3.0))

        assert result.rotation.degrees == pytest.approx(-10.0, abs=1e-9)


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


def _pad(
    designator: str,
    x: float,
    y: float,
    half: float = 0.4,
    copper_half: float | None = None,
) -> Pad:
    """中心 (x, y) の正方形 pad を作る（copper_half 指定時は実銅箔も別サイズ設定）."""
    kwargs = {}
    if copper_half is not None:
        kwargs["copper_polygon"] = _square(x, y, copper_half)
    return Pad(
        designator=designator,
        pad_number="1",
        net_name="NET",
        layer=Layer.TOP,
        polygon=_square(x, y, half),
        **kwargs,
    )


class TestPlanPadRegions:
    """plan_pad_regionsのテスト（計画書「設計（確定）」「凍結する公開 IF」節）."""

    def test_single_pad_creates_one_region(self):
        """単一padはそのpadだけを含む1領域になる."""
        pad = _pad("R1", 2.0, 2.0)

        regions = plan_pad_regions([pad], region_size=(10.0, 10.0))

        assert len(regions) == 1
        assert regions[0].key == (0, 0)
        assert regions[0].pads == (pad,)

    def test_nearby_pads_in_same_cell_are_merged_into_one_region(self):
        """region_size内で近接するpadは同一領域へ併合される（冗長照合の解消）."""
        pad_a = _pad("R1", 2.0, 2.0)
        pad_b = _pad("R2", 3.0, 3.0)

        regions = plan_pad_regions([pad_a, pad_b], region_size=(10.0, 10.0))

        assert len(regions) == 1
        assert {p.designator for p in regions[0].pads} == {"R1", "R2"}

    def test_pads_in_different_cells_are_kept_separate_and_sorted_by_key(self):
        """離れたpadは別領域keyとなり、(col,row)昇順で返る."""
        pads = [
            _pad("C", 25.0, 5.0),  # key (2, 0)
            _pad("A", 5.0, 5.0),  # key (0, 0)
            _pad("B", 15.0, 5.0),  # key (1, 0)
        ]

        regions = plan_pad_regions(pads, region_size=(10.0, 10.0))

        assert [r.key for r in regions] == [(0, 0), (1, 0), (2, 0)]

    def test_grid_is_anchored_at_board_origin_not_at_pad_bounding_box(self):
        """グリッドはboard原点(0,0)固定 — 他padの有無でkeyが動かない.

        Min-bboxベースの相対グリッドだった場合、遠方padの追加でこのpadの
        keyが変わってしまう。原点固定であればkeyは不変。
        """
        target = _pad("F1", 25.0, 5.0)
        region_size = (10.0, 10.0)

        alone = plan_pad_regions([target], region_size)
        with_far_neighbor = plan_pad_regions(
            [target, _pad("Z9", 205.0, 305.0)], region_size
        )

        key_alone = next(r.key for r in alone if target in r.pads)
        key_with_neighbor = next(r.key for r in with_far_neighbor if target in r.pads)
        assert key_alone == key_with_neighbor == (2, 0)

    def test_giant_pad_is_reassigned_to_cell_with_max_boundary_intersection(self):
        """銅箔が中心セルを完全に覆う巨大padは、輪郭と最大長で交差するセルへ再割当てされる.

        Region_size 10mm、pad中心(5,5)はfloor割当でセル(0,0)になるが、
        copper_polygon（半辺7mmの正方形 = x:[-2,12], y:[-2,12]）は
        セル(0,0)を全周マージン2mmで包含し、輪郭が(0,0)を通らない （交差長0 =
        サーマルパッドのケース）。輪郭とセル矩形の交差長は 上下左右の隣接セル (0,-1)/(-1,0)/(1,0)/(0,1)
        が同値10mmで最大となり、 タイブレーク (col,row) 昇順で最小の (-1,0) が選ばれる。
        """
        pad = _pad("TH1", 5.0, 5.0, half=0.4, copper_half=7.0)

        regions = plan_pad_regions([pad], region_size=(10.0, 10.0))

        assert len(regions) == 1
        assert regions[0].key == (-1, 0)
        assert regions[0].pads == (pad,)

    def test_label_and_designators_are_derived_from_key_and_pads(self):
        """Labelは'C{col}R{row}'（計画書の例 "C3R5"）、designatorsは重複除去・ソート済み."""
        pads = [
            _pad("R2", 35.0, 55.0),
            _pad("R1", 36.0, 56.0),
            _pad("R2", 37.0, 57.0, half=0.2),  # 同一designatorの別pad（重複除去対象）
        ]

        regions = plan_pad_regions(pads, region_size=(10.0, 10.0))

        assert len(regions) == 1
        region = regions[0]
        assert region.key == (3, 5)
        assert region.label == "C3R5"
        assert region.designators == ("R1", "R2")

    def test_empty_pads_returns_empty_list(self):
        """padが空なら領域も空リスト."""
        assert plan_pad_regions([], region_size=(10.0, 10.0)) == []
