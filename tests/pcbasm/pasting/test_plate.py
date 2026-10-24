"""位置合わせしない銅板（外形だけの矩形基板）の公開契約.

高さ計測は Board 計測結果（実カメラ）を要するので通さず、高さ面を直接与えた ``CopperPlate`` を組む。
装置側は自前 HAL の fake で、検証は送信した G-code に対して行う。
"""

from pathlib import Path
from typing import Any

import pytest

from pcbasm.align import BoardFrame
from pcbasm.config import load_machine
from pcbasm.geometry import HeightPlane, Identity, Point3d, Shift
from pcbasm.hal import XYZStage
from pcbasm.hal.fake import FakeKlipper
from pcbasm.pasting.plate import CopperPlate, write_rect_board
from pcbasm.pasting.session import PasteSession
from tests.support.paths import TESTING_DATA_DIR

MACHINE = load_machine(TESTING_DATA_DIR / "machine.toml")

# 機械 XY に対して z = 0.01x + 0.02y で傾いた面
TILTED = HeightPlane(
    tuple(
        Point3d(x, y, 0.01 * x + 0.02 * y)
        for x, y in ((0, 0), (100, 0), (0, 100), (100, 100), (200, 0), (0, 200))
    )
)


@pytest.fixture
def klipper() -> FakeKlipper:
    return FakeKlipper()


@pytest.fixture
def plate(tmp_path: Path, klipper: FakeKlipper) -> CopperPlate:
    """20 x 10 mm の銅板を機械座標 (100, 50) に置く（toolhead offset は 0）."""
    unused: Any = object()
    session = PasteSession(
        machine=MACHINE,
        klipper=klipper,
        stage=XYZStage(klipper),
        camera=unused,
        board_frame=BoardFrame(
            board=write_rect_board(tmp_path / "plate.kicad_pcb", 20.0, 10.0),
            camera_model=unused,
            board_transform=Shift(x=100.0, y=50.0),
        ),
        toolhead_offset=Identity(),
        probe_executor=unused,
        height_measurer=unused,
    )
    return CopperPlate(session, TILTED)


class TestWriteRectBoard:
    def test_writes_an_outline_only_board_and_reads_it_back(self, tmp_path: Path):
        path = tmp_path / "plate.kicad_pcb"

        board = write_rect_board(path, 20.0, 10.0)

        assert path.is_file()
        assert board.outline.polygon.bounds == pytest.approx((0.0, 0.0, 20.0, 10.0))
        assert not board.pads


class TestCenterZ:
    def test_is_the_height_plane_at_the_plate_center(self, plate: CopperPlate):
        """板の中心 (10, 5) は機械座標 (110, 55)。傾いた面のそこでの高さを返す."""
        assert plate.center_z() == pytest.approx(0.01 * 110.0 + 0.02 * 55.0)


class TestMoveClear:
    def test_moves_in_front_of_the_plate_edge(
        self, plate: CopperPlate, klipper: FakeKlipper
    ):
        """板の手前（board の -Y）へ、板端から clearance だけ離れた点へ動かして到達を待つ."""
        assert plate.move_clear(5.0) is None

        (move,) = klipper.g1_moves()
        assert (move["x"], move["y"]) == pytest.approx((100.0, 45.0))
        assert klipper.sent_lines[-1] == "M400"

    def test_does_not_move_when_the_target_is_outside_the_travel(
        self, plate: CopperPlate, klipper: FakeKlipper
    ):
        """機械の Y 下限は 0。板端から 60 mm 手前は可動域の外."""
        error = plate.move_clear(60.0)

        assert error == "退避位置 (100.000, -10.000) が可動域外のため移動しません"
        assert klipper.sent == ()
