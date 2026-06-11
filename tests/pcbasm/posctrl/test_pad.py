"""Posctrl/pad の仕様テスト.

計画書 memory/agents/implementation-planner/pad-alignment.md「posctrl/pad.py」に
基づく。CopperPadObserver は capture → detect_edges → match_rigid(roi) →
camera_transform の observer 契約（observe() -> Transform、カメラ mm・画像中心
原点・想定→観測）。PadAlignmentResult は machine_transform から表示用の並進・
回転を導出する。PadAligner の収束ループ自体は XYPositionAdjustor のテストと
実機検証でカバーする（計画書テスト計画）。

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
from pcbasm.posctrl.pad import CopperPadObserver, PadAlignmentResult, cluster_pads
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


def _pad(designator: str, x: float, y: float, half: float = 0.4) -> Pad:
    """中心 (x, y) の正方形padを作る."""
    return Pad(
        designator=designator,
        pad_number="1",
        net_name="NET",
        layer=Layer.TOP,
        polygon=shapely.Polygon(
            [
                (x - half, y - half),
                (x + half, y - half),
                (x + half, y + half),
                (x - half, y + half),
            ]
        ),
    )


class TestClusterPads:
    """cluster_padsのテスト."""

    def test_separates_distant_clusters(self):
        """閾値より離れたpadのまとまりは別グループになる."""
        pads = [
            _pad("A1", 0.0, 0.0),
            _pad("A2", 1.0, 0.0),
            _pad("B1", 20.0, 0.0),
            _pad("B2", 21.0, 0.0),
        ]

        groups = cluster_pads(pads, cluster_distance_mm=2.0, pads_per_representative=10)

        members = {frozenset(p.designator for p in g.pads) for g in groups}
        assert members == {frozenset({"A1", "A2"}), frozenset({"B1", "B2"})}

    def test_chained_pads_form_single_cluster(self):
        """推移的な近傍連結（A-B近い, B-C近い, A-C遠い）は1クラスタになる."""
        # 隣接間の隙間 1.2mm (< 2.0)、両端間の隙間 4.2mm (> 2.0)
        pads = [
            _pad("A", 0.0, 0.0),
            _pad("B", 2.5, 0.0),
            _pad("C", 5.0, 0.0),
        ]

        groups = cluster_pads(pads, cluster_distance_mm=2.0, pads_per_representative=10)

        assert len(groups) == 1
        assert {p.designator for p in groups[0].pads} == {"A", "B", "C"}

    def test_distance_is_polygon_gap_not_center_distance(self):
        """距離はpadポリゴンの隙間で測る（中心間距離ではない）."""
        # 中心間 6mm だが、大きいpad(half=5)と小pad(half=0.4)の隙間は 0.6mm
        pads = [
            _pad("BIG", 0.0, 0.0, half=5.0),
            _pad("SMALL", 6.0, 0.0),
        ]

        groups = cluster_pads(pads, cluster_distance_mm=1.0, pads_per_representative=10)

        assert len(groups) == 1

    def test_representative_is_nearest_to_centroid(self):
        """代表はグループ内padの重心に最も近いpad."""
        # 重心は (4, 4) 付近 → R2 (5, 5) が最も近い
        pads = [
            _pad("R1", 1.0, 1.0),
            _pad("R2", 5.0, 5.0),
            _pad("R3", 6.0, 6.0),
        ]

        groups = cluster_pads(
            pads, cluster_distance_mm=10.0, pads_per_representative=10
        )

        assert len(groups) == 1
        assert groups[0].representative.designator == "R2"

    def test_pads_are_ordered_by_distance_to_centroid(self):
        """グループ内のpadは重心に近い順（照合フォールバックの試行順）."""
        # 重心 = (11/3, 11/3) ≈ (3.67, 3.67)
        # 距離: (4,4)→0.47 < (6,6)→3.30 < (1,1)→3.77
        pads = [
            _pad("FAR", 1.0, 1.0),
            _pad("NEAR", 4.0, 4.0),
            _pad("MID", 6.0, 6.0),
        ]

        groups = cluster_pads(
            pads, cluster_distance_mm=10.0, pads_per_representative=10
        )

        assert [p.designator for p in groups[0].pads] == ["NEAR", "MID", "FAR"]

    def test_large_cluster_splits_into_multiple_representatives(self):
        """要素数がpads_per_representativeを超えるクラスタは複数グループに分割される."""
        # 一直線に密集した8 pad、代表1つあたり4 pad → 2グループ
        pads = [_pad(f"P{i}", float(i), 0.0) for i in range(8)]

        groups = cluster_pads(pads, cluster_distance_mm=1.0, pads_per_representative=4)

        assert len(groups) == 2
        assert sorted(len(g.pads) for g in groups) == [4, 4]
        # 分割は空間的（左半分・右半分）
        members = sorted(tuple(sorted(p.designator for p in g.pads)) for g in groups)
        assert members == [
            ("P0", "P1", "P2", "P3"),
            ("P4", "P5", "P6", "P7"),
        ]

    def test_empty_pads_returns_empty(self):
        assert (
            cluster_pads([], cluster_distance_mm=1.0, pads_per_representative=10) == []
        )

    def test_non_positive_distance_raises(self):
        with pytest.raises(ValueError, match="正の値"):
            cluster_pads(
                [_pad("R1", 1.0, 1.0)],
                cluster_distance_mm=0.0,
                pads_per_representative=10,
            )

    def test_pads_per_representative_below_one_raises(self):
        with pytest.raises(ValueError, match="1以上"):
            cluster_pads(
                [_pad("R1", 1.0, 1.0)],
                cluster_distance_mm=1.0,
                pads_per_representative=0,
            )
