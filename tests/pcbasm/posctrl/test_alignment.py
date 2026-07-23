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
（configs/test-fixture/machine.toml）、pcb は components / pads / copper を
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
from pcbasm.pcb import Component, Copper, CopperList, Layer, Pad, PcbFile
from pcbasm.posctrl import (
    PadAlignmentCandidates,
    PadAlignments,
    PadAlignmentTarget,
    corrected_top_pad_entries,
    rank_safe_pad_alignment_targets,
)
from pcbasm.posctrl.alignment import (
    ComponentAlignments,
    PadAlignmentSession,
    sorted_top_component_pads,
)
from pcbasm.posctrl.copper import CopperEdgeMatcher, CopperProjector, RigidEdgeMatch
from pcbasm.posctrl.pad import ComponentPads, PadAlignmentResult
from pcbasm.posctrl.setup import BoardCalibrationResult
from pcbasm.vision import CalibrationResult, Image, Offset
from tests.helpers import PROJECT_ROOT, FakeCamera

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


def _pad_target(
    identifier: str,
    designator: str,
    pad_number: str,
    x: float,
    y: float,
    *,
    size: float = 1.0,
    layer: Layer = Layer.TOP,
    copper_polygon: shapely.Polygon | None = None,
) -> PadAlignmentTarget:
    """候補順位テスト用の単一 copper pad target."""
    pad = Pad(
        designator=designator,
        pad_number=pad_number,
        net_name="NET",
        layer=layer,
        polygon=_square(x, y, size / 2.0),
        copper_polygon=(
            copper_polygon if copper_polygon is not None else _square(x, y, size / 2.0)
        ),
    )
    return PadAlignmentTarget(identifier=identifier, pad=pad)


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


class TestPadAlignmentTarget:
    """PadAlignmentTarget は paste 開口でなく実銅箔重心を照合位置にする."""

    def test_position_is_copper_polygon_centroid(self):
        copper = shapely.Polygon([(8.0, 3.0), (12.0, 3.0), (12.0, 5.0), (8.0, 5.0)])
        target = _pad_target(
            "U1.3",
            "U1",
            "3",
            1.0,
            2.0,
            copper_polygon=copper,
        )

        assert target.position == Point2d(10.0, 4.0)


