"""基板表面のbed meshを計測する."""

import logging
from collections.abc import Iterable, Iterator

import numpy as np

from pcb_assembly import gcode
from pcb_assembly.control.probe import ProbeExecutor
from pcb_assembly.geometry import (
    HeightMap,
    HeightPoints,
    Move,
    Point2d,
    Point3d,
    Transform,
    sample_points_in_polygons,
)
from pcb_assembly.hal import Klipper, XYZStage
from pcb_assembly.pcb import Copper, Outline
from pcb_assembly.utils import get_class_module_path


class _BoardPointProber:
    """1点のBoard座標へ移動して高さをプローブ計測する内部ヘルパ."""

    def __init__(
        self,
        probe_executor: ProbeExecutor,
        klipper: Klipper,
        stage: XYZStage,
        *,
        move_settle_time: float,
        move_velocity_ratio: float,
        logger: logging.Logger,
    ) -> None:
        self._probe_executor = probe_executor
        self._klipper = klipper
        self._stage = stage
        self._move_settle_time = move_settle_time
        self._move_velocity = stage.max_velocity * move_velocity_ratio
        self._logger = logger

    def probe_at(
        self, board_pt: Point2d, board_to_machine: Transform, label: str
    ) -> float:
        """Board座標 board_pt へ移動して高さをプローブし、Z値を返す."""
        machine_pt = board_to_machine.apply(board_pt)
        self._logger.info(
            f"計測点 {label}: Board({board_pt.x:.1f}, {board_pt.y:.1f}) "
            f"-> Machine({machine_pt.x:.3f}, {machine_pt.y:.3f})"
        )

        self._klipper.send_gcode(
            self._stage.to_gcode(Move.from_point(machine_pt, v=self._move_velocity))
            + gcode.wait(self._move_settle_time)
            + gcode.wait_for_done()
        )

        z = self._probe_executor.probe()
        self._logger.info(f"Z={z:.4f}mm")
        return z


class _GridProber:
    """Board座標系のグリッド上で高さをプローブ計測する内部ヘルパ."""

    def __init__(
        self,
        point_prober: _BoardPointProber,
        *,
        grid_size: tuple[int, int],
        inset: float,
    ) -> None:
        self._point_prober = point_prober
        self._grid_size = grid_size
        self._inset = inset

    def bounds(self, outline: Outline) -> tuple[float, float, float, float]:
        """(x_min, x_max, y_min, y_max) をinset適用後のBoard座標で返す."""
        return (
            self._inset,
            outline.width - self._inset,
            self._inset,
            outline.height - self._inset,
        )

    def iter_points(
        self,
        outline: Outline,
        board_to_machine: Transform,
    ) -> Iterator[tuple[int, int, Point2d, float]]:
        """グリッドを順に走査し、(i, j, board_pt, z) を逐次yieldする."""
        rows, cols = self._grid_size
        x_min, x_max, y_min, y_max = self.bounds(outline)
        xs = np.linspace(x_min, x_max, cols)
        ys = np.linspace(y_min, y_max, rows)

        for i, y in enumerate(ys):
            for j, x in enumerate(xs):
                board_pt = Point2d(float(x), float(y))
                z = self._point_prober.probe_at(
                    board_pt, board_to_machine, label=f"({i},{j})"
                )
                yield i, j, board_pt, z


class HeightTransformMeasurer:
    """Board座標系のグリッド上でプローブ計測し、双線形補間のHeightMapを返す."""

    def __init__(
        self,
        probe_executor: ProbeExecutor,
        klipper: Klipper,
        stage: XYZStage,
        *,
        grid_size: tuple[int, int] = (3, 3),
        inset: float = 5.0,
        move_settle_time: float = 0.5,
        move_velocity_ratio: float = 0.9,
    ) -> None:
        self._logger = logging.getLogger(get_class_module_path(self.__class__))
        self._grid_size = grid_size
        self._prober = _GridProber(
            point_prober=_BoardPointProber(
                probe_executor=probe_executor,
                klipper=klipper,
                stage=stage,
                move_settle_time=move_settle_time,
                move_velocity_ratio=move_velocity_ratio,
                logger=self._logger,
            ),
            grid_size=grid_size,
            inset=inset,
        )

    def measure(
        self,
        outline: Outline,
        board_to_machine: Transform,
    ) -> HeightMap:
        """Board上のグリッドで高さ計測し、HeightMapを返す."""
        rows, cols = self._grid_size
        x_min, x_max, y_min, y_max = self._prober.bounds(outline)
        self._logger.info(
            f"Bed mesh計測開始: {rows}x{cols}グリッド, "
            f"Board範囲: ({x_min:.1f}, {y_min:.1f}) - ({x_max:.1f}, {y_max:.1f})"
        )

        z_values = np.zeros((rows, cols))
        for i, j, _board_pt, z in self._prober.iter_points(outline, board_to_machine):
            z_values[i, j] = z

        self._logger.info("Bed mesh計測完了")
        return HeightMap(
            z_values=z_values,
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
        )


class HeightPointsMeasurer:
    """銅箔島ベースでプローブ計測し、散在点補間のHeightPointsを返す.

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
            move_settle_time=move_settle_time,
            move_velocity_ratio=move_velocity_ratio,
            logger=self._logger,
        )

    def measure(
        self,
        coppers: Iterable[Copper],
        board_to_machine: Transform,
    ) -> HeightPoints:
        """銅箔島内のサンプル点で高さ計測し、HeightPointsを返す."""
        board_points = sample_points_in_polygons(
            (c.polygon for c in coppers),
            min_radius=self._min_radius,
            min_samples=self._min_samples,
            max_samples=self._max_samples,
        )
        coord_str = ", ".join(f"({p.x:.1f}, {p.y:.1f})" for p in board_points)
        self._logger.info(f"Probe点 {len(board_points)}個: {coord_str}")

        results = [
            Point3d(
                x=board_pt.x,
                y=board_pt.y,
                z=self._point_prober.probe_at(
                    board_pt, board_to_machine, label=str(idx)
                ),
            )
            for idx, board_pt in enumerate(board_points)
        ]

        self._logger.info("Height points計測完了")
        return HeightPoints(points=tuple(results))
