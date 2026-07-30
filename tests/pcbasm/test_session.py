"""`pcbasm.session.PasteSession` の座標変換契約のテスト.

region-local-correction で塗布の座標変換は **pad ごと**になった。
`pad_to_machine(board_point, alignment=..., height_plane=...)` が
「board 座標 → ノズル機械座標」の全変換を 1 箇所で組む。合成順は

    board_transform → 局所補正 → toolhead_offset → height_plane

で、これはドメイン規則そのもの:

- 銅箔照合で測った変位は**カメラの機械座標系**で定義されているので、補正は
  board 変換の**直後**（`toolhead_offset` より前）。board 変換の前に挿す
  （= board 空間へ補正を持ち込む共役適用）と、board 変換の回転ぶんだけ
  補正がねじれる。ユーザーが明示的に却下した構造
- `height_plane` の定義域はノズルの機械 XY なので**最後尾**。手前に置くと
  カメラ XY や board XY の高さを引いてしまう

この規則は `pad_to_machine` と `corrected_board_transform`（`posctrl`）にしか
書かれていない。job 層はここを呼ぶだけなので、規則のピンはここに集約する。

klipper / stage は自前 HAL のため手書き stub、camera は tests/helpers.py の
FakeCamera、machine は実 Machine（data/testing/config/machine.toml）、pcb は
PasteSession が保持するだけなので手書き stub を使う。
"""

from datetime import datetime

import numpy as np
import pytest
import shapely

from pcbasm import gcode
from pcbasm.config import Machine
from pcbasm.geometry import HeightPlane, Identity, Point2d, Point3d, Rotation
from pcbasm.pcb import CopperList, Layer, Outline, Pad
from pcbasm.posctrl import BoardAlignment, BoardCalibrationResult, RegionAlignment
from pcbasm.posctrl.copper import EdgeMatch
from pcbasm.posctrl.region import AlignmentRegion
from pcbasm.session import PasteSession
from pcbasm.vision import CalibrationResult, Image, Offset
from tests.helpers import TESTING_CONFIG_DIR, FakeCamera

PPM = 10.0  # pixel/mm
REGION_MM = 3.3  # 区の一辺（実機の region_size_px=100 @ 30.2px/mm 相当）
HALF_MM = REGION_MM / 2
BOARD_TRANSFORM = Rotation(30.0)  # 回転があると補正の挿し位置を弁別できる

# 区中心（board 座標）と、そこで測った変位（機械座標 [mm]）
LOCAL_FIELD: dict[tuple[int, int], Point2d] = {
    (0, 0): Point2d(0.10, -0.20),
    (1, 0): Point2d(0.46, -0.18),  # 隣と dx が 0.36mm 違う（実機で観測した勾配）
    (0, 1): Point2d(-0.05, 0.12),
    (1, 1): Point2d(0.30, 0.08),
}


def _machine_surface_z(x: float, y: float) -> float:
    """基板高さ面（2 次曲面）。6 点で HeightPlane に厳密に載る."""
    return 0.2 + 0.01 * x - 0.005 * y + 0.0002 * x**2 + 0.0001 * y**2 - 0.00015 * x * y


def _height_plane() -> HeightPlane:
    points = [
        (100.0, 30.0),
        (145.0, 32.0),
        (104.0, 70.0),
        (142.0, 68.0),
        (120.0, 45.0),
        (133.0, 58.0),
    ]
    return HeightPlane(
        tuple(Point3d(x, y, _machine_surface_z(x, y)) for x, y in points)
    )


def _center(cell: tuple[int, int]) -> Point2d:
    return Point2d(cell[0] * REGION_MM, cell[1] * REGION_MM)


def _alignment() -> BoardAlignment:
    """LOCAL_FIELD を成功区として持つ局所補正."""
    return BoardAlignment(
        results=tuple(
            RegionAlignment(
                region=AlignmentRegion(
                    index=index,
                    board_center=_center(cell),
                    anchor=BOARD_TRANSFORM.apply(_center(cell)),
                    roi=(0, 0, 100, 100),
                    constraint=120.0,
                    edge_point_count=240,
                ),
                match=EdgeMatch(
                    offset=Offset(px=Point2d(0.0, 0.0), pixel_per_mm=PPM),
                    rms_distance_px=0.4,
                    sharpness=0.7,
                ),
                displacement=displacement,
                increment=Point2d(0.0, 0.0),
                passes=1,
                converged=True,
            )
            for index, (cell, displacement) in enumerate(LOCAL_FIELD.items())
        )
    )