class TestRankSafePadAlignmentTargets:
    """安全距離による候補選別と二段階の決定的な順位."""

    @staticmethod
    def _rank(
        targets: list[PadAlignmentTarget],
        *,
        max_correction_mm: float = 1.0,
        tolerance_mm: float = 0.05,
    ) -> PadAlignmentCandidates:
        return rank_safe_pad_alignment_targets(
            targets,
            max_correction_mm=max_correction_mm,
            tolerance_mm=tolerance_mm,
        )

    @pytest.mark.parametrize(
        ("distance", "expected_identifiers"),
        [
            (2.05, []),
            (2.049, []),
            (2.051, ["R1.1", "R2.1"]),
        ],
    )
    def test_requires_centroid_distance_strictly_greater_than_safety_limit(
        self, distance: float, expected_identifiers: list[str]
    ):
        targets = [
            _pad_target("R1.1", "R1", "1", 0.0, 0.0),
            _pad_target("R2.1", "R2", "1", distance, 0.0),
        ]

        candidates = self._rank(targets)

        assert [target.identifier for target in candidates.targets] == (
            expected_identifiers
        )

    def test_same_centroid_split_pads_are_rejected(self):
        targets = [
            _pad_target("U1.#1", "U1", "1", 0.0, 0.0),
            _pad_target("U1.#2", "U1", "1", 0.0, 0.0),
        ]

        candidates = self._rank(targets)

        assert candidates.targets == ()
        assert candidates.rejected_count == 2

    def test_fine_pitch_array_is_rejected(self):
        targets = [
            _pad_target(f"U1.{number}", "U1", str(number), x, 0.0, size=0.2)
            for number, x in enumerate((0.0, 0.5, 1.0), start=1)
        ]

        candidates = self._rank(targets)

        assert candidates.targets == ()
        assert candidates.preferred_component_count == 0
        assert candidates.rejected_count == 3

    def test_filters_bottom_empty_invalid_and_zero_area_copper(self):
        invalid = shapely.Polygon([(20.0, 0.0), (21.0, 1.0), (20.0, 1.0), (21.0, 0.0)])
        zero_area = shapely.Polygon([(30.0, 0.0), (31.0, 0.0), (32.0, 0.0)])
        targets = [
            _pad_target("R1.1", "R1", "1", 0.0, 0.0),
            _pad_target("R2.1", "R2", "1", 10.0, 0.0, layer=Layer.BOTTOM),
            _pad_target("R3.1", "R3", "1", 20.0, 0.0, copper_polygon=invalid),
            _pad_target(
                "R4.1",
                "R4",
                "1",
                30.0,
                0.0,
                copper_polygon=zero_area,
            ),
            _pad_target(
                "R5.1",
                "R5",
                "1",
                40.0,
                0.0,
                copper_polygon=shapely.Polygon(),
            ),
        ]

        candidates = self._rank(targets)

        assert [target.identifier for target in candidates.targets] == ["R1.1"]
        assert candidates.preferred_component_count == 1
        assert candidates.rejected_count == 4

    def test_places_one_smallest_pad_per_component_before_remaining_pads(self):
        targets = [
            _pad_target("R1.2", "R1", "2", 0.0, 0.0, size=2.0),
            _pad_target("U1.1", "U1", "1", 10.0, 0.0, size=1.5),
            _pad_target("C1.1", "C1", "1", 20.0, 0.0, size=1.75),
            _pad_target("R1.1", "R1", "1", 30.0, 0.0, size=1.0),
            _pad_target("U1.2", "U1", "2", 40.0, 0.0, size=0.5),
        ]

        candidates = self._rank(targets)

        assert candidates.preferred_component_count == 3
        assert [target.identifier for target in candidates.targets] == [
            "U1.2",
            "R1.1",
            "C1.1",
            "U1.1",
            "R1.2",
        ]

    def test_equal_area_ties_use_designator_pad_number_and_centroid(self):
        targets = [
            _pad_target("U1.2-right", "U1", "2", 40.0, 0.0),
            _pad_target("R1.1", "R1", "1", 0.0, 0.0),
            _pad_target("U1.2-left", "U1", "2", 30.0, 0.0),
            _pad_target("U1.1", "U1", "1", 20.0, 0.0),
            _pad_target("C1.1", "C1", "1", 10.0, 0.0),
        ]

        candidates = self._rank(targets)

        assert [target.identifier for target in candidates.targets] == [
            "C1.1",
            "R1.1",
            "U1.1",
            "U1.2-left",
            "U1.2-right",
        ]


class TestPadAlignments:
    """複数 pad の成功結果から共通 XY 補正だけを平均する."""

    @staticmethod
    def _alignment_result(dx: float, dy: float, theta: float) -> PadAlignmentResult:
        anchor = Point2d(0.0, 0.0)
        return _result(
            Compose([Rotation(theta), Shift(dx, dy)]),
            anchor=anchor,
        )

    def test_average_translation_is_arithmetic_mean_of_successful_xy(self):
        targets = (
            _pad_target("R1.1", "R1", "1", 0.0, 0.0),
            _pad_target("U1.1", "U1", "1", 10.0, 0.0),
            _pad_target("C1.1", "C1", "1", 20.0, 0.0),
        )
        alignments = PadAlignments(
            board_transform=Identity(),
            results=(
                (targets[0], self._alignment_result(0.3, -0.2, 10.0)),
                (targets[1], self._alignment_result(-0.1, 0.4, -5.0)),
                (targets[2], self._alignment_result(0.1, 0.1, 2.0)),
            ),
        )

        mean = alignments.average_translation()

        assert mean.x == pytest.approx(0.1)
        assert mean.y == pytest.approx(0.1)

    def test_averaged_machine_transform_ignores_theta(self):
        targets = (
            _pad_target("R1.1", "R1", "1", 0.0, 0.0),
            _pad_target("U1.1", "U1", "1", 10.0, 0.0),
        )
        alignments = PadAlignments(
            board_transform=Identity(),
            results=(
                (targets[0], self._alignment_result(0.4, -0.2, 30.0)),
                (targets[1], self._alignment_result(0.2, 0.2, -20.0)),
            ),
        )

        transform = alignments.averaged_machine_transform()

        origin = transform.apply(Point2d(0.0, 0.0))
        other = transform.apply(Point2d(2.0, -1.0))
        assert origin.x == pytest.approx(0.3)
        assert origin.y == pytest.approx(0.0)
        assert other.x == pytest.approx(2.3)
        assert other.y == pytest.approx(-1.0)

    def test_averaged_board_correction_is_conjugated_machine_shift(self):
        board_transform = Compose([Rotation(30.0), Shift(10.0, 5.0)])
        target = _pad_target("R1.1", "R1", "1", 0.0, 0.0)
        alignments = PadAlignments(
            board_transform=board_transform,
            results=((target, self._alignment_result(0.4, -0.2, 15.0)),),
        )

        correction = alignments.averaged_board_correction()
        machine = Shift(0.4, -0.2)

        for board_point in (
            Point2d(0.0, 0.0),
            Point2d(1.0, 2.0),
            Point2d(-3.5, 7.25),
        ):
            corrected_machine_point = board_transform.apply(
                correction.apply(board_point)
            )
            expected = machine.apply(board_transform.apply(board_point))
            assert corrected_machine_point.x == pytest.approx(expected.x)
            assert corrected_machine_point.y == pytest.approx(expected.y)

    def test_empty_results_are_rejected(self):
        with pytest.raises(ValueError):
            PadAlignments(board_transform=Identity(), results=())


