"""Posctrl/aligner の仕様テスト.

region-local-correction の仕様「反復計測」に基づく（この反復計測は補正モデルを
アフィンから局所補正へ作り直しても一切変わらない）。

RegionAligner は領域のアンカーへ移動し、最大 ``max_passes`` 回まで反復して
累積変位を測る。**測れる offset はステージ位置に不変**（想定投影と観測が画像内で
同じだけ動く）なので、2 パス目は「累積変位を board 変換の後段へ挿した投影器」で
投影しなければならない。素朴に「補正位置へ移動して再計測」すると同じ変位を
2 回足してしまう（計画書「3. 反復計測」の実測: 真の変位 0.30mm で誤差 1.4um 対
298.2um）。この二重計上を捕まえるのが本ファイルの最重要テスト
（``test_second_pass_does_not_double_count_the_displacement``）。

各パスの補正は純並進なので累積は単純和でよい（``to_machine_transform`` の
展開が p に依存しない）。補正量がアンカーからの距離（レバー腕）に依存しては
ならないことも合わせてピンする。

カメラは tests/helpers.py の FakeCamera（自前 HAL Camera の test Impl）、
klipper / stage は自前 HAL のため手書き stub、エッジ検出・照合・投影は実物を使う。
"""

import inspect

import cv2
import numpy as np
import pytest
from shapely import Polygon

from pcbasm import gcode
from pcbasm.config import PadAlign
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
# 銅箔 ±5mm 角をアンカーで投影すると画像中心 ±50px = (150,150)-(250,250)
COPPER_HALF_MM = 5.0


def _copper(center: Point2d = ANCHOR) -> Polygon:
    """指定の機械座標に置いた ±5mm 角の銅箔（両方向に拘束がある）."""
    x, y = center.x, center.y
    return Polygon(
        [
            (x - COPPER_HALF_MM, y - COPPER_HALF_MM),
            (x + COPPER_HALF_MM, y - COPPER_HALF_MM),
            (x + COPPER_HALF_MM, y + COPPER_HALF_MM),
            (x - COPPER_HALF_MM, y + COPPER_HALF_MM),
        ]
    )


def _board_image(shift_x: int = 0, shift_y: int = 0) -> Image:
    """黒地に白矩形を指定 px ずらして描いた合成画像.

    実 CopperEdgeDetector の Canny で矩形境界がエッジ化される入力。ずれ 0 の画像は
    「そのパスの想定投影と観測が一致している」状態を表す。塗り潰しの右下端を +1px 伸ばすのは、Canny が明側の外周 1px
    を落とすことで生じる −0.5px の系統 ずれを打ち消すため（この補正込みで残る偏りは 0.02px = 2um）。
    """
    frame = np.zeros((IMAGE_SIZE[1], IMAGE_SIZE[0], 3), dtype=np.uint8)
    cv2.rectangle(
        frame,
        (150 + shift_x, 150 + shift_y),
        (251 + shift_x, 251 + shift_y),
        (255, 255, 255),
        thickness=-1,
    )
    return Image(frame)


def _black_image() -> Image:
    """観測エッジが 1 つも無い画像（照合失敗を起こす）."""
    return Image(np.zeros((IMAGE_SIZE[1], IMAGE_SIZE[0], 3), dtype=np.uint8))


def _region(anchor: Point2d = ANCHOR, index: int = 0) -> AlignmentRegion:
    """画像中心 ROI を持つ照合領域（board_transform = Identity なので中心 = anchor）."""
    return AlignmentRegion(
        index=index,
        board_center=anchor,
        anchor=anchor,
        roi=centered_roi(IMAGE_SIZE, REGION_PX),
        constraint=120.0,
        edge_point_count=240,
    )


class _StubKlipper:
    """send_gcode を数えるだけの Klipper 代替（自前 HAL）."""

    def __init__(self) -> None:
        self.sent: list[gcode.GCode] = []

    def send_gcode(self, code: gcode.GCode) -> None:
        self.sent.append(code)


