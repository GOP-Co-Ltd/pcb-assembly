"""Posctrl/alignment の仕様テスト.

計画書 memory/agents/implementation-planner/pad-alignment-reuse.md「Branch 2」に
基づく。

ComponentAlignments は board_transform と部品ごとの照合結果から補正済み変換を
導出する純粋なコンテナ:

- corrected_board_transform(d) = Compose([board_transform, machine_transform])
  （board_tour 実機検証済みの順序）
- board_correction(d) = T_b⁻¹ ∘ M ∘ T_b（board 座標系での共役補正）。
  ピン: 任意の board 点 b で T_b(C(b)) == M(T_b(b))

PadAlignmentSession は BoardCalibrationResult から照合の配線
（CopperProjector / CopperEdgeMatcher / CopperEdgeDetector / PadAligner）を
集約する。align() は照合失敗（RuntimeError）を漏らさず None を返し、
corrected_projector(M) は Compose([board_transform, M]) ベースの投影を返す。

カメラは tests/helpers.py の FakeCamera（自前 HAL Camera の test Impl）、
klipper / stage は自前 HAL のため mocker.Mock（test_position.py のイディオム）、
calibration は実 CalibrationResult、machine は実 Machine
（data/testing/config/machine.toml）、pcb は components / pads / copper を
返す Mock を使う。合成矩形画像のイディオムは test_pad.py を踏襲する。
"""

from collections.abc import Callable
from datetime import datetime

import cv2
import numpy as np
import pytest
import shapely
from pytest_mock import MockerFixture

from pcbasm import gcode
from pcbasm.config import Machine
from pcbasm.geometry import (
    Compose,
    Identity,
    Point2d,
    Point3d,
    Rotation,
    Shift,
    Transform,
)
from pcbasm.pcb import Component, Copper, CopperList, Layer, Pad
from pcbasm.posctrl.alignment import (
    ComponentAlignments,
    PadAlignmentSession,
    sorted_top_component_pads,
)
from pcbasm.posctrl.copper import CopperProjector, RigidEdgeMatch
from pcbasm.posctrl.pad import ComponentPads, PadAlignmentResult
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
    copper_half: float | None = None,
) -> Pad:
    """中心 (x, y) の正方形 pad を作る（copper_half 指定時は実銅箔も設定）."""
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


def _target(designator: str, x: float, y: float) -> ComponentPads:
    return ComponentPads(
        component=_component(designator, x, y),
        pads=(_pad(designator, x, y),),
    )


class TestComponentAlignments:
    """ComponentAlignments の lookup と補正変換導出のテスト（純粋）."""

    @staticmethod
    def _build(machine_transform: Transform) -> tuple[Transform, ComponentAlignments]:
        """非自明な board_transform と R1 の照合結果を持つ ComponentAlignments."""
        board_transform = Compose([Rotation(30.0), Shift(10.0, 5.0)])
        anchor = board_transform.apply(Point2d(1.0, 2.0))
        alignments = ComponentAlignments(
            board_transform=board_transform,
            results=((_target("R1", 1.0, 2.0), _result(machine_transform, anchor)),),
        )
        return board_transform, alignments

    def test_result_of_returns_result_of_the_designator(self):
        """登録済み designator ごとに対応する照合結果が返る."""
        r1_result = _result(Shift(0.3, -0.2), anchor=Point2d(1.0, 2.0))
        u1_result = _result(Shift(-0.1, 0.4), anchor=Point2d(8.0, 3.0))
        alignments = ComponentAlignments(
            board_transform=Identity(),
            results=(
                (_target("R1", 1.0, 2.0), r1_result),
                (_target("U1", 8.0, 3.0), u1_result),
            ),
        )

        assert alignments.result_of("R1") is r1_result
        assert alignments.result_of("U1") is u1_result

    def test_unregistered_designator_returns_none(self):
        """未登録 designator は全 lookup API で None."""
        _, alignments = self._build(Shift(0.3, -0.2))

        assert alignments.result_of("C9") is None
        assert alignments.corrected_board_transform("C9") is None
        assert alignments.board_correction("C9") is None

    def test_corrected_board_transform_composes_board_then_machine(self):
        """corrected_board_transform = Compose([board_transform,
        machine_transform]).

        任意の board 点で Compose の適用結果と一致する（board_tour 実機検証済み の合成順序: T_b
        を先に適用し M を後に適用）。
        """
        machine = Compose([Rotation(2.0), Shift(0.3, -0.2)])
        board_transform, alignments = self._build(machine)

        corrected = alignments.corrected_board_transform("R1")

        assert corrected is not None
        expected = Compose([board_transform, machine])
        for point in [Point2d(0.0, 0.0), Point2d(1.0, 2.0), Point2d(-3.5, 7.25)]:
            got = corrected.apply(point)
            want = expected.apply(point)
            assert got.x == pytest.approx(want.x)
            assert got.y == pytest.approx(want.y)

    def test_board_correction_is_conjugation_of_machine_transform(self):
        """共役ピン: C = T_b⁻¹∘M∘T_b ⇔ 任意の board 点 b で T_b(C(b)) == M(T_b(b)).

        board 座標系の補正 C を board_transform で機械座標へ写すと、機械座標系の 補正 M
        と一致する。回転+並進の非自明な T_b と M で符号・順序を固定する。
        """
        machine = Compose([Rotation(2.0), Shift(0.3, -0.2)])
        board_transform, alignments = self._build(machine)

        correction = alignments.board_correction("R1")

        assert correction is not None
        for b in [Point2d(0.0, 0.0), Point2d(1.0, 2.0), Point2d(10.5, -4.25)]:
            lhs = board_transform.apply(correction.apply(b))
            rhs = machine.apply(board_transform.apply(b))
            assert lhs.x == pytest.approx(rhs.x)
            assert lhs.y == pytest.approx(rhs.y)