class TestCorrectedTopPadEntries:
    """board_tour は照合対象外を含む全TOP padへ同じ平均補正を適用する."""

    def test_returns_every_top_pad_with_one_common_machine_correction(self):
        pads = [
            _pad("R1", 0.0, 0.0),
            _pad("U1", 10.0, 0.0),
            Pad(
                designator="R2",
                pad_number="1",
                net_name="NET",
                layer=Layer.BOTTOM,
                polygon=_square(20.0, 0.0, 0.4),
            ),
        ]
        board_transform = Compose([Rotation(20.0), Shift(5.0, -3.0)])
        machine_correction = Shift(0.3, -0.2)

        entries = corrected_top_pad_entries(
            pads,
            board_transform=board_transform,
            machine_correction=machine_correction,
            start=Point2d(0.0, 0.0),
        )

        assert {pad.designator for pad, _ in entries} == {"R1", "U1"}
        for pad, target in entries:
            expected = machine_correction.apply(board_transform.apply(pad.center))
            assert target.x == pytest.approx(expected.x)
            assert target.y == pytest.approx(expected.y)


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


def _mask_image(fill_mask: np.ndarray) -> Image:
    """CopperProjectorの塗り潰しmaskを実Cannyへ入力できるBGR画像にする."""
    return Image(cv2.cvtColor(fill_mask, cv2.COLOR_GRAY2BGR))


def _copper_projector(polygons: list[shapely.Polygon]) -> CopperProjector:
    """Identity変換・標準テスト解像度の実CopperProjectorを作る."""
    return CopperProjector(
        polygons=polygons,
        board_transform=Identity(),
        offset_transform=Identity(),
        pixel_per_mm=PPM,
        image_size=(WIDTH, HEIGHT),
    )