class _StubKlipper:
    """send_gcode と printer.cfg 参照だけを持つ Klipper 代替（自前 HAL）.

    `ProbeExecutor` / `PasteDispenser` / `AirPump` が構築時に printer.cfg の
    section を確かめるので、その 3 つだけ持った config を返す。
    """

    def __init__(self) -> None:
        self.sent: list[gcode.GCode] = []

    @property
    def readonly(self) -> "_StubKlipper":
        return self

    def get_config(self) -> dict[str, dict[str, str]]:
        return {
            "load_cell_probe": {},
            "manual_stepper paste_dispenser": {"rotation_distance": "8.0"},
            "output_pin air_pump": {},
        }

    def send_gcode(self, code: gcode.GCode) -> None:
        self.sent.append(code)


class _StubStage:
    """Move() の指令 XY を記録する XYZStage 代替（自前 HAL）."""

    max_velocity = 100.0

    def __init__(self) -> None:
        self._position = Point3d(0.0, 0.0, 5.0)
        self.moves: list[tuple[float, float]] = []

    def move(self, **kwargs) -> gcode.GCode:
        if "x" in kwargs and "y" in kwargs:
            self.moves.append((kwargs["x"], kwargs["y"]))
        return gcode.GCode("G1")

    def to_gcode(self, *args, **kwargs) -> gcode.GCode:
        return gcode.GCode()

    def get_position(self) -> Point3d:
        return self._position


class _StubPcb:
    """PasteSession が保持するだけの PcbFile 代替."""

    def __init__(self) -> None:
        self.outline = Outline(shapely.Polygon([(0, 0), (50, 0), (50, 30), (0, 30)]))
        self.copper = CopperList([])
        self.pads: list[object] = []
        self.components: list[object] = []


def _session(stage: _StubStage | None = None) -> PasteSession:
    """実 Machine 設定 + stub HAL で組んだ塗布セッション."""
    result = BoardCalibrationResult(
        machine=Machine(TESTING_CONFIG_DIR / "machine.toml"),
        klipper=_StubKlipper(),  # type: ignore[arg-type]
        stage=stage if stage is not None else _StubStage(),  # type: ignore[arg-type]
        camera=FakeCamera([Image(np.zeros((10, 10, 3), dtype=np.uint8))]),
        calibration=CalibrationResult(
            pixel_per_mm=PPM,
            square_size_mm=1.0,
            mean_distance_px=PPM,
            std_distance_px=0.0,
            resolution=(1280, 720),
            crop_size=(600, 600),
            calibrated_at=datetime.now(),
            z_position=5.0,
        ),
        offset_transform=Identity(),
        board_transform=BOARD_TRANSFORM,
        pcb=_StubPcb(),  # type: ignore[arg-type]
    )
    return PasteSession.from_calibration(result)