def _board_image(shift_x: int = 0, shift_y: int = 0) -> Image:
    """黒地に白矩形 (600,320)-(680,400) を指定 px ずらして描いた合成画像.

    銅箔 ±4mm 角（board 原点中心）、board/offset 変換 Identity、stage が anchor (0,0)
    のとき、投影公式 pixel = center + ppm*(s − b) で銅箔は (600,320)-(680,400) px
    に投影される。実 CopperEdgeDetector の Canny で 矩形境界がエッジ化される入力。
    """
    frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    cv2.rectangle(
        frame,
        (600 + shift_x, 320 + shift_y),
        (680 + shift_x, 400 + shift_y),
        (255, 255, 255),
        thickness=-1,
    )
    return Image(frame)


def _machine_config() -> Machine:
    return Machine(TESTING_CONFIG_DIR / "machine.toml")


def _calibration() -> CalibrationResult:
    return CalibrationResult(
        pixel_per_mm=PPM,
        square_size_mm=1.0,
        mean_distance_px=PPM,
        std_distance_px=0.0,
        resolution=(WIDTH, HEIGHT),
        crop_size=(600, 600),
        calibrated_at=datetime.now(),
        z_position=5.0,
    )


class TestPadAlignmentSession:
    """PadAlignmentSession の from_calibration 配線と align/None 契約のテスト."""

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

    @pytest.fixture
    def pcb(self, mocker: MockerFixture):
        """TOP 層に board 原点中心 ±4mm の銅箔島を持つ PcbFile の Mock.

        pad の paste 開口は ±0.5mm、実銅箔 copper_polygon は ±4mm。ROI が
        copper_polygon ベースであれば矩形境界（±4mm）を覆い照合に成功する （paste 開口ベースだと ROI
        内に想定エッジが無く照合できない）。
        """
        pcb = mocker.Mock()
        pcb.components = [_component("R1", 0.0, 0.0)]
        pcb.pads = [_pad("R1", 0.0, 0.0, half=0.5, copper_half=4.0)]
        pcb.copper = CopperList(
            [
                Copper(layer=Layer.TOP, polygon=_square(0.0, 0.0, 4.0)),
                Copper(layer=Layer.BOTTOM, polygon=_square(30.0, 30.0, 4.0)),
            ]
        )
        return pcb

    @staticmethod
    def _session(
        camera: FakeCamera,
        klipper,
        stage,
        pcb,
        board_transform: Transform | None = None,
        frame_sink: Callable[[Image], None] | None = None,
    ) -> PadAlignmentSession:
        result = BoardCalibrationResult(
            machine=_machine_config(),
            klipper=klipper,
            stage=stage,
            camera=camera,
            calibration=_calibration(),
            offset_transform=Identity(),
            board_transform=(
                board_transform if board_transform is not None else Identity()
            ),
            pcb=pcb,
        )
        return PadAlignmentSession.from_calibration(result, frame_sink=frame_sink)

    def test_align_returns_result_with_translation_matching_known_shift(
        self, klipper, stage, pcb
    ):
        """既知ずれの合成画像列で align が成功し translation ≈ (−0.2, +0.2) mm.

        Board が機械座標で (−0.2, +0.2) mm 変位したシナリオ: 1 枚目は anchor (0,0)
        での観測（画像上 (+2,−2)px のずれ）、2 枚目は補正移動 (−0.2, +0.2)
        後の観測（想定どおり）。machine_transform は設計→観測の ずれ Shift(−0.2, +0.2)
        に収束し、translation がそれと整合する。
        """
        camera = FakeCamera([_board_image(2, -2), _board_image()])
        session = self._session(camera, klipper, stage, pcb)
        target = ComponentPads(component=pcb.components[0], pads=tuple(pcb.pads))

        result = session.align(target)

        assert result is not None
        assert result.translation.x == pytest.approx(-0.2, abs=0.1)
        assert result.translation.y == pytest.approx(0.2, abs=0.1)

    def test_align_delivers_edge_match_frames_to_frame_sink(self, klipper, stage, pcb):
        """frame_sink 指定時、align() 中に照合状況の合成フレームが届く.

        Phase 4（webui-phase4.md §1）: window_name 全廃。照合の観測ごとに
        render_edge_match の合成画像が frame_sink へ流れる（webui は ctx.frame
        を渡してプレビューへ配信する）。
        """
        frames: list[Image] = []
        camera = FakeCamera([_board_image(2, -2), _board_image()])
        session = self._session(camera, klipper, stage, pcb, frame_sink=frames.append)
        target = ComponentPads(component=pcb.components[0], pads=tuple(pcb.pads))

        result = session.align(target)

        assert result is not None
        assert len(frames) >= 1  # 観測（observe）1 回につき 1 枚
        assert frames[0].size == (WIDTH, HEIGHT)

    def test_align_returns_none_when_matching_fails(self, klipper, stage, pcb):
        """真っ黒な画像（エッジなし）では照合失敗を漏らさず None を返す."""
        camera = FakeCamera([Image(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))])
        session = self._session(camera, klipper, stage, pcb)
        target = ComponentPads(component=pcb.components[0], pads=tuple(pcb.pads))

        result = session.align(target)

        assert result is None

    def test_corrected_projector_projects_with_composed_board_transform(
        self, klipper, stage, pcb
    ):
        """corrected_projector(M) の pixel_of は Compose([board_transform, M])
        投影.

        非可換な board_transform=Shift と M=Rotation の組で合成順序 （T_b を先、M
        を後）を固定する。
        """
        board_transform = Shift(5.0, -1.0)
        machine = Rotation(90.0)
        camera = FakeCamera([_board_image()])
        session = self._session(
            camera, klipper, stage, pcb, board_transform=board_transform
        )

        corrected = session.corrected_projector(machine)

        reference = CopperProjector(
            polygons=[],
            board_transform=Compose([board_transform, machine]),
            offset_transform=Identity(),
            pixel_per_mm=PPM,
            image_size=(WIDTH, HEIGHT),
        )
        for board_point, stage_xy in [
            (Point2d(0.0, 0.0), Point2d(0.0, 0.0)),
            (Point2d(1.0, 0.5), Point2d(3.0, 2.0)),
        ]:
            got = corrected.pixel_of(board_point, stage_xy)
            want = reference.pixel_of(board_point, stage_xy)
            assert got.x == pytest.approx(want.x)
            assert got.y == pytest.approx(want.y)