def _machine_config() -> Machine:
    return Machine(PROJECT_ROOT / "configs" / "test-fixture" / "machine.toml")


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

    def test_align_pad_matches_single_copper_pad_with_real_opencv(
        self, klipper, stage, pcb
    ):
        """個別 pad API も合成画像を実 detector / matcher へ通して既知XYを復元する."""
        camera = FakeCamera([_board_image(2, -2), _board_image()])
        session = self._session(camera, klipper, stage, pcb)
        target = PadAlignmentTarget(identifier="R1.1", pad=pcb.pads[0])

        result = session.align_pad(target)

        assert result is not None
        assert result.anchor == target.position
        assert result.translation.x == pytest.approx(-0.2, abs=0.1)
        assert result.translation.y == pytest.approx(0.2, abs=0.1)

    def test_align_pad_uses_target_polygon_roi_with_all_copper_in_expected_mask(
        self, klipper, stage
    ):
        """ROIは対象padだけから決まり、範囲内の隣padも想定maskへ残る.

        実KiCad基板のU1.1/U1.2は別々の銅箔で、U1.2の一部がU1.1由来ROIへ
        入る。全TOP銅箔の実投影から合成画像を作り、実detector/matcherで
        align_padを通したうえで、公開されたresult.roiとprojector maskを確認する。
        """
        pcb = PcbFile(
            PROJECT_ROOT / "data" / "testing" / "led_blinker" / "led_blinker.kicad_pcb"
        )
        target_pad = next(
            pad for pad in pcb.pads if pad.designator == "U1" and pad.pad_number == "1"
        )
        neighbor_pad = next(
            pad for pad in pcb.pads if pad.designator == "U1" and pad.pad_number == "2"
        )
        target = PadAlignmentTarget(identifier="U1.1", pad=target_pad)
        top_copper = [
            copper.polygon for copper in pcb.copper if copper.layer == Layer.TOP
        ]
        source_projector = _copper_projector(top_copper)
        camera = FakeCamera(
            [_mask_image(source_projector.project(target.position).fill_mask)]
        )
        session = self._session(camera, klipper, stage, pcb)

        result = session.align_pad(target)

        assert result is not None
        pad_align = _machine_config().paste_dispenser.pad_align
        target_roi = session.projector.roi_of(
            target_pad.copper_polygon,
            target.position,
            margin_mm=pad_align.roi_margin,
            min_size_mm=pad_align.min_roi,
        )
        union_roi = session.projector.roi_of(
            [target_pad.copper_polygon, neighbor_pad.copper_polygon],
            target.position,
            margin_mm=pad_align.roi_margin,
            min_size_mm=pad_align.min_roi,
        )
        assert result.roi == target_roi
        assert result.roi != union_roi

        x0, y0, x1, y1 = result.roi
        full_projection = session.projector.project(target.position)
        target_projection = _copper_projector([target_pad.copper_polygon]).project(
            target.position
        )
        neighbor_projection = _copper_projector([neighbor_pad.copper_polygon]).project(
            target.position
        )
        target_inside_roi = target_projection.fill_mask[y0:y1, x0:x1] > 0
        neighbor_inside_roi = neighbor_projection.fill_mask[y0:y1, x0:x1] > 0
        full_inside_roi = full_projection.fill_mask[y0:y1, x0:x1] > 0

        assert np.any(neighbor_inside_roi)
        assert not np.any(target_inside_roi & neighbor_inside_roi)
        assert np.all(full_inside_roi[neighbor_inside_roi])

    def test_align_pad_rejects_neighbor_match_beyond_max_correction(
        self, klipper, stage
    ):
        """隣padだけを観測して得た最良候補が上限超過ならNoneを返す.

        実KiCad基板のC1.1/C1.2（重心間1.02mm）を使う。C1.1の想定ROIに
        対してC1.2だけを撮像した合成画像を実matcherへ通すと隣pad方向の
        約1mmずれが最良になるが、machine設定の0.3mm上限を超えるため
        PadAlignmentSession.align_padは成功結果にしない。
        """
        pcb = PcbFile(
            PROJECT_ROOT / "data" / "testing" / "led_blinker" / "led_blinker.kicad_pcb"
        )
        target_pad = next(
            pad for pad in pcb.pads if pad.designator == "C1" and pad.pad_number == "1"
        )
        neighbor_pad = next(
            pad for pad in pcb.pads if pad.designator == "C1" and pad.pad_number == "2"
        )
        target = PadAlignmentTarget(identifier="C1.1", pad=target_pad)
        top_copper = [
            copper.polygon for copper in pcb.copper if copper.layer == Layer.TOP
        ]
        full_projector = _copper_projector(top_copper)
        neighbor_projector = _copper_projector([neighbor_pad.copper_polygon])
        observed = _mask_image(neighbor_projector.project(target.position).fill_mask)
        camera = FakeCamera([observed])
        session = self._session(camera, klipper, stage, pcb)
        pad_align = _machine_config().paste_dispenser.pad_align
        roi = full_projector.roi_of(
            target_pad.copper_polygon,
            target.position,
            margin_mm=pad_align.roi_margin,
            min_size_mm=pad_align.min_roi,
        )
        match = CopperEdgeMatcher(
            pixel_per_mm=PPM,
            search_window_mm=pad_align.search_window,
            theta_range_degrees=pad_align.theta_range,
        ).match_rigid(
            session.edge_detector.detect_edges(observed),
            full_projector.project(target.position).edge_mask,
            roi=roi,
        )

        assert match is not None
        assert match.offset.mm.norm == pytest.approx(1.0, abs=0.2)
        assert match.offset.mm.norm > pad_align.max_correction
        assert session.align_pad(target) is None

    def test_align_pad_returns_none_when_matching_fails(self, klipper, stage, pcb):
        camera = FakeCamera([Image(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))])
        session = self._session(camera, klipper, stage, pcb)
        target = PadAlignmentTarget(identifier="R1.1", pad=pcb.pads[0])

        assert session.align_pad(target) is None

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
