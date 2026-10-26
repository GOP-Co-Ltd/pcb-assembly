"""位置合わせしない銅板（外形だけの矩形基板）.

吐出量の校正と塗布量の校正は、その場で生成した銅板の外形を Board 計測してから塗る。
銅板には照合する銅箔のパターンが無いので、領域照合の補正を挟まず、板上のどの点にも同じ変換を使う。
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

import attrs

from pcbasm.board import Board, Copper, Layer
from pcbasm.gcode import GCode
from pcbasm.geometry import HeightPlane, Point2d, Transform
from pcbasm.kicad import load_board
from pcbasm.kicad.build import generate_rect_pcb, save_board
from pcbasm.pasting.labels import fmt_xy
from pcbasm.pasting.session import PasteSession


def write_rect_board(path: Path, width: float, height: float) -> Board:
    """外形だけの矩形基板を ``path`` へ書き、読み直した Board を返す."""
    save_board(generate_rect_pcb(width, height), path)
    return load_board(path)


@attrs.frozen
class CopperPlate:
    """Board 計測と高さ計測を済ませた銅板.

    Attributes:
        session: 銅板を Board 計測した塗布セッション
        height_plane: 銅板の高さ面
    """

    session: PasteSession
    height_plane: HeightPlane

    @classmethod
    def measure(cls, session: PasteSession) -> Self:
        """外形を銅箔とみなして高さ面を計測する（生成した板は TOP 銅箔を持たない）."""
        outline = session.board_frame.board.outline.polygon
        return cls(
            session,
            session.measure_height_plane([Copper(layer=Layer.TOP, polygon=outline)]),
        )

    @property
    def transform(self) -> Transform:
        """板上の点の board → toolhead → 高さ変換（補正を挟まない）."""
        return self.session.plate_transform(height_plane=self.height_plane)

    def center_z(self) -> float:
        """高さ面の、銅板中心における Z [mm]."""
        minx, miny, maxx, maxy = self.session.board_frame.board.outline.polygon.bounds
        center = Point2d((minx + maxx) / 2.0, (miny + maxy) / 2.0)
        machine = self.session.board_to_machine.apply(center)
        return self.height_plane.apply(machine.to3d(0.0)).z

    def move_clear(self, clearance_mm: float) -> str | None:
        """ノズルを板の手前（board の -Y）へ板端から ``clearance_mm`` 逃がす.

        Returns:
            逃がし先が可動域外で動かなかったときの理由（動いたら ``None``）
        """
        session = self.session
        target = session.board_to_machine.apply(Point2d(0.0, -clearance_mm))
        if not session.stage.limits.contains(x=target.x, y=target.y):
            return f"退避位置 {fmt_xy(target, 3)} が可動域外のため移動しません"
        session.klipper.send_gcode(
            session.stage.move(x=target.x, y=target.y) + GCode.wait_for_done()
        )
        return None