class TestPadToMachine:
    """pad_to_machine: board → ノズル機械座標の全変換（局所補正込み）."""

    def test_correction_is_applied_in_machine_coordinates_after_the_board_transform(
        self,
    ):
        """XY = board 変換 → その区の変位 → toolhead オフセット.

        変位はカメラの機械座標系で定義されているので、board 変換の後に足す。
        """
        session = _session()
        alignment = _alignment()
        toolhead = session.toolhead_offset

        for cell, displacement in LOCAL_FIELD.items():
            point = Point2d(_center(cell).x + 0.8 * HALF_MM, _center(cell).y)
            moved = session.pad_to_machine(
                point, alignment=alignment, height_plane=_height_plane()
            ).apply(point.to3d(0.0))

            want = toolhead.apply((BOARD_TRANSFORM.apply(point) + displacement).to3d(0))
            assert moved.x == pytest.approx(want.x, abs=1e-9)
            assert moved.y == pytest.approx(want.y, abs=1e-9)

    def test_correction_is_not_conjugated_into_board_space(self):
        """Board 空間で補正してから board 変換する構造とは違う結果になる.

        board 変換が 30° 回転なので、共役適用は変位を 30° 回した量だけずらす （dx 0.46 / dy -0.18
        なら 0.3mm 級の差）。却下された構造への回帰を落とす。
        """
        session = _session()
        point = _center((1, 0))

        moved = session.pad_to_machine(
            point, alignment=_alignment(), height_plane=_height_plane()
        ).apply(point.to3d(0.0))

        conjugated = session.toolhead_offset.apply(
            BOARD_TRANSFORM.apply(point + LOCAL_FIELD[(1, 0)]).to3d(0.0)
        )
        assert moved.x != pytest.approx(conjugated.x, abs=1e-6)
        assert moved.y != pytest.approx(conjugated.y, abs=1e-6)

    def test_height_plane_is_evaluated_at_the_final_nozzle_xy(self):
        """Z は**ノズル機械 XY** での高さ（board XY でもカメラ XY でもない）.

        height_plane を合成の手前に置くと、board 座標やカメラ機械座標での高さを 引いてしまう。toolhead
        オフセットは 23mm あるので実機では致命的にずれる。
        """
        session = _session()
        paste_height = 0.5
        point = _center((1, 1))

        moved = session.pad_to_machine(
            point, alignment=_alignment(), height_plane=_height_plane()
        ).apply(point.to3d(paste_height))

        assert moved.z == pytest.approx(
            paste_height + _machine_surface_z(moved.x, moved.y), abs=1e-9
        )
        # カメラ機械座標（toolhead オフセット前）で引いた高さとは違う
        camera_xy = BOARD_TRANSFORM.apply(point) + LOCAL_FIELD[(1, 1)]
        assert moved.z != pytest.approx(
            paste_height + _machine_surface_z(camera_xy.x, camera_xy.y), abs=1e-6
        )

    def test_each_pad_gets_its_own_correction(self):
        """Pad ごとに違う変換が返る（1 つ組んで全 pad に使い回さない）.

        区境界をまたぐ 2 点の XY の差が、board 変換ぶんの差 + 変位の段差になる。 全 pad 同一補正なら段差が消える。
        """
        session = _session()
        alignment = _alignment()
        plane = _height_plane()
        left = Point2d(_center((0, 0)).x + 0.8 * HALF_MM, 0.0)
        right = Point2d(_center((1, 0)).x - 0.8 * HALF_MM, 0.0)

        def shift_of(point: Point2d) -> Point2d:
            moved = session.pad_to_machine(
                point, alignment=alignment, height_plane=plane
            ).apply(point.to3d(0.0))
            base = session.toolhead_offset.apply(BOARD_TRANSFORM.apply(point).to3d(0.0))
            return Point2d(moved.x - base.x, moved.y - base.y)

        left_shift, right_shift = shift_of(left), shift_of(right)

        assert left_shift.x == pytest.approx(LOCAL_FIELD[(0, 0)].x, abs=1e-9)
        assert right_shift.x == pytest.approx(LOCAL_FIELD[(1, 0)].x, abs=1e-9)
        assert right_shift.x - left_shift.x == pytest.approx(0.36, abs=1e-9)

    def test_the_lookup_point_and_the_applied_point_are_independent(self):
        """Pad 中心で引いた補正を、その pad の全頂点へ同じだけ適用する.

        塗布は pad 中心で 1 回引いた変換をポリゴン全体に使う。頂点ごとに引き直すと 区境界をまたぐ pad の形が割れる。
        """
        session = _session()
        center = Point2d(_center((0, 0)).x + 0.8 * HALF_MM, 0.0)
        transform = session.pad_to_machine(
            center, alignment=_alignment(), height_plane=_height_plane()
        )

        vertex = Point2d(center.x + REGION_MM, center.y)  # 隣の区の中にある頂点
        moved = transform.apply(vertex.to3d(0.0))

        want = session.toolhead_offset.apply(
            (BOARD_TRANSFORM.apply(vertex) + LOCAL_FIELD[(0, 0)]).to3d(0.0)
        )
        assert moved.x == pytest.approx(want.x, abs=1e-9)
        assert moved.y == pytest.approx(want.y, abs=1e-9)


