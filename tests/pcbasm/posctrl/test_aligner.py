"""Posctrl/aligner の仕様テスト.

計画書 memory/agents/implementation-planner/region-alignment-average.md
「公開インターフェース → src/pcbasm/posctrl/aligner.py」に基づく。

RegionAligner は領域のアンカーへ移動し、**1 回の撮像**で並進ずれを測る
（収束ループを持たない）。照合は並進のみなので machine_transform も純並進に
なり、補正量はアンカーからの距離（レバー腕）に依存してはならない。

カメラは tests/helpers.py の FakeCamera（自前 HAL Camera の test Impl）、
klipper / stage は自前 HAL のため mocker.Mock、エッジ検出・照合・投影は実物を使う。
"""

import cv2
import numpy as np
import pytest
from pytest_mock import MockerFixture
from shapely import Polygon

from pcbasm import gcode
from pcbasm.geometry import (
    Compose,
    Identity,
    Point2d,
    Point3d,
    Rotation,
    Scale,
    Shift,
    Transform,
)
from pcbasm.posctrl import (
    AlignmentRegion,
    CopperEdgeMatcher,
    CopperProjector,
    EdgeMatch,
    RegionAligner,
    RegionAlignment,
    centered_roi,
)
from pcbasm.posctrl.correction import to_machine_transform
from pcbasm.vision import CopperEdgeDetector, Image, Offset
from tests.helpers import FakeCamera

PPM = 10.0  # pixel/mm
IMAGE_SIZE = (400, 400)
REGION_PX = 200
ANCHOR = Point2d(30.0, 20.0)  # 機械座標 [mm]（board_transform = Identity）
# 銅箔 ±5mm 角を anchor で投影すると画像中心 ±50px = (150,150)-(250,250)
COPPER_HALF_MM = 5.0


def _copper() -> Polygon:
    """Anchor 直下に置いた ±5mm 角の銅箔（両方向に拘束がある）."""
    x, y = ANCHOR.x, ANCHOR.y
    return Polygon(
        [
            (x - COPPER_HALF_MM, y - COPPER_HALF_MM),
            (x + COPPER_HALF_MM, y - COPPER_HALF_MM),
            (x + COPPER_HALF_MM, y + COPPER_HALF_MM),
            (x - COPPER_HALF_MM, y + COPPER_HALF_MM),
        ]
    )


def _board_image(shift_x: int = 0, shift_y: int = 0) -> Image:
    """黒地に白矩形 (150,150)-(250,250) を指定 px ずらして描いた合成画像.

    実 CopperEdgeDetector の Canny で矩形境界がエッジ化される入力。
    """
    frame = np.zeros((IMAGE_SIZE[1], IMAGE_SIZE[0], 3), dtype=np.uint8)
    cv2.rectangle(
        frame,
        (150 + shift_x, 150 + shift_y),
        (250 + shift_x, 250 + shift_y),
        (255, 255, 255),
        thickness=-1,
    )
    return Image(frame)


def _region(anchor: Point2d = ANCHOR, index: int = 0) -> AlignmentRegion:
    """画像中心 ROI を持つ照合領域."""
    return AlignmentRegion(
        index=index,
        anchor=anchor,
        roi=centered_roi(IMAGE_SIZE, REGION_PX),
        constraint=120.0,
        edge_point_count=240,
    )


