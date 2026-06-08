"""基板表面の高さをプローブ計測する."""

import logging
from collections.abc import Iterable

from pcbasm import gcode
from pcbasm.geometry import (
    HeightPlane,
    Point2d,
    Point3d,
    Transform,
    sample_points_in_polygons,
)
from pcbasm.hal import Klipper, Speed, XYZStage
from pcbasm.pasting.probe import ProbeExecutor
from pcbasm.pcb import Copper
from pcbasm.utils import get_class_module_path


class _BoardPointProber:
    """1点のBoard座標へ移動して高さをプローブ計測する内部ヘルパ."""

    def __init__(
        self,
        probe_executor: ProbeExecutor,
        klipper: Klipper,
        stage: XYZStage,
        *,
        shift: tuple[float, float],
        move_settle_time: float,
        move_velocity_ratio: float,
        logger: logging.Logger,
    ) -> None:
        self._probe_executor = probe_executor
        self._klipper = klipper
        self._stage = stage
        self._shift = shift
        self._move_settle_time = move_settle_time
        self._move_velocity = stage.max_velocity * move_velocity_ratio
        self._logger = logger

    def probe_at(
        self, board_pt: Point2d, board_to_machine: Transform, label: str
    ) -> Point3d:
        """Board座標 board_pt をシフトしてプローブし、実接触点(Board座標)とZを返す."""
        machine_pt = board_to_machine.apply(board_pt)
        dx, dy = self._shift
        probe_pt = Point2d(x=machine_pt.x + dx, y=machine_pt.y + dy)
        self._logger.info(
            f"計測点 {label}: Board({board_pt.x:.1f}, {board_pt.y:.1f}) "
            f"-> Machine({probe_pt.x:.3f}, {probe_pt.y:.3f})"
        )

        self._klipper.send_gcode(
            self._stage.move(
                x=probe_pt.x,
                y=probe_pt.y,
                speed=Speed.absolute(self._move_velocity),
            )
            + gcode.wait(self._move_settle_time)
            + gcode.wait_for_done()
        )

        z = self._probe_executor.probe()
        self._logger.info(f"Z={z:.4f}mm")
        probed_board = board_to_machine.inverse().apply(probe_pt)
        return Point3d(x=probed_board.x, y=probed_board.y, z=z)


class HeightPlaneMeasurer:
    """銅箔島ベースでプローブ計測し、平面フィットしたHeightPlaneを返す.

    入力銅箔を `sample_points_in_polygons` で疎にサンプリングし、
    各点でプローブ計測を行う。
    """

    def __init__(
        self,
        probe_executor: ProbeExecutor,
        klipper: Klipper,
        stage: XYZStage,
        *,
        min_radius: float,
        min_samples: int,
        max_samples: int,
        probe_shift: tuple[float, float] = (0.0, 0.0),
        move_settle_time: float = 0.5,
        move_velocity_ratio: float = 0.9,
    ) -> None:
        self._logger = logging.getLogger(get_class_module_path(self.__class__))
        self._min_radius = min_radius
        self._min_samples = min_samples
        self._max_samples = max_samples
        self._point_prober = _BoardPointProber(
            probe_executor=probe_executor,
            klipper=klipper,
            stage=stage,
            shift=probe_shift,
            move_settle_time=move_settle_time,
            move_velocity_ratio=move_velocity_ratio,
            logger=self._logger,
        )

    def measure(
        self,
        coppers: Iterable[Copper],
        board_to_machine: Transform,
    ) -> HeightPlane:
        """銅箔島内のサンプル点で高さ計測し、HeightPlaneを返す."""
        board_points = sample_points_in_polygons(
            (c.polygon for c in coppers),
            min_radius=self._min_radius,
            min_samples=self._min_samples,
            max_samples=self._max_samples,
        )
        coord_str = ", ".join(f"({p.x:.1f}, {p.y:.1f})" for p in board_points)
        self._logger.info(f"Probe点 {len(board_points)}個: {coord_str}")

        results = [
            self._point_prober.probe_at(board_pt, board_to_machine, label=str(idx))
            for idx, board_pt in enumerate(board_points)
        ]

        self._logger.info("Height plane計測完了")
        return HeightPlane(points=tuple(results))