class TestBoardToMachine:
    """board_to_machine: 補正を含まない board → ノズル機械座標（高さ計測などに使う）."""

    def test_is_the_uncorrected_composition(self):
        """board_transform + toolhead_offset のみ（局所補正は入らない）.

        高さ計測やプレビューは補正前の設計座標で回すので、pad_to_machine とは 別物であり続ける必要がある。
        """
        session = _session()
        point = _center((1, 0))

        moved = session.board_to_machine.apply(point.to3d(0.0))

        want = session.toolhead_offset.apply(BOARD_TRANSFORM.apply(point).to3d(0.0))
        assert moved.x == pytest.approx(want.x, abs=1e-9)
        assert moved.y == pytest.approx(want.y, abs=1e-9)


class TestPerPadPasteLoop:
    """塗布ループの契約: pad ごとに引いた変換がノズルの指令座標まで届くこと.

    `_run_paste_solder` は実 Klipper でのホーミングと基準点合わせを必須とするため
    直接は走らせられない（分担は `tests/webui/jobs/test_posctrl.py` の docstring
    と同じ）。ここではその中核だけを pcbasm 側の実オブジェクトで組み直し、
    「pad ごとに `pad_to_machine` を引き、`apply(transform=...)` へ渡す」という
    ループの形が成立していることを押さえる。

    同じ pad ポリゴンを「自分の区で引いた変換」と「隣の区で引いた変換」で塗り、
    指令 XY の差が変位の差そのものになることを見る。補正が経路生成に食い込んで
    いれば点数が変わり、変換が使われていなければ差が 0 になる。
    """

    PAD_HALF = 0.6
    # 区境界をまたぐ 2 点（それぞれ (0,0) と (1,0) の内側）
    OWN = Point2d(_center((0, 0)).x + 0.8 * HALF_MM, 0.0)
    NEIGHBOUR = Point2d(_center((1, 0)).x - 0.8 * HALF_MM, 0.0)

    def _pad_polygon(self, center: Point2d) -> shapely.Polygon:
        half = self.PAD_HALF
        return shapely.Polygon(
            [
                (center.x - half, center.y - half),
                (center.x + half, center.y - half),
                (center.x + half, center.y + half),
                (center.x - half, center.y + half),
            ]
        )

    def _paste(self, center: Point2d, lookup_at: Point2d) -> list[tuple[float, float]]:
        """Center の pad を、lookup_at で引いた per-pad 変換で塗り指令 XY を返す."""
        stage = _StubStage()
        session = _session(stage)
        with session.make_applicator() as applicator:
            applicator.apply(
                [self._pad_polygon(center)],
                transform=session.pad_to_machine(
                    lookup_at, alignment=_alignment(), height_plane=_height_plane()
                ),
            )
        return stage.moves

    def test_the_lookup_point_decides_the_nozzle_coordinates(self):
        """引く点を隣の区へ移すと、指令 XY が変位の差ぶんだけ丸ごとずれる.

        全 pad に同じ変換を使い回す / 変換が経路へ届いていない実装では差が 0 になる。
        """
        own = self._paste(self.OWN, self.OWN)
        borrowed = self._paste(self.OWN, self.NEIGHBOUR)

        assert own, "塗布の指令が 1 つも出ていない（空振り防止）"
        assert len(own) == len(borrowed)  # 補正は経路の構造を変えない
        step = LOCAL_FIELD[(1, 0)] - LOCAL_FIELD[(0, 0)]
        assert step.x == pytest.approx(0.36, abs=1e-12)
        for (ax, ay), (bx, by) in zip(own, borrowed, strict=True):
            assert bx - ax == pytest.approx(step.x, abs=1e-9)
            assert by - ay == pytest.approx(step.y, abs=1e-9)


def _pad(designator: str, center: Point2d, half: float = 0.3) -> Pad:
    """中心 (center) の正方 pad."""
    return Pad(
        designator=designator,
        pad_number="1",
        net_name="NET",
        layer=Layer.TOP,
        polygon=shapely.Polygon(
            [
                (center.x - half, center.y - half),
                (center.x + half, center.y - half),
                (center.x + half, center.y + half),
                (center.x - half, center.y + half),
            ]
        ),
    )


