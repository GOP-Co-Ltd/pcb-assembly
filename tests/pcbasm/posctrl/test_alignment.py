"""Posctrl/alignment の仕様テスト.

計画書 memory/agents/implementation-planner/region-alignment-average.md
「公開インターフェース → src/pcbasm/posctrl/alignment.py」に基づく。

BoardAlignment は複数領域の計測から基板全体の**単一の平均並進**を導く純粋な
コンテナ。領域ごとの補正を pad ごとに使い分けることはしない（平均で直らない
誤差は spread に現れ、そこで初めて剛体フィットを検討する）。

RegionAlignmentSession は BoardCalibrationResult から照合の配線
（CopperProjector / CopperEdgeMatcher / CopperEdgeDetector / RegionAligner）を
集約する。measure() は照合失敗（RuntimeError）を漏らさず None を返し、
corrected_projector(M) は Compose([board_transform, M]) ベースの投影を返す。

カメラは tests/helpers.py の FakeCamera（自前 HAL Camera の test Impl）、
klipper / stage は自前 HAL のため mocker.Mock、calibration は実
CalibrationResult、machine は実 Machine（data/testing/config/machine.toml）、
pcb は components / pads / copper を返す Mock を使う。
"""

import logging
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
from pcbasm.pcb import Component, Copper, CopperList, Layer, Outline, Pad
from pcbasm.posctrl.aligner import RegionAlignment
from pcbasm.posctrl.alignment import BoardAlignment, RegionAlignmentSession
from pcbasm.posctrl.copper import CopperProjector, EdgeMatch, centered_roi
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


def _region(index: int, anchor: Point2d) -> AlignmentRegion:
    return AlignmentRegion(
        index=index,
        anchor=anchor,
        roi=(0, 0, 100, 100),
        constraint=120.0,
        edge_point_count=240,
    )


def _alignment(index: int, anchor: Point2d, translation: Point2d) -> RegionAlignment:
    """指定の並進を返す領域計測結果（純並進の machine_transform）."""
    return RegionAlignment(
        region=_region(index, anchor),
        match=_match(),
        machine_transform=Shift.from_point(translation),
    )


class TestBoardAlignment:
    """BoardAlignment の平均・ばらつき・空検証（純粋）."""

    def test_translation_is_the_mean_of_region_translations(self):
        """平均並進は各領域の並進の算術平均（領域の位置には依らない）."""
        alignment = BoardAlignment(
            results=(
                _alignment(0, Point2d(10.0, 10.0), Point2d(0.10, -0.20)),
                _alignment(1, Point2d(60.0, 10.0), Point2d(0.20, -0.40)),
                _alignment(2, Point2d(60.0, 40.0), Point2d(0.30, -0.30)),
            )
        )

        assert alignment.translation.x == pytest.approx(0.20)
        assert alignment.translation.y == pytest.approx(-0.30)

    def test_machine_transform_is_a_pure_shift_by_the_mean(self):
        """machine_transform = Shift(translation)（どの点も同じだけ動く）."""
        alignment = BoardAlignment(
            results=(
                _alignment(0, Point2d(10.0, 10.0), Point2d(0.10, -0.20)),
                _alignment(1, Point2d(60.0, 40.0), Point2d(0.30, -0.40)),
            )
        )

        transform = alignment.machine_transform

        translation = alignment.translation
        for point in (Point2d(0.0, 0.0), Point2d(80.0, -25.0)):
            moved = transform.apply(point)
            assert moved.x == pytest.approx(point.x + translation.x)
            assert moved.y == pytest.approx(point.y + translation.y)

    def test_spread_is_the_population_standard_deviation(self):
        """Spread は領域間の母標準偏差（ddof=0）.

        平均並進で直らない誤差（board キャリブレーションの回転・スケール、
        基板の反り）はここに出る。実機診断ログの根拠なので定義を固定する。
        """
        alignment = BoardAlignment(
            results=(
                _alignment(0, Point2d(10.0, 10.0), Point2d(0.10, -0.20)),
                _alignment(1, Point2d(60.0, 40.0), Point2d(0.30, -0.40)),
            )
        )

        assert alignment.spread.x == pytest.approx(0.10)
        assert alignment.spread.y == pytest.approx(0.10)

    def test_single_region_has_zero_spread(self):
        """1 領域なら ばらつきは (0, 0)."""
        alignment = BoardAlignment(
            results=(_alignment(0, Point2d(10.0, 10.0), Point2d(0.10, -0.20)),)
        )

        assert alignment.spread.x == pytest.approx(0.0)
        assert alignment.spread.y == pytest.approx(0.0)

    def test_empty_results_raise_value_error(self):
        """成功領域が 0 件の BoardAlignment は作れない（平均が定義できない）."""
        with pytest.raises(ValueError):
            BoardAlignment(results=())