class TestRegionAlignerMeasure:
    """RegionAligner.measure の 1 ショット計測契約."""

    @pytest.fixture
    def klipper(self, mocker: MockerFixture):
        return mocker.Mock()

    @pytest.fixture
    def stage(self, mocker: MockerFixture):
        """Move() の指令位置を get_position() が追跡する stage の Mock."""
        stage = mocker.Mock()
        stage.max_velocity = 100.0
        state = {"position": Point3d(0.0, 0.0, 5.0)}

        def move(**kwargs) -> gcode.GCode:
            current = state["position"]
            state["position"] = Point3d(
                kwargs.get("x", current.x),
                kwargs.get("y", current.y),
                kwargs.get("z", current.z),
            )
            return gcode.GCode("G1")

        stage.move.side_effect = move
        stage.get_position.side_effect = lambda: state["position"]
        return stage

    @staticmethod
    def _aligner(
        camera: FakeCamera,
        klipper,
        stage,
        max_correction_mm: float | None = 1.0,
        frame_sink=None,
    ) -> RegionAligner:
        projector = CopperProjector(
            polygons=[_copper()],
            board_transform=Identity(),
            offset_transform=Identity(),
            pixel_per_mm=PPM,
            image_size=IMAGE_SIZE,
        )
        return RegionAligner(
            camera=camera,
            klipper=klipper,
            stage=stage,
            projector=projector,
            matcher=CopperEdgeMatcher(pixel_per_mm=PPM),
            edge_detector=CopperEdgeDetector(),
            offset_transform=Identity(),
            max_correction_mm=max_correction_mm,
            frame_sink=frame_sink,
        )

    def test_measure_moves_to_the_anchor_and_captures_once(self, klipper, stage):
        """アンカーへ移動し、撮像は 1 回だけ（収束ループを持たない）."""
        camera = FakeCamera([_board_image(6, -4)])
        aligner = self._aligner(camera, klipper, stage)

        aligner.measure(_region())

        assert camera.capture_count == 1
        move_kwargs = stage.move.call_args.kwargs
        assert move_kwargs["x"] == pytest.approx(ANCHOR.x)
        assert move_kwargs["y"] == pytest.approx(ANCHOR.y)
        assert klipper.send_gcode.call_count == 1

    def test_measure_reports_translation_matching_the_known_shift(self, klipper, stage):
        """観測が +6,−4 px ずれた画像 → translation ≈ (−0.6, +0.4) mm.

        符号: 観測が画像上で +x へずれる ＝ 基板が機械座標で −x にある。
        """
        camera = FakeCamera([_board_image(6, -4)])
        aligner = self._aligner(camera, klipper, stage)

        alignment = aligner.measure(_region())

        assert isinstance(alignment, RegionAlignment)
        assert alignment.region.index == 0
        assert alignment.translation.x == pytest.approx(-0.6, abs=0.1)
        assert alignment.translation.y == pytest.approx(0.4, abs=0.1)

    def test_measure_reports_match_quality(self, klipper, stage):
        """EdgeMatch の rms_distance_px / sharpness がそのまま結果に載る.

        webui のログが毎領域「dx/dy/rms/sharpness」を出すための供給元。
        """
        camera = FakeCamera([_board_image(6, -4)])
        aligner = self._aligner(camera, klipper, stage)

        alignment = aligner.measure(_region())

        assert alignment.match.sharpness > 0.5  # ±5mm 角の銅箔は等方
        assert alignment.match.rms_distance_px >= 0.0

    def test_measure_delivers_one_frame_to_frame_sink(self, klipper, stage):
        """frame_sink 指定時、照合状況の合成フレームが 1 枚届く（1 ショット）."""
        frames: list[Image] = []
        camera = FakeCamera([_board_image(6, -4)])
        aligner = self._aligner(camera, klipper, stage, frame_sink=frames.append)

        aligner.measure(_region())

        assert len(frames) == 1
        assert frames[0].size == IMAGE_SIZE

    def test_measure_raises_when_matching_fails(self, klipper, stage):
        """真っ黒な画像（観測エッジなし）では照合に失敗し RuntimeError."""
        camera = FakeCamera(
            [Image(np.zeros((IMAGE_SIZE[1], IMAGE_SIZE[0], 3), dtype=np.uint8))]
        )
        aligner = self._aligner(camera, klipper, stage)

        with pytest.raises(RuntimeError, match="照合"):
            aligner.measure(_region())

    def test_measure_raises_when_offset_exceeds_max_correction(self, klipper, stage):
        """照合ずれが max_correction_mm を超えると誤マッチとして RuntimeError.

        +15px = 1.5mm > 上限 1.0mm。探索窓（2.0mm）内なので照合自体は成立する。
        """
        camera = FakeCamera([_board_image(15, 0)])
        aligner = self._aligner(camera, klipper, stage, max_correction_mm=1.0)

        with pytest.raises(RuntimeError, match="超過"):
            aligner.measure(_region())

    def test_measure_accepts_offset_within_max_correction(self, klipper, stage):
        """上限内のずれは通常どおり計測される."""
        camera = FakeCamera([_board_image(6, -4)])  # 0.72mm < 1.0mm
        aligner = self._aligner(camera, klipper, stage, max_correction_mm=1.0)

        alignment = aligner.measure(_region())

        assert alignment.translation.x == pytest.approx(-0.6, abs=0.1)


