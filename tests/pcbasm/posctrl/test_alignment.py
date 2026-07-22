"""Posctrl/alignment の仕様テスト.

計画書 memory/agents/orchestrator/region-pad-align-plan.md「凍結する公開 IF」
「posctrl/alignment.py」に基づく。

RegionAlignments は board_transform と領域ごとの照合結果から補正済み変換を
導出する純粋なコンテナ:

- result_for(pad) = pad を含む領域の照合結果（Pad の attrs 同値比較で線形探索。
  (designator, pad_number) は KiCAD 上一意でないため不採用）
- board_correction(pad) = T_b⁻¹ ∘ M ∘ T_b（board 座標系での共役補正）。
  ピン: 任意の board 点 b で T_b(C(b)) == M(T_b(b))

PadAlignmentSession は BoardCalibrationResult から照合の配線
（CopperProjector / CopperEdgeMatcher / CopperEdgeDetector / PadAligner）を
集約する。align() は照合失敗（RuntimeError）を漏らさず None を返し、
corrected_projector(M) は Compose([board_transform, M]) ベースの投影を返す。
__init__ 時に machine.camera.crop.size ÷ calibration.pixel_per_mm 由来の
領域サイズが視野に収まるかを検証し、収まらなければ ValueError（設定エラー）。

sorted_top_pad_regions は TOP 層 pad（pads 省略時は result.pcb.pads）から
plan_pad_regions で領域サイズ = crop.size ÷ pixel_per_mm を導出して分割し、
領域中心を board_transform で機械座標化してから現在stage位置基準の巡回順
（nearest neighbor + 2-opt）で返す（旧実装の board/machine 座標混在バグの修正）。

カメラは tests/helpers.py の FakeCamera（自前 HAL Camera の test Impl）、
klipper / stage は自前 HAL のため mocker.Mock（test_position.py のイディオム）、
calibration は実 CalibrationResult、machine は実 Machine
（configs/test-fixture/machine.toml）、pcb は pads / copper を返す Mock を使う。
合成矩形画像のイディオムは test_pad.py を踏襲する。
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
from pcbasm.pcb import Copper, CopperList, Layer, Pad
from pcbasm.posctrl.alignment import (
    PadAlignmentSession,
    RegionAlignments,
    sorted_top_pad_regions,
)
from pcbasm.posctrl.copper import CopperProjector, RigidEdgeMatch
from pcbasm.posctrl.pad import PadAlignmentResult, PadRegion
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


def _region(
    pad: Pad, bounds: tuple[float, float, float, float] = (-6.0, -6.0, 6.0, 6.0)
) -> PadRegion:
    """`pad` を唯一の pad として持つ PadRegion（既定 bounds は中心 (0,0) の 12mm角）."""
    return PadRegion(key=(0, 0), bounds=bounds, pads=(pad,))


class TestRegionAlignments:
    """RegionAlignments の pad lookup と補正変換導出のテスト（純粋）."""

    @staticmethod
    def _build(machine_transform: Transform) -> tuple[Transform, Pad, RegionAlignments]:
        """非自明な board_transform と R1 の照合結果を持つ RegionAlignments."""
        board_transform = Compose([Rotation(30.0), Shift(10.0, 5.0)])
        pad = _pad("R1", 1.0, 2.0)
        anchor = board_transform.apply(Point2d(1.0, 2.0))
        alignments = RegionAlignments(
            board_transform=board_transform,
            results=((_region(pad), _result(machine_transform, anchor)),),
        )
        return board_transform, pad, alignments

    def test_result_for_returns_result_of_the_pad(self):
        """登録済み pad はそれを含む領域の照合結果が返る."""
        r1_pad = _pad("R1", 1.0, 2.0)
        u1_pad = _pad("U1", 8.0, 3.0)
        r1_result = _result(Shift(0.3, -0.2), anchor=Point2d(1.0, 2.0))
        u1_result = _result(Shift(-0.1, 0.4), anchor=Point2d(8.0, 3.0))
        alignments = RegionAlignments(
            board_transform=Identity(),
            results=(
                (_region(r1_pad), r1_result),
                (_region(u1_pad), u1_result),
            ),
        )

        assert alignments.result_for(r1_pad) is r1_result
        assert alignments.result_for(u1_pad) is u1_result

    def test_unregistered_pad_returns_none(self):
        """未登録 pad は全 lookup API で None."""
        _, _, alignments = self._build(Shift(0.3, -0.2))
        orphan = _pad("C9", 50.0, 50.0)

        assert alignments.result_for(orphan) is None
        assert alignments.board_correction(orphan) is None

    def test_result_for_matches_equal_but_distinct_pad_object(self):
        """同一属性値の別オブジェクトのPadでも同一視して照合結果を返す.

        Pasting フローは同一オブジェクトを渡すが、契約としては attrs 同値比較で 照合する（(designator,
        pad_number) は KiCAD 上一意でないため不採用、 という計画書の判断のピン）。
        """
        _, pad, alignments = self._build(Shift(0.3, -0.2))
        pad_copy = _pad("R1", 1.0, 2.0)
        assert pad_copy is not pad
        assert pad_copy == pad

        assert alignments.result_for(pad_copy) is not None
        assert alignments.board_correction(pad_copy) is not None

    def test_board_correction_is_conjugation_of_machine_transform(self):
        """共役ピン: C = T_b⁻¹∘M∘T_b ⇔ 任意の board 点 b で T_b(C(b)) == M(T_b(b)).

        board 座標系の補正 C を board_transform で機械座標へ写すと、機械座標系の 補正 M
        と一致する。回転+並進の非自明な T_b と M で符号・順序を固定する。
        """
        machine = Compose([Rotation(2.0), Shift(0.3, -0.2)])
        board_transform, pad, alignments = self._build(machine)

        correction = alignments.board_correction(pad)

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
    return Machine(PROJECT_ROOT / "configs" / "test-fixture" / "machine.toml")


def _calibration(resolution: tuple[int, int] = (WIDTH, HEIGHT)) -> CalibrationResult:
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

    def test_init_raises_value_error_when_region_size_does_not_fit_field_of_view(
        self, klipper, stage, pcb
    ):
        """Crop÷ppm由来の領域サイズが視野に収まらないと構築時に ValueError.

        領域サイズ×ρ + 2×(roi_margin+search_window) ≤ FOV の収容制約
        （計画書「設計（確定）」節）。test-fixture の crop 600px ÷ ppm 10 = 60mm
        の領域サイズに対し、解像度を意図的に小さくして視野 40mm 四方まで 縮小し、制約を破る（60 + 2*(0.5+1.4) =
        63.8mm > 40mm）。
        """
        tiny_calibration = _calibration(resolution=(400, 400))
        result = BoardCalibrationResult(
            machine=_machine_config(),
            klipper=klipper,
            stage=stage,
            camera=FakeCamera([_board_image()]),
            calibration=tiny_calibration,
            offset_transform=Identity(),
            board_transform=Identity(),
            pcb=pcb,
        )

        with pytest.raises(ValueError):
            PadAlignmentSession.from_calibration(result)

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
        target = _region(pcb.pads[0])

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
        target = _region(pcb.pads[0])

        result = session.align(target)

        assert result is not None
        assert len(frames) >= 1  # 観測（observe）1 回につき 1 枚
        assert frames[0].size == (WIDTH, HEIGHT)

    def test_align_returns_none_when_matching_fails(self, klipper, stage, pcb):
        """真っ黒な画像（エッジなし）では照合失敗を漏らさず None を返す."""
        camera = FakeCamera([Image(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))])
        session = self._session(camera, klipper, stage, pcb)
        target = _region(pcb.pads[0])

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


def _calibration_result(
    pcb, stage, board_transform: Transform, mocker: MockerFixture
) -> BoardCalibrationResult:
    """sorted_top_pad_regions テスト用の
    BoardCalibrationResult（camera/klipperは未使用）."""
    return BoardCalibrationResult(
        machine=_machine_config(),
        klipper=mocker.Mock(),
        stage=stage,
        camera=mocker.Mock(),
        calibration=_calibration(),
        offset_transform=Identity(),
        board_transform=board_transform,
        pcb=pcb,
    )


class TestSortedTopPadRegions:
    """sorted_top_pad_regions のTOP層フィルタ・領域サイズ結線・巡回順のテスト."""

    def test_filters_to_top_layer_pads_when_pads_omitted(self, mocker: MockerFixture):
        """Pads省略時、result.pcb.padsのBOTTOM層は除外されTOP層のみになる."""
        pcb = mocker.Mock()
        pcb.pads = [
            _pad("T1", 1.0, 0.0),
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
        result = _calibration_result(pcb, stage, Identity(), mocker)

        regions = sorted_top_pad_regions(result)

        assert [d for r in regions for d in r.designators] == ["T1"]

    def test_uses_given_pads_argument_instead_of_pcb_pads(self, mocker: MockerFixture):
        """Pads指定時はpcb.padsでなく引数のpadsから領域を構築し、TOP層フィルタは維持される."""
        pcb = mocker.Mock()
        pcb.pads = [_pad("IGNORED", 1.0, 0.0)]  # pads指定時はこちらは使われない
        given_pads = [
            _pad("T1", 1.0, 0.0),
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
        result = _calibration_result(pcb, stage, Identity(), mocker)

        regions = sorted_top_pad_regions(result, given_pads)

        assert [d for r in regions for d in r.designators] == ["T1"]

    def test_region_size_is_derived_from_crop_size_and_pixel_per_mm(
        self, mocker: MockerFixture
    ):
        """領域サイズ = camera.crop.size(600px) ÷ calibration.pixel_per_mm(10) =
        60mm.

        Board x=1mmとx=55mmのpadは同一領域（60mm未満）に併合され、x=65mmのpad
        は別領域（60mm超）に分かれることで、crop÷ppm由来の領域サイズ結線を 確認する。
        """
        pcb = mocker.Mock()
        pcb.pads = [
            _pad("A", 1.0, 0.0),
            _pad("B", 55.0, 0.0),
            _pad("C", 65.0, 0.0),
        ]
        stage = mocker.Mock()
        stage.get_position.return_value = Point3d(0.0, 0.0, 5.0)
        result = _calibration_result(pcb, stage, Identity(), mocker)

        regions = sorted_top_pad_regions(result)

        assert len(regions) == 2
        designator_groups = [set(r.designators) for r in regions]
        assert {"A", "B"} in designator_groups
        assert {"C"} in designator_groups

    def test_cyclic_order_is_based_on_machine_coordinates_not_board_coordinates(
        self, mocker: MockerFixture
    ):
        """巡回順は領域中心をboard_transformで機械座標化してから現在位置と比較する.

        Board座標ではNEAR(1,0)がFAR(65,0)よりも原点に近いが、非自明な
        board_transform（+100mm shift）適用後の機械座標では現在stage位置
        (105,0)からNEARの方が近い（|101-105|=4 < |165-105|=60）。旧実装の
        「board座標のままstage位置（機械座標）と比較する」バグでは逆順 （FAR, NEAR）になる。
        """
        pcb = mocker.Mock()
        pcb.pads = [
            _pad("FAR", 65.0, 0.0),
            _pad("NEAR", 1.0, 0.0),
        ]
        stage = mocker.Mock()
        stage.get_position.return_value = Point3d(105.0, 0.0, 5.0)
        result = _calibration_result(pcb, stage, Shift(100.0, 0.0), mocker)

        regions = sorted_top_pad_regions(result)

        assert [r.designators[0] for r in regions] == ["NEAR", "FAR"]
