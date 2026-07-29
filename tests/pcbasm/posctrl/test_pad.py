"""Posctrl/pad の仕様テスト.

計画書 memory/agents/implementation-planner/pad-alignment.md「posctrl/pad.py」に
基づく。CopperPadObserver は capture → detect_edges → match(roi) →
camera_transform の observer 契約（observe() -> Transform、カメラ mm・画像中心
原点・想定→観測）。PadAlignmentResult は machine_transform から表示用の並進を
導出する。PadAligner の収束ループ自体は XYPositionAdjustor のテストと
実機検証でカバーする（計画書テスト計画）。

θ 撤去（memory/agents/spec-test-author/pad-align-drop-theta.md）により照合は
並進のみになり、camera_transform は Shift、PadAlignmentResult.rotation は廃止された。

カメラは tests/helpers.py の FakeCamera（自前 HAL Camera の test Impl）、
エッジ検出は実 CopperEdgeDetector（Canny）を使う。
"""

import cv2
import numpy as np
import pytest
import shapely

from pcbasm.geometry import Compose, Point2d, Rotation, Scale, Shift, Transform
from pcbasm.pcb import Component, Layer, Pad
from pcbasm.posctrl.copper import CopperEdgeMatcher, CopperProjection, EdgeMatch
from pcbasm.posctrl.correction import to_machine_transform
from pcbasm.posctrl.pad import (
    CopperPadObserver,
    PadAlignmentResult,
    group_pads_by_component,
)
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
        assert isinstance(match, EdgeMatch)
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


def _dummy_match(offset_px: Point2d = Point2d(0.0, 0.0)) -> EdgeMatch:
    """指定 px ずれの照合結果（既定はずれなし）."""
    return EdgeMatch(
        offset=Offset(px=offset_px, pixel_per_mm=PPM),
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


class TestPadAlignmentIsPureTranslation:
    """照合結果から作る machine_transform が純並進であることのピン.

    θ が乗ると M(p) = Q(p − anchor) + … となり、補正量が |p − anchor| に比例して 増える（θ=2°
    でレバー腕 1mm あたり 35µm、10mm 部品で 0.35mm）。これが board_tour で
    部品ごとにバラバラなずれが出た原因。θ 撤去後は camera_transform が Shift になり、 共役 M
    も純並進でなければならない。
    """

    @staticmethod
    def _machine_transform(offset_transform: Transform) -> tuple[Transform, Point2d]:
        """実照合と同じ経路（EdgeMatch → to_machine_transform）で M を作る."""
        anchor = Point2d(120.0, 85.0)
        match = _dummy_match(Point2d(12.0, -8.0))
        machine_transform = to_machine_transform(
            match.camera_transform,
            offset_transform,
            projection_anchor=anchor,
            observed_at=Point2d(120.3, 84.6),
        )
        return machine_transform, anchor

    @pytest.mark.parametrize(
        "offset_transform",
        [
            Rotation(0.0),
            Rotation(30.0),
            Compose([Rotation(90.0), Shift(1.0, -2.0)]),
            # 下向きカメラは画像 y が機械 Y と逆向きになり det<0 になり得る
            Compose([Rotation(30.0), Scale.flip(y=True)]),
        ],
    )
    def test_displacement_is_independent_of_lever_arm(
        self, offset_transform: Transform
    ):
        """アンカーから 10mm 離れた 2 点の変位ベクトルが一致する（レバー腕ゼロ）."""
        machine_transform, anchor = self._machine_transform(offset_transform)

        far_a = anchor + Point2d(10.0, 0.0)
        far_b = anchor + Point2d(-6.0, 8.0)  # anchor から 10mm、別方向
        displacement_at_anchor = machine_transform.apply(anchor) - anchor
        displacement_a = machine_transform.apply(far_a) - far_a
        displacement_b = machine_transform.apply(far_b) - far_b

        assert displacement_a.x == pytest.approx(displacement_at_anchor.x, abs=1e-9)
        assert displacement_a.y == pytest.approx(displacement_at_anchor.y, abs=1e-9)
        assert displacement_b.x == pytest.approx(displacement_at_anchor.x, abs=1e-9)
        assert displacement_b.y == pytest.approx(displacement_at_anchor.y, abs=1e-9)

    def test_result_translation_applies_to_every_pad_of_the_component(self):
        """PadAlignmentResult.translation が部品内のどの pad にも同じだけ効く.

        translation は anchor（フットプリント原点）での変位。純並進なら 10mm 離れた pad
        の補正量もこれと一致する。
        """
        machine_transform, anchor = self._machine_transform(Rotation(30.0))
        result = _result(machine_transform, anchor=anchor)

        pad_far = anchor + Point2d(10.0, 0.0)
        displacement = machine_transform.apply(pad_far) - pad_far

        assert displacement.x == pytest.approx(result.translation.x, abs=1e-9)
        assert displacement.y == pytest.approx(result.translation.y, abs=1e-9)


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


class TestGroupPadsByComponent:
    """group_pads_by_componentのテスト."""

    @staticmethod
    def _component(designator: str, x: float, y: float) -> Component:
        return Component(
            designator=designator,
            value="10k",
            package="0402",
            position=Point2d(x, y),
            rotation=0.0,
            layer=Layer.TOP,
        )

    def test_groups_pads_by_designator(self):
        """padはdesignatorで部品に対応付けられる."""
        components = [
            self._component("R1", 1.0, 1.0),
            self._component("U1", 10.0, 10.0),
        ]
        pads = [
            _pad("R1", 0.5, 1.0),
            _pad("R1", 1.5, 1.0),
            _pad("U1", 10.0, 10.0),
        ]

        groups = group_pads_by_component(components, pads)

        by_designator = {g.component.designator: g for g in groups}
        assert len(by_designator["R1"].pads) == 2
        assert len(by_designator["U1"].pads) == 1

    def test_component_without_pads_is_excluded(self):
        """padを持たない部品はグループに含まれない."""
        components = [
            self._component("R1", 1.0, 1.0),
            self._component("J9", 50.0, 50.0),
        ]
        pads = [_pad("R1", 1.0, 1.0)]

        groups = group_pads_by_component(components, pads)

        assert [g.component.designator for g in groups] == ["R1"]

    def test_pad_without_component_is_excluded(self):
        """対応する部品がないpadはどのグループにも入らない."""
        components = [self._component("R1", 1.0, 1.0)]
        pads = [_pad("R1", 1.0, 1.0), _pad("ORPHAN", 5.0, 5.0)]

        groups = group_pads_by_component(components, pads)

        assert len(groups) == 1
        assert {p.designator for p in groups[0].pads} == {"R1"}

    def test_preserves_component_order(self):
        """グループはcomponentsの順序を保つ."""
        components = [
            self._component("U2", 5.0, 5.0),
            self._component("R1", 1.0, 1.0),
        ]
        pads = [_pad("R1", 1.0, 1.0), _pad("U2", 5.0, 5.0)]

        groups = group_pads_by_component(components, pads)

        assert [g.component.designator for g in groups] == ["U2", "R1"]
