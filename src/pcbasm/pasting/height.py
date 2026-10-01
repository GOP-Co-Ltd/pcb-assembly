"""基板表面の高さをプローブ計測する."""

import logging
from collections.abc import Iterable

from shapely.geometry import Polygon

from pcbasm.config import Probe
from pcbasm.gcode import GCode
from pcbasm.geometry import (
    HeightPlane,
    Point2d,
    Point3d,
    Transform,
    sample_points_in_polygons,
    sort_by_nearest,
)
from pcbasm.hal import Klipper, Speed, XYZStage
from pcbasm.pasting.probe import ProbeExecutor
from pcbasm.pcb import Copper
from pcbasm.utils import get_class_module_path


def plan_probe_points(
    coppers: Iterable[Copper], outline: Polygon | None, *, config: Probe
) -> list[Point2d]:
    """Machine の probe 設定で計測点（銅箔島内・外形マージン内）を計画する（装置不要）."""
    return _sample_points(
        coppers,
        outline,
        min_radius=config.min_radius,
        board_edge_margin=config.board_edge_margin,
        min_samples=config.min_samples,
        max_samples=config.max_samples,
    )


def _sample_points(
    coppers: Iterable[Copper],
    outline: Polygon | None,
    *,
    min_radius: float,
    board_edge_margin: float,
    min_samples: int,
    max_samples: int,
) -> list[Point2d]:
    return sample_points_in_polygons(
        (c.polygon for c in coppers),
        min_radius=min_radius,
        min_samples=min_samples,
        max_samples=max_samples,
        outline=outline,
        outline_margin=board_edge_margin,
    )


class _BoardPointProber:
    """1 点の Board 座標から機械座標へ移動して高さをプローブ計測する内部ヘルパ."""

    def __init__(
        self,
        probe_executor: ProbeExecutor,
        klipper: Klipper,
        stage: XYZStage,
        *,
        settle_sec: float,
        move_velocity_ratio: float,
        logger: logging.Logger,
    ) -> None:
        self._probe_executor = probe_executor
        self._klipper = klipper
        self._stage = stage
        self._settle_sec = settle_sec
        self._move_velocity = stage.max_velocity * move_velocity_ratio
        self._logger = logger

    def probe_at(
        self, board_pt: Point2d, board_to_machine: Transform, label: str
    ) -> Point3d:
        """Board 座標 board_pt をプローブし、機械座標 XY と Z を返す."""
        probe_pt = board_to_machine.apply(board_pt)
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
            + GCode.wait(self._settle_sec)
            + GCode.wait_for_done()
        )

        z = self._probe_executor.probe()
        self._logger.info(f"Z={z:.4f}mm")
        return Point3d(x=probe_pt.x, y=probe_pt.y, z=z)

    def route_points(
        self, board_points: Iterable[Point2d], board_to_machine: Transform
    ) -> list[Point2d]:
        """現在位置から実 probe 位置への移動距離が短くなる順へ並べる."""
        return sort_by_nearest(
            board_points,
            self._stage.get_position(),
            key=lambda p: board_to_machine.apply(p).to3d(),
        )


class HeightPlaneMeasurer:
    """銅箔島ベースでプローブ計測し、2 次曲面フィットした HeightPlane を返す.

    入力銅箔を `sample_points_in_polygons` で疎にサンプリングし、
    各 Board 点を機械座標へ写してプローブ計測を行う。返す HeightPlane の XY は
    実際にプローブした機械座標で、Z はその位置の絶対 surface Z。
    """

    def __init__(
        self,
        probe_executor: ProbeExecutor,
        klipper: Klipper,
        stage: XYZStage,
        *,
        min_radius: float,
        board_edge_margin: float,
        min_samples: int,
        max_samples: int,
        settle_sec: float,
        move_velocity_ratio: float = 0.9,
    ) -> None:
        self._logger = logging.getLogger(get_class_module_path(self.__class__))
        self._min_radius = min_radius
        self._board_edge_margin = board_edge_margin
        self._min_samples = min_samples
        self._max_samples = max_samples
        self._point_prober = _BoardPointProber(
            probe_executor=probe_executor,
            klipper=klipper,
            stage=stage,
            settle_sec=settle_sec,
            move_velocity_ratio=move_velocity_ratio,
            logger=self._logger,
        )

    def plan_points(
        self, coppers: Iterable[Copper], outline: Polygon | None = None
    ) -> list[Point2d]:
        """計測する Board 点（銅箔島内、外形マージン内）を計画する（装置を動かさない）."""
        return _sample_points(
            coppers,
            outline,
            min_radius=self._min_radius,
            board_edge_margin=self._board_edge_margin,
            min_samples=self._min_samples,
            max_samples=self._max_samples,
        )

    def measure(
        self,
        coppers: Iterable[Copper],
        board_to_machine: Transform,
        outline: Polygon | None = None,
    ) -> HeightPlane:
        """銅箔島内の Board 点で高さ計測し、機械 XY の HeightPlane を返す."""
        board_points = self._point_prober.route_points(
            self.plan_points(coppers, outline), board_to_machine
        )
        coord_str = ", ".join(f"({p.x:.1f}, {p.y:.1f})" for p in board_points)
        self._logger.info(f"Probe点 {len(board_points)}個: {coord_str}")

        results = [
            self._point_prober.probe_at(board_pt, board_to_machine, label=str(idx))
            for idx, board_pt in enumerate(board_points)
        ]

        self._logger.info("Height plane計測完了")
        return HeightPlane(points=tuple(results))