def _dummy_match(offset_px: Point2d = Point2d(0.0, 0.0)) -> EdgeMatch:
    """指定 px ずれの照合結果（既定はずれなし）."""
    return EdgeMatch(
        offset=Offset(px=offset_px, pixel_per_mm=PPM),
        rms_distance_px=0.5,
        sharpness=0.7,
    )


def _alignment(machine_transform: Transform, anchor: Point2d) -> RegionAlignment:
    """machine_transform と anchor のみ可変の RegionAlignment を作る."""
    return RegionAlignment(
        region=_region(anchor=anchor),
        match=_dummy_match(),
        machine_transform=machine_transform,
    )


class TestRegionAlignmentTranslation:
    """RegionAlignment.translation の導出."""

    def test_translation_of_pure_shift_is_the_shift(self):
        """純並進 M=Shift(d) → translation = d（anchor に依らない）."""
        alignment = _alignment(Shift(0.3, -0.2), anchor=Point2d(12.0, 34.0))

        assert alignment.translation.x == pytest.approx(0.3, abs=1e-9)
        assert alignment.translation.y == pytest.approx(-0.2, abs=1e-9)

    def test_translation_is_the_anchor_displacement(self):
        """Translation = M(anchor) − anchor（anchor 回りの回転+並進の M）."""
        anchor = Point2d(12.0, 34.0)
        machine = Compose(
            [
                Shift(-anchor.x, -anchor.y),
                Rotation(30.0),
                Shift(anchor.x + 0.5, anchor.y),
            ]
        )

        alignment = _alignment(machine, anchor=anchor)

        assert alignment.translation.x == pytest.approx(0.5, abs=1e-9)
        assert alignment.translation.y == pytest.approx(0.0, abs=1e-9)


class TestRegionAlignmentIsPureTranslation:
    """照合から作る machine_transform が純並進であることのピン（MR !149 の移植）.

    θ が乗ると M(p) = Q(p − anchor) + … となり、補正量が |p − anchor| に比例して 増える（θ=2°
    でレバー腕 1mm あたり 35µm、10mm 部品で 0.35mm）。これが board_tour
    で部品ごとにバラバラなずれが出た原因。照合を並進のみにした後は 共役 M も純並進でなければならず、「基板全体で 1
    つの平均並進」という補正 モデル自体がこの性質に依存している。
    """

    @staticmethod
    def _machine_transform(offset_transform: Transform) -> tuple[Transform, Point2d]:
        """実計測と同じ経路（EdgeMatch → to_machine_transform）で M を作る."""
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

    def test_translation_applies_equally_to_every_board_point(self):
        """RegionAlignment.translation が基板上のどの点にも同じだけ効く.

        領域ごとの補正を平均して全 pad へ一律に適用できる根拠。
        """
        machine_transform, anchor = self._machine_transform(Rotation(30.0))
        alignment = _alignment(machine_transform, anchor=anchor)

        far = anchor + Point2d(40.0, -25.0)
        displacement = machine_transform.apply(far) - far

        assert displacement.x == pytest.approx(alignment.translation.x, abs=1e-9)
        assert displacement.y == pytest.approx(alignment.translation.y, abs=1e-9)