class TestPadTransforms:
    """pad_transforms: pad と変換の対応を値として返す（塗布ループの唯一の入口）.

    塗布ループも初回パージも、この 1 本の列から変換を受け取る。対応を呼び出し側の
    ループの書き方に委ねると「全 pad に同じ変換を使う」「パージだけ別の変換を
    組み直す」といった退行が job 層で起きても検出できない。**入力順を保つ**ことは
    塗布側がスライス（`entries[0]` がパージ / `entries[len(purge):]` が塗布）で
    依存している契約なので、順序そのものもピンする。
    """

    # 区 (0,0) と (1,0) にそれぞれ入る 2 pad。同じ区の pad だけでは
    # 「pad ごとに引いているか」を弁別できない
    OWN = Point2d(_center((0, 0)).x + 0.8 * HALF_MM, 0.0)
    NEIGHBOUR = Point2d(_center((1, 0)).x - 0.8 * HALF_MM, 0.0)

    def _shift_of(self, session: PasteSession, pad: Pad, transform) -> Point2d:
        """その変換が pad 中心に与える補正ぶんの平行移動（機械座標）."""
        moved = transform.apply(pad.center.to3d(0.0))
        base = session.toolhead_offset.apply(
            BOARD_TRANSFORM.apply(pad.center).to3d(0.0)
        )
        return Point2d(moved.x - base.x, moved.y - base.y)

    def test_each_entry_uses_the_correction_of_its_own_pad(self):
        """区をまたぐ 2 pad が、それぞれ自分の区の変位を受け取る.

        引く点を `pads[0].center` などに固定した実装では 2 つの変換が同じになり、
        0.36mm の段差が消える。
        """
        session = _session()
        pads = [_pad("R1", self.OWN), _pad("R2", self.NEIGHBOUR)]

        entries = session.pad_transforms(
            pads, alignment=_alignment(), height_plane=_height_plane()
        )

        shifts = [self._shift_of(session, pad, transform) for pad, transform in entries]
        assert shifts[0].x == pytest.approx(LOCAL_FIELD[(0, 0)].x, abs=1e-9)
        assert shifts[1].x == pytest.approx(LOCAL_FIELD[(1, 0)].x, abs=1e-9)
        assert shifts[1].x - shifts[0].x == pytest.approx(0.36, abs=1e-9)

    def test_keeps_the_input_pads_in_order(self):
        """戻り値は入力と同順・同数（並べ替えない）.

        塗布側は index のスライスで対応づけるので、順序が変わると pad と変換の 対応がまるごとずれる。designator
        の辞書順とも塗布順とも違う並びで渡す。
        """
        session = _session()
        pads = [
            _pad("R9", self.NEIGHBOUR),
            _pad("R1", self.OWN),
            _pad("R5", self.NEIGHBOUR),
        ]

        entries = session.pad_transforms(
            pads, alignment=_alignment(), height_plane=_height_plane()
        )

        assert [pad.designator for pad, _ in entries] == ["R9", "R1", "R5"]

    def test_purge_prefix_slicing_maps_each_pad_to_its_own_transform(self):
        """先頭にパージ pad を足した列をスライスしても対応が保たれる.

        塗布ジョブの使い方そのもの（`pad_transforms([*purge, *routed])` を 1 回だけ呼び、パージは
        `entries[0]`、塗布は `entries[len(purge):]`）。
        パージ用の変換を別途組み直す場所が無いことが「パージだけ無補正」への 対策なので、この対応が崩れないことを契約にする。
        """
        session = _session()
        purge_pads = [_pad("PURGE", self.NEIGHBOUR)]
        routed_pads = [_pad("R1", self.OWN), _pad("R2", self.NEIGHBOUR)]

        entries = session.pad_transforms(
            [*purge_pads, *routed_pads],
            alignment=_alignment(),
            height_plane=_height_plane(),
        )

        purge_pad, purge_transform = entries[0]
        assert purge_pad.designator == "PURGE"
        # パージ pad は隣の区にいるので、その区の変位を受け取る
        assert self._shift_of(session, purge_pad, purge_transform).x == pytest.approx(
            LOCAL_FIELD[(1, 0)].x, abs=1e-9
        )
        paste_entries = entries[len(purge_pads) :]
        assert [pad.designator for pad, _ in paste_entries] == ["R1", "R2"]
        assert self._shift_of(session, *paste_entries[0]).x == pytest.approx(
            LOCAL_FIELD[(0, 0)].x, abs=1e-9
        )

    def test_no_pads_yields_an_empty_list(self):
        """Pad が空でも例外にしない（パージ無し = 前置きが空のケース）."""
        session = _session()

        assert (
            session.pad_transforms(
                [], alignment=_alignment(), height_plane=_height_plane()
            )
            == []
        )
