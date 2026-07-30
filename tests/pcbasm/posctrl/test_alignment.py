"""Posctrl/alignment の仕様テスト.

計画書 memory/agents/implementation-planner/region-affine-correction.md
「公開インターフェース → src/pcbasm/posctrl/alignment.py」に基づく。

区ごとに測った変位 d_i から基板全体の **6 自由度アフィン**
（並進 + 回転 + スケール + スキュー）を最小二乗で 1 つ求める
（`fit_displacement` → `DisplacementFit`）。平均並進はその特別な場合として
自動的に再現される（純並進の場では L = 0）。

アンカーが 3 点未満、または準共線（最小主軸方向 RMS 広がりが
`_MIN_ANCHOR_SPREAD_MM = 2.0` 未満）なら並進のみへ縮退する。実測では
spread 1.5mm 付近でアフィンが並進に負け、0.16mm では p95 853um まで暴れるため、
縮退は「安全側へ倒す」正しい挙動（計画書「4. 共線縮退の閾値」）。

区ごとの残差 r_i = d_i − d̂(anchor_i) は「アフィンで取り切れなかった分」で、
実機では「残差 RMS ≈ 照合ノイズならアフィンで足りている / 数倍なら非線形な
ひずみが残っている」の判定材料になる（計画書「5. 残差の解釈」）。
残差が観測可能であること自体が本タスクの成果物なので、純アフィン場で ~0、
2 次の場で有意に立つことをテストで固定する。

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
from pcbasm.geometry import Identity, Point2d, Point3d, Transform
from pcbasm.pcb import Component, Copper, CopperList, Layer, Outline, Pad
from pcbasm.posctrl.aligner import RegionAlignment
from pcbasm.posctrl.alignment import (
    BoardAlignment,
    DisplacementFit,
    RegionAlignmentSession,
    fit_displacement,
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


def _region(index: int, anchor: Point2d) -> AlignmentRegion:
    return AlignmentRegion(
        index=index,
        anchor=anchor,
        roi=(0, 0, 100, 100),
        constraint=120.0,
        edge_point_count=240,
    )


def _alignment(
    index: int,
    anchor: Point2d,
    displacement: Point2d,
    *,
    increment: Point2d = Point2d(0.0, 0.0),
    passes: int = 1,
    converged: bool = True,
) -> RegionAlignment:
    """指定の累積変位を持つ領域計測結果."""
    return RegionAlignment(
        region=_region(index, anchor),
        match=_match(),
        displacement=displacement,
        increment=increment,
        passes=passes,
        converged=converged,
    )


def _affine_field(
    anchors: Sequence[Point2d], matrix: np.ndarray, translation: Point2d
) -> list[Point2d]:
    """D(p) = L (p − c) + t をアンカー上で評価した変位列を作る."""
    centroid = _centroid_of(anchors)
    field: list[Point2d] = []
    for anchor in anchors:
        relative = np.array([anchor.x - centroid.x, anchor.y - centroid.y])
        moved = matrix @ relative
        field.append(
            Point2d(float(moved[0]) + translation.x, float(moved[1]) + translation.y)
        )
    return field


def _centroid_of(anchors: Sequence[Point2d]) -> Point2d:
    return Point2d(
        x=float(np.mean([a.x for a in anchors])),
        y=float(np.mean([a.y for a in anchors])),
    )


# 非共線かつ y 方向にも十分広がったアンカー配置（spread >= 2.0mm）
SPREAD_ANCHORS = [
    Point2d(0.0, 0.0),
    Point2d(20.0, 0.0),
    Point2d(0.0, 13.2),
    Point2d(20.0, 13.2),
    Point2d(10.0, 6.6),
]
# 3 点法誤差の残りとして現実的な大きさ（回転 0.02°・スケール 300ppm 相当）
FIELD_MATRIX = np.array([[3.0e-4, -3.5e-4], [3.5e-4, -2.0e-4]])
FIELD_TRANSLATION = Point2d(0.08, -0.05)


class TestFitDisplacementAffine:
    """アフィン変位場の復元（本タスクの存在理由）."""

    def test_recovers_a_known_affine_field(self):
        """既知の L / t で作った変位から、任意の点で d(p) を再現する.

        平均並進では 46um 残っていた（実測）誤差の主因が回転・スケール・スキュー
        なので、この復元が成り立つことが補正精度の根拠になる。
        """
        displacements = _affine_field(SPREAD_ANCHORS, FIELD_MATRIX, FIELD_TRANSLATION)

        fit = fit_displacement(SPREAD_ANCHORS, displacements)

        assert fit.model == "affine"
        centroid = _centroid_of(SPREAD_ANCHORS)
        assert fit.centroid.x == pytest.approx(centroid.x, abs=1e-9)
        assert fit.centroid.y == pytest.approx(centroid.y, abs=1e-9)
        assert fit.translation.x == pytest.approx(FIELD_TRANSLATION.x, abs=1e-9)
        assert fit.translation.y == pytest.approx(FIELD_TRANSLATION.y, abs=1e-9)
        # アンカー集合の外側も含めて変位場そのものを再現する
        for point in (Point2d(0.0, 0.0), Point2d(87.0, -30.0), Point2d(-15.0, 40.0)):
            relative = np.array([point.x - centroid.x, point.y - centroid.y])
            want = FIELD_MATRIX @ relative
            moved = fit.machine_transform.apply(point)
            assert moved.x == pytest.approx(
                point.x + float(want[0]) + FIELD_TRANSLATION.x, abs=1e-9
            )
            assert moved.y == pytest.approx(
                point.y + float(want[1]) + FIELD_TRANSLATION.y, abs=1e-9
            )

    def test_predict_is_the_displacement_of_the_machine_transform(self):
        """Predict(p) = machine_transform.apply(p) − p（式を二重に持たない）."""
        displacements = _affine_field(SPREAD_ANCHORS, FIELD_MATRIX, FIELD_TRANSLATION)

        fit = fit_displacement(SPREAD_ANCHORS, displacements)

        for point in (Point2d(3.0, 4.0), Point2d(-40.0, 55.0)):
            moved = fit.machine_transform.apply(point)
            predicted = fit.predict(point)
            assert predicted.x == pytest.approx(moved.x - point.x, abs=1e-12)
            assert predicted.y == pytest.approx(moved.y - point.y, abs=1e-12)

    def test_pure_translation_field_degenerates_to_the_mean_translation(self):
        """全区が同じ変位なら、どの点も同じだけ動く（従来挙動への退化）.

        アフィンにしたことで純並進の場が壊れないことのピン。
        """
        shift = Point2d(0.12, -0.34)
        displacements = [shift] * len(SPREAD_ANCHORS)

        fit = fit_displacement(SPREAD_ANCHORS, displacements)

        assert fit.translation.x == pytest.approx(shift.x, abs=1e-12)
        assert fit.translation.y == pytest.approx(shift.y, abs=1e-12)
        for point in (Point2d(0.0, 0.0), Point2d(90.0, -60.0)):
            moved = fit.machine_transform.apply(point)
            assert moved.x == pytest.approx(point.x + shift.x, abs=1e-9)
            assert moved.y == pytest.approx(point.y + shift.y, abs=1e-9)

    def test_translation_is_the_mean_of_the_measured_displacements(self):
        """重心での変位 t は d_i の算術平均（Σ(p_i − c) = 0 による分離）."""
        displacements = [
            Point2d(0.10, -0.20),
            Point2d(0.20, -0.40),
            Point2d(0.30, -0.30),
            Point2d(0.40, -0.10),
            Point2d(0.00, 0.00),
        ]

        fit = fit_displacement(SPREAD_ANCHORS, displacements)

        assert fit.translation.x == pytest.approx(0.20, abs=1e-12)
        assert fit.translation.y == pytest.approx(-0.20, abs=1e-12)


class TestFitDisplacementDegeneracy:
    """準共線・少数アンカーでの並進フォールバック."""

    def test_collinear_anchors_fall_back_to_translation(self):
        """Y が全て同じ（spread = 0）なら並進のみ。使ったモデルが結果に現れる.

        アフィンのままだと最小二乗が y 方向のレバー腕を持たず、pad へ外挿した ときに暴れる（実測 spread 0.16mm で
        p95 853um）。
        """
        anchors = [Point2d(x, 7.0) for x in (0.0, 15.0, 30.0, 45.0)]
        displacements = [
            Point2d(0.10, -0.20),
            Point2d(0.20, -0.30),
            Point2d(0.30, -0.40),
            Point2d(0.40, -0.10),
        ]

        fit = fit_displacement(anchors, displacements)

        assert fit.model == "translation"
        assert fit.anchor_spread_mm == pytest.approx(0.0, abs=1e-9)
        mean = Point2d(0.25, -0.25)
        for point in (Point2d(0.0, 0.0), Point2d(80.0, 40.0)):
            moved = fit.machine_transform.apply(point)
            assert moved.x == pytest.approx(point.x + mean.x, abs=1e-9)
            assert moved.y == pytest.approx(point.y + mean.y, abs=1e-9)

    def test_near_collinear_anchors_fall_back_to_translation(self):
        """Spread が 2.0mm 未満（y に ±1mm の 6 点 = 1.0mm）なら並進のみ.

        厳密な共線より準共線のほうが危険（lstsq の最小ノルム解が効かない帯）。
        """
        anchors = [
            Point2d(x, 1.0 if index % 2 == 0 else -1.0)
            for index, x in enumerate((0.0, 10.0, 20.0, 30.0, 40.0, 50.0))
        ]
        displacements = _affine_field(anchors, FIELD_MATRIX, FIELD_TRANSLATION)

        fit = fit_displacement(anchors, displacements)

        assert 0.0 < fit.anchor_spread_mm < 2.0  # 実測 0.96mm（閾値 2.0mm 未満）
        assert fit.model == "translation"

    def test_two_rows_of_anchors_are_enough_for_affine(self):
        """Y が 0 と 13.2mm の 2 行なら spread 6.6mm でアフィンを使う.

        タイル格子ではアンカー間隔が region_mm の整数倍になるので、2 行に 分かれていれば spread は
        region_mm のオーダーになる。
        """
        anchors = [
            Point2d(0.0, 0.0),
            Point2d(20.0, 0.0),
            Point2d(0.0, 13.2),
            Point2d(20.0, 13.2),
        ]
        displacements = _affine_field(anchors, FIELD_MATRIX, FIELD_TRANSLATION)

        fit = fit_displacement(anchors, displacements)

        assert fit.anchor_spread_mm == pytest.approx(6.6, abs=1e-9)
        assert fit.model == "affine"

    @pytest.mark.parametrize("count", [1, 2])
    def test_fewer_than_three_anchors_fall_back_to_translation(self, count: int):
        """アフィンの下限は非共線 3 点なので 1〜2 区では並進のみ."""
        anchors = [Point2d(0.0, 0.0), Point2d(20.0, 13.2)][:count]
        displacements = [Point2d(0.10, -0.20), Point2d(0.30, -0.40)][:count]

        fit = fit_displacement(anchors, displacements)

        assert fit.model == "translation"
        mean = _centroid_of(displacements)
        moved = fit.machine_transform.apply(Point2d(50.0, 50.0))
        assert moved.x == pytest.approx(50.0 + mean.x, abs=1e-9)
        assert moved.y == pytest.approx(50.0 + mean.y, abs=1e-9)


class TestFitDisplacementValidation:
    """引数検証（いずれも ValueError）."""

    def test_empty_anchors_raise(self):
        with pytest.raises(ValueError):
            fit_displacement([], [])

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            fit_displacement(
                [Point2d(0.0, 0.0), Point2d(10.0, 0.0)], [Point2d(0.1, 0.1)]
            )


class TestBoardAlignmentResiduals:
    """区ごとの残差（アフィンで取り切れない分の観測可能性）."""

    @staticmethod
    def _board(displacements: Sequence[Point2d]) -> BoardAlignment:
        return BoardAlignment(
            results=tuple(
                _alignment(index, anchor, displacement)
                for index, (anchor, displacement) in enumerate(
                    zip(SPREAD_ANCHORS, displacements, strict=True)
                )
            )
        )

    def test_fit_is_derived_from_the_region_measurements(self):
        """Fit は (anchor, displacement) の対から当てはめる（results と整合）."""
        displacements = _affine_field(SPREAD_ANCHORS, FIELD_MATRIX, FIELD_TRANSLATION)

        board = self._board(displacements)

        assert isinstance(board.fit, DisplacementFit)
        assert board.model == "affine"
        assert board.translation.x == pytest.approx(FIELD_TRANSLATION.x, abs=1e-9)
        assert board.translation.y == pytest.approx(FIELD_TRANSLATION.y, abs=1e-9)
        moved = board.machine_transform.apply(SPREAD_ANCHORS[0])
        want = board.fit.machine_transform.apply(SPREAD_ANCHORS[0])
        assert moved.x == pytest.approx(want.x, abs=1e-12)
        assert moved.y == pytest.approx(want.y, abs=1e-12)

    def test_pure_affine_field_leaves_no_residual(self):
        """純アフィンな入力では残差が消える（当てはめが正しいことの裏）."""
        displacements = _affine_field(SPREAD_ANCHORS, FIELD_MATRIX, FIELD_TRANSLATION)

        board = self._board(displacements)

        assert len(board.residuals) == len(board.results)
        assert board.residual_rms == pytest.approx(0.0, abs=1e-9)
        assert board.residual_max == pytest.approx(0.0, abs=1e-9)

    def test_nonlinear_field_leaves_a_measurable_residual(self):
        """2 次の変位場（d = (k x², 0)）では残差 RMS が有意に立つ.

        「アフィンで取り切れない歪みが残っているかどうか」をユーザーが実機で 判定する唯一の材料。残差が常に 0
        に潰れる実装では非線形を見逃す。 k = 5e-4 /mm は 20mm 幅で 200um 級の 2 次項 （実測で残差
        16um を出した 50um の 2 次ひずみより大きい）に相当する。
        """
        curvature = 5.0e-4
        displacements = [
            Point2d(curvature * anchor.x**2, 0.0) for anchor in SPREAD_ANCHORS
        ]

        board = self._board(displacements)

        assert board.residual_rms > 5.0e-3  # 5um 超（区あたり照合ノイズと同程度以上）
        assert board.residual_max >= board.residual_rms
        assert len(board.residuals) == len(board.results)
        # 残差は results と同順（区ごとのログ行が領域番号と対応する根拠）
        for result, residual in zip(board.results, board.residuals, strict=True):
            want = result.displacement - board.fit.predict(result.region.anchor)
            assert residual.x == pytest.approx(want.x, abs=1e-12)
            assert residual.y == pytest.approx(want.y, abs=1e-12)

    def test_residual_rms_and_max_agree_with_the_residual_list(self):
        """RMS と最大は residuals のノルムから定義される."""
        curvature = 1.0e-4
        displacements = [
            Point2d(curvature * anchor.x**2, curvature * anchor.y**2)
            for anchor in SPREAD_ANCHORS
        ]

        board = self._board(displacements)

        norms = [r.norm for r in board.residuals]
        assert board.residual_rms == pytest.approx(
            float(np.sqrt(np.mean(np.square(norms)))), abs=1e-12
        )
        assert board.residual_max == pytest.approx(max(norms), abs=1e-12)

    def test_empty_results_raise_value_error(self):
        """成功領域が 0 件の BoardAlignment は作れない（当てはめが定義できない）."""
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
        with pytest.raises(ValueError, match="region_size_px"):
            self._session(
                FakeCamera([_board_image()]),
                klipper,
                stage,
                self._pcb(),
                resolution=(200, 200),
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
        assert region.constraint > 0.0

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