class _StubStage:
    """Move() の指令位置を get_position() が追跡する XYZStage 代替（自前 HAL）.

    ``to_machine_transform`` の ``observed_at`` には「その撮像を撮った実ステージ
    位置」が渡らなければならないので、指令位置の追跡がテストの前提になる。
    """

    max_velocity = 100.0

    def __init__(self) -> None:
        self._position = Point3d(0.0, 0.0, 5.0)
        self.targets: list[Point2d] = []

    def move(self, **kwargs) -> gcode.GCode:
        current = self._position
        self._position = Point3d(
            kwargs.get("x", current.x),
            kwargs.get("y", current.y),
            kwargs.get("z", current.z),
        )
        self.targets.append(Point2d(self._position.x, self._position.y))
        return gcode.GCode("G1")

    def get_position(self) -> Point3d:
        return self._position


class TestRegionAlignerMeasure:
    """RegionAligner.measure の反復計測契約."""

    @pytest.fixture
    def klipper(self) -> _StubKlipper:
        return _StubKlipper()

    @pytest.fixture
    def stage(self) -> _StubStage:
        return _StubStage()

    @staticmethod
    def _aligner(
        camera: FakeCamera,
        klipper: _StubKlipper,
        stage: _StubStage,
        *,
        anchor: Point2d = ANCHOR,
        max_correction_mm: float | None = 1.0,
        max_passes: int = 2,
        converge_tolerance_mm: float = 0.01,
        frame_sink=None,
    ) -> RegionAligner:
        projector = CopperProjector(
            polygons=[_copper(anchor)],
            board_transform=Identity(),
            offset_transform=Identity(),
            pixel_per_mm=PPM,
            image_size=IMAGE_SIZE,
        )
        return RegionAligner(
            camera=camera,
            klipper=klipper,  # type: ignore[arg-type]
            stage=stage,  # type: ignore[arg-type]
            projector=projector,
            matcher=CopperEdgeMatcher(pixel_per_mm=PPM),
            edge_detector=CopperEdgeDetector(),
            offset_transform=Identity(),
            max_correction_mm=max_correction_mm,
            max_passes=max_passes,
            converge_tolerance_mm=converge_tolerance_mm,
            frame_sink=frame_sink,
        )

    def test_converged_first_pass_stops_after_one_capture(self, klipper, stage):
        """観測が想定と一致（変位 0）なら 1 パスで打ち切る.

        反復は「増分が converge_tolerance 以下」で止まる。常に max_passes 回
        撮像する実装では実機時間が倍になる。
        """
        camera = FakeCamera([_board_image()])
        aligner = self._aligner(camera, klipper, stage)

        alignment = aligner.measure(_region())

        assert isinstance(alignment, RegionAlignment)
        assert alignment.passes == 1
        assert alignment.converged is True
        assert alignment.displacement.norm == pytest.approx(0.0, abs=0.02)
        assert camera.capture_count == 1
        assert len(klipper.sent) == 1
        assert stage.targets[0].x == pytest.approx(ANCHOR.x)
        assert stage.targets[0].y == pytest.approx(ANCHOR.y)

    def test_second_pass_does_not_double_count_the_displacement(self, klipper, stage):
        """2 パス計測の累積が**真の変位そのもの**になる（二重計上しない）.

        観測される offset はステージ位置に不変なので、2 パス目を素朴に 「補正位置へ移動して元の投影で再計測」すると同じ
        −6px を再び測り、 累積が 2 倍の (1.2, −0.8) mm になる。正しい実装は累積変位を board 変換の
        後段へ挿して投影するので、2 パス目の観測は想定と一致し増分は 0 になる。

        本タスクで実測した最重要の罠（真の変位 0.30mm で誤差 1.4um 対 298.2um）。
        """
        camera = FakeCamera([_board_image(-6, 4), _board_image()])
        aligner = self._aligner(camera, klipper, stage)

        alignment = aligner.measure(_region())

        assert alignment.displacement.x == pytest.approx(0.6, abs=0.02)
        assert alignment.displacement.y == pytest.approx(-0.4, abs=0.02)
        assert alignment.passes == 2
        assert alignment.converged is True
        assert camera.capture_count == 2
        # 2 パス目は 1 パス目の累積を当てたアンカーへ移動する
        assert stage.targets[1].x == pytest.approx(ANCHOR.x + 0.6, abs=0.02)
        assert stage.targets[1].y == pytest.approx(ANCHOR.y - 0.4, abs=0.02)

    def test_max_passes_one_keeps_the_first_pass_result(self, klipper, stage):
        """max_passes=1 なら 1 パスで終わり、displacement は 1 パス目の値.

        実機で 2 パス目の増分が常に微小なら max_passes=1 に落として計測時間を
        半分にできる、という設計上の逃げ道のピン。
        """
        camera = FakeCamera([_board_image(-6, 4), _board_image()])
        aligner = self._aligner(camera, klipper, stage, max_passes=1)

        alignment = aligner.measure(_region())

        assert alignment.passes == 1
        assert alignment.converged is False
        assert alignment.displacement.x == pytest.approx(0.6, abs=0.02)
        assert alignment.displacement.y == pytest.approx(-0.4, abs=0.02)
        assert camera.capture_count == 1

    def test_unconverged_region_is_still_returned(self, klipper, stage):
        """max_passes を使い切っても収束しない区は converged=False で返す.

        棄却すると min_regions を割ってジョブが止まりやすくなるので採用し、
        判定材料は残差ログとこのフラグに残す（orchestrator 裁定 3）。
        """
        camera = FakeCamera([_board_image(-6, 4), _board_image(-1, 0)])
        aligner = self._aligner(camera, klipper, stage)

        alignment = aligner.measure(_region())

        assert alignment.passes == 2
        assert alignment.converged is False
        assert alignment.displacement.x == pytest.approx(0.7, abs=0.03)
        assert alignment.displacement.y == pytest.approx(-0.4, abs=0.03)

    def test_third_pass_corrects_the_projection_by_the_cumulative_not_the_increment(
        self, klipper, stage
    ):
        """3 パス目も**累積**で投影を補正する（直前の増分だけでは足りない）.

        `max_passes` の検証は「1 以上」だけなので WebUI から 3 以上が到達し得る。
        3 パス目の投影補正に直前の増分（0.1mm）しか使わないと、1 パス目で測った
        0.6mm ぶんが再び offset として現れて二重計上する（累積 1.3mm）。
        2 パスまでは累積 = 増分なのでこの穴は 3 パス目でしか露出しない。

        観測列: 1 パス目 −6px（0.6mm）→ 2 パス目 −1px（0.1mm）→ 3 パス目は
        累積 0.7mm を当てた投影と一致（増分 0）。
        """
        camera = FakeCamera([_board_image(-6, 4), _board_image(-1, 0), _board_image()])
        # 上限判定に頼らず「累積 0.7mm か、二重計上の 1.3mm か」で弁別する
        aligner = self._aligner(
            camera, klipper, stage, max_passes=3, max_correction_mm=None
        )

        alignment = aligner.measure(_region())

        assert alignment.passes == 3
        assert alignment.converged is True
        assert alignment.displacement.x == pytest.approx(0.7, abs=0.03)
        assert alignment.displacement.y == pytest.approx(-0.4, abs=0.03)
        assert alignment.increment.norm == pytest.approx(0.0, abs=0.02)
        assert camera.capture_count == 3
        # 3 パス目の移動先も累積を当てた位置
        assert stage.targets[2].x == pytest.approx(ANCHOR.x + 0.7, abs=0.03)
        assert stage.targets[2].y == pytest.approx(ANCHOR.y - 0.4, abs=0.03)

    def test_converge_tolerance_decides_the_early_exit(self, klipper, stage):
        """増分が converge_tolerance 以下になったパスで打ち切る.

        同じ観測列でも許容 0.01mm では収束せず、0.2mm では 2 パス目 （増分
        0.1mm）で収束扱いになる。閾値が実際に判定に使われていることのピン。
        """
        images = [_board_image(-6, 4), _board_image(-1, 0), _board_image()]
        camera = FakeCamera(images)
        aligner = self._aligner(
            camera, klipper, stage, max_passes=3, converge_tolerance_mm=0.2
        )

        alignment = aligner.measure(_region())

        assert alignment.passes == 2
        assert alignment.converged is True
        assert camera.capture_count == 2

    def test_final_pass_match_quality_and_increment_are_reported(self, klipper, stage):
        """Match と increment は最終パスの値（webui のログと収束の読み取り用）.

        increment はユーザーが「2 パス目が意味を持っているか（= max_passes を 1 に
        落とせるか）」を実機で判断する材料（orchestrator 裁定 3）。
        """
        camera = FakeCamera([_board_image(-6, 4), _board_image()])
        aligner = self._aligner(camera, klipper, stage)

        alignment = aligner.measure(_region())

        assert alignment.region.index == 0
        assert alignment.match.sharpness > 0.5  # ±5mm 角の銅箔は等方
        assert alignment.match.rms_distance_px < 2.0
        # 2 パス目の増分は 0（想定と観測が一致するので二重計上しない）
        assert alignment.increment.norm == pytest.approx(0.0, abs=0.02)

    def test_increment_of_a_single_pass_is_the_whole_displacement(self, klipper, stage):
        """1 パスで終わったときの increment は累積そのもの."""
        camera = FakeCamera([_board_image(-6, 4)])
        aligner = self._aligner(camera, klipper, stage, max_passes=1)

        alignment = aligner.measure(_region())

        assert alignment.increment.x == pytest.approx(
            alignment.displacement.x, abs=1e-9
        )
        assert alignment.increment.y == pytest.approx(
            alignment.displacement.y, abs=1e-9
        )

    def test_delivers_one_frame_per_pass_to_frame_sink(self, klipper, stage):
        """frame_sink 指定時、パスごとに照合状況の合成フレームが 1 枚届く."""
        frames: list[Image] = []
        camera = FakeCamera([_board_image(-6, 4), _board_image()])
        aligner = self._aligner(camera, klipper, stage, frame_sink=frames.append)

        alignment = aligner.measure(_region())

        assert alignment.passes == 2
        assert len(frames) == 2
        assert frames[0].size == IMAGE_SIZE

    def test_raises_when_the_first_pass_fails_to_match(self, klipper, stage):
        """真っ黒な画像（観測エッジなし）では照合に失敗し RuntimeError."""
        aligner = self._aligner(FakeCamera([_black_image()]), klipper, stage)

        with pytest.raises(RuntimeError, match="照合"):
            aligner.measure(_region())

    def test_raises_when_a_later_pass_fails_to_match(self, klipper, stage):
        """2 パス目の照合失敗でも RuntimeError（1 パス目の結果を拾わない）.

        模型と観測が食い違っている状態なので、区ごと落として min_regions に 判定を委ねる。
        """
        camera = FakeCamera([_board_image(-6, 4), _black_image()])
        aligner = self._aligner(camera, klipper, stage)

        with pytest.raises(RuntimeError, match="照合"):
            aligner.measure(_region())

    def test_max_correction_is_checked_against_the_cumulative_displacement(
        self, klipper, stage
    ):
        """上限判定は累積に対して行う（パスごとでは上限が実質 2 倍になる）.

        各パスの増分は 0.72mm で上限 1.0mm の内側だが、累積 1.44mm は超過する。
        """
        camera = FakeCamera([_board_image(-6, 4), _board_image(-6, 4)])
        aligner = self._aligner(camera, klipper, stage, max_correction_mm=1.0)

        with pytest.raises(RuntimeError, match="超過"):
            aligner.measure(_region())

    def test_first_pass_beyond_max_correction_raises(self, klipper, stage):
        """1 パス目で上限を超えるずれは誤マッチとして RuntimeError.

        +15px = 1.5mm > 上限 1.0mm。探索窓（2.0mm）内なので照合自体は成立する。
        """
        camera = FakeCamera([_board_image(15, 0)])
        aligner = self._aligner(camera, klipper, stage, max_correction_mm=1.0)

        with pytest.raises(RuntimeError, match="超過"):
            aligner.measure(_region())

    @pytest.mark.parametrize(
        "anchor", [Point2d(30.0, 20.0), Point2d(120.0, 85.0), Point2d(-40.0, -15.0)]
    )
    def test_displacement_is_independent_of_the_anchor(self, klipper, stage, anchor):
        """同じ観測ずれなら、アンカーがどこでも同じ displacement になる.

        補正がレバー腕に依存しないこと（MR !149 のピンの移植）。区ごとの変位が
        アンカー位置で汚れると、区ごとの局所補正そのものが壊れる。
        """
        camera = FakeCamera([_board_image(-6, 4), _board_image()])
        aligner = self._aligner(camera, klipper, stage, anchor=anchor)

        alignment = aligner.measure(_region(anchor=anchor))

        assert alignment.displacement.x == pytest.approx(0.6, abs=0.02)
        assert alignment.displacement.y == pytest.approx(-0.4, abs=0.02)