class TestSortedTopComponentPads:
    """sorted_top_component_pads のTOP層フィルタと巡回順のテスト."""

    def test_top層のpadを部品ごとに現在位置からの巡回順で返す(
        self, mocker: MockerFixture
    ):
        """BOTTOM層とpadなし部品を除外し、現在位置から近い順に並ぶ."""
        pcb = mocker.Mock()
        pcb.components = [
            _component("FAR", 30.0, 0.0),
            _component("NEAR", 1.0, 0.0),
            _component("NOPAD", 2.0, 0.0),
            Component(
                designator="B1",
                value="10k",
                package="0402",
                position=Point2d(0.5, 0.0),
                rotation=0.0,
                layer=Layer.BOTTOM,
            ),
        ]
        pcb.pads = [
            _pad("FAR", 30.0, 0.0),
            _pad("NEAR", 1.0, 0.0),
            Pad(
                designator="B1",
                pad_number="1",
                net_name="NET",
                layer=Layer.BOTTOM,
                polygon=_square(0.5, 0.0, 0.4),
            ),
        ]
        stage = mocker.Mock()
        stage.get_position.return_value = Point3d(0.0, 0.0, 5.0)
        result = BoardCalibrationResult(
            machine=_machine_config(),
            klipper=mocker.Mock(),
            stage=stage,
            camera=mocker.Mock(),
            calibration=_calibration(),
            offset_transform=Identity(),
            board_transform=Identity(),
            pcb=pcb,
        )

        groups = sorted_top_component_pads(result)

        assert [g.component.designator for g in groups] == ["NEAR", "FAR"]
        assert [p.designator for g in groups for p in g.pads] == ["NEAR", "FAR"]