def _board_image(shift_x: int = 0, shift_y: int = 0) -> Image:
    """黒地に白矩形 (600,320)-(680,400) を指定 px ずらして描いた合成画像.

    銅箔 ±4mm 角（board 原点中心）、board/offset 変換 Identity、stage が anchor (0,0)
    のとき、投影公式 pixel = center + ppm*(s − b) で銅箔は (600,320)-(680,400) px
    に投影される（画像中心 400px ROI の内側）。
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


class TestRegionAlignmentSession:
    """RegionAlignmentSession の配線・領域計画・計測失敗の握りつぶし."""

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

        BOTTOM 層にも銅箔と pad を置き、TOP だけが照合対象になることを見る。 外形は board 原点中心 ±35mm
        角。外周マージン 2mm を引いた ±33mm に 400px (= 40mm) の ROI が収まる格子点は原点 1
        点だけになる。
        """
        pcb = mocker.Mock()
        pcb.components = [_component("R1", 0.0, 0.0)]
        pcb.pads = [
            _pad("R1", 0.0, 0.0, half=0.5),
            _pad("B1", 300.0, 300.0, half=0.5, layer=Layer.BOTTOM),
        ]
        pcb.outline = Outline(_square(0.0, 0.0, 35.0))
        pcb.copper = CopperList(
            [
                Copper(layer=Layer.TOP, polygon=_square(0.0, 0.0, 4.0)),
                Copper(layer=Layer.BOTTOM, polygon=_square(300.0, 300.0, 4.0)),
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
        resolution: tuple[int, int] = (WIDTH, HEIGHT),
    ) -> RegionAlignmentSession:
        result = BoardCalibrationResult(
            machine=_machine_config(),
            klipper=klipper,
            stage=stage,
            camera=camera,
            calibration=_calibration(resolution),
            offset_transform=Identity(),
            board_transform=(
                board_transform if board_transform is not None else Identity()
            ),
            pcb=pcb,
        )
        return RegionAlignmentSession.from_calibration(result, frame_sink=frame_sink)

    def test_region_roi_is_the_image_centered_square(self, klipper, stage, pcb):
        """region_roi = centered_roi(キャリブレーション解像度, region_size_px).

        overlay の描画範囲と照合 ROI が同一であることの根拠。
        """
        session = self._session(FakeCamera([_board_image()]), klipper, stage, pcb)

        size = _machine_config().paste_dispenser.pad_align.region_size_px
        assert session.region_roi == centered_roi((WIDTH, HEIGHT), size)

    def test_region_plus_search_window_must_fit_in_the_resolution(
        self, klipper, stage, pcb
    ):
        """region_size_px + 2*window_px が解像度に収まらなければ構築時 ValueError.

        収まらない設定では探索窓が切り詰められ、ずれの計測範囲が黙って 狭くなる。設定ミスを実行時ではなく配線時に落とす。
        """
        with pytest.raises(ValueError, match="region_size_px"):
            self._session(
                FakeCamera([_board_image()]),
                klipper,
                stage,
                pcb,
                resolution=(400, 300),
            )

    def test_plan_regions_uses_the_board_outline_and_the_shared_roi(
        self, klipper, stage, pcb
    ):
        """基板外形の内側から領域を計画し、roi は region_roi と同一.

        BOTTOM 層の銅箔（300mm 離れた位置）は外形の外なので候補にならない。
        """
        session = self._session(FakeCamera([_board_image()]), klipper, stage, pcb)

        regions = session.plan_regions()

        assert len(regions) == 1  # ROI が収まる格子点は原点 1 点だけ
        region = regions[0]
        assert region.index == 0
        assert region.roi == session.region_roi
        assert region.anchor.x == pytest.approx(0.0, abs=1e-6)
        assert region.anchor.y == pytest.approx(0.0, abs=1e-6)
        assert region.constraint > 0.0

    def test_plan_regions_shrinks_the_outline_by_board_edge_margin(
        self, klipper, stage, pcb
    ):
        """照合領域は outline.buffer(-board_edge_margin) の内側からしか選ばない.

        外形 ±30mm 角なら ROI (400px = 40mm) が収まる格子点は ±10mm に出るが、 既定マージン 2mm
        を引いた ±28mm 角には 1 つも残らないので領域 0 個。
        外形をそのまま使っていれば領域が選ばれてしまうので、マージンが実際に
        効いていることのピンになる（外周部でマッチしないというユーザー要求）。
        """
        pcb.outline = Outline(_square(0.0, 0.0, 30.0))
        session = self._session(FakeCamera([_board_image()]), klipper, stage, pcb)

        assert session.plan_regions() == []

    def test_measure_returns_alignment_for_a_known_shift(self, klipper, stage, pcb):
        """既知ずれ (+2,−2)px の観測 → translation ≈ (−0.2, +0.2) mm.

        1 ショットなので補正移動後の再撮像は行わない。
        """
        camera = FakeCamera([_board_image(2, -2)])
        session = self._session(camera, klipper, stage, pcb)
        region = session.plan_regions()[0]

        alignment = session.measure(region)

        assert alignment is not None
        assert alignment.translation.x == pytest.approx(-0.2, abs=0.1)
        assert alignment.translation.y == pytest.approx(0.2, abs=0.1)
        assert camera.capture_count == 1

    def test_measure_returns_none_and_warns_when_matching_fails(
        self, klipper, stage, pcb, caplog
    ):
        """照合失敗（RuntimeError）は漏らさず警告 log の後 None を返す.

        ループ側（measure_regions）が失敗領域を数えて続行できるようにする。
        """
        camera = FakeCamera([Image(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))])
        session = self._session(camera, klipper, stage, pcb)
        region = session.plan_regions()[0]

        with caplog.at_level(logging.WARNING):
            alignment = session.measure(region)

        assert alignment is None
        assert "照合" in caplog.text

    def test_measure_delivers_edge_match_frames_to_frame_sink(
        self, klipper, stage, pcb
    ):
        """frame_sink 指定時、照合状況の合成フレームが届く（webui プレビュー）."""
        frames: list[Image] = []
        camera = FakeCamera([_board_image(2, -2)])
        session = self._session(camera, klipper, stage, pcb, frame_sink=frames.append)

        session.measure(session.plan_regions()[0])

        assert len(frames) == 1
        assert frames[0].size == (WIDTH, HEIGHT)

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
        session = self._session(
            FakeCamera([_board_image()]),
            klipper,
            stage,
            pcb,
            board_transform=board_transform,
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