class TestLibraryDefaultsMatchTheConfigDefaults:
    """RegionAligner のライブラリ既定が PadAlign の既定と一致すること.

    同じ概念の既定値が `RegionAligner.__init__` と `[paste_dispenser.pad_align]` の
    2 箇所にあり、設定を通さない呼び出し（スクリプト・診断ツール）は前者を使う。
    片方だけ更新すると本番と違う反復回数・収束判定で動き、実機でしか気づけない。
    値そのものではなく **2 つの既定が一致すること** を契約として固定する。
    """

    @pytest.mark.api_contract
    @pytest.mark.parametrize(
        ("parameter", "config_key"),
        [
            ("max_passes", "max_passes"),
            ("converge_tolerance_mm", "converge_tolerance"),
        ],
    )
    def test_default_equals_the_pad_align_default(
        self, parameter: str, config_key: str
    ):
        default = (
            inspect.signature(RegionAligner.__init__).parameters[parameter].default
        )

        assert default == pytest.approx(getattr(PadAlign(), config_key))


def _dummy_match(offset_px: Point2d = Point2d(0.0, 0.0)) -> EdgeMatch:
    """指定 px ずれの照合結果（既定はずれなし）."""
    return EdgeMatch(
        offset=Offset(px=offset_px, pixel_per_mm=PPM),
        rms_distance_px=0.5,
        sharpness=0.7,
    )


class TestMachineTransformIsPureTranslation:
    """照合から作る machine_transform が純並進であることのピン（MR !149 の移植）.

    θ が乗ると M(p) = Q(p − anchor) + … となり、補正量が |p − anchor| に比例して
    増える（θ=2° でレバー腕 1mm あたり 35µm、10mm 部品で 0.35mm）。これが
    board_tour で部品ごとにバラバラなずれが出た原因。

    純並進であることは反復計測の前提でもある: 各パスの増分を単純和で累積できる
    根拠が「M が p に依存しない」ことなので、ここが崩れると
    ``RegionAlignment.displacement`` の定義自体が意味を失う。
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

    def test_successive_passes_compose_as_a_sum_of_shifts(self):
        """2 パスの M を合成した作用が、各パスの並進の和と厳密に一致する.

        累積を ``Point2d`` の加算で持ち回れる根拠。合成が和にならないなら
        ``displacement`` は Transform で持たなければならない。
        """
        first, anchor = self._machine_transform(Rotation(30.0))
        step_one = first.apply(anchor) - anchor
        second_anchor = anchor + step_one
        second = to_machine_transform(
            _dummy_match(Point2d(2.0, -1.0)).camera_transform,
            Rotation(30.0),
            projection_anchor=second_anchor,
            observed_at=second_anchor,
        )
        step_two = second.apply(second_anchor) - second_anchor

        composed = Compose([first, second])

        for point in (Point2d(0.0, 0.0), Point2d(200.0, -50.0)):
            moved = composed.apply(point)
            assert moved.x == pytest.approx(point.x + step_one.x + step_two.x, abs=1e-9)
            assert moved.y == pytest.approx(point.y + step_one.y + step_two.y, abs=1e-9)
