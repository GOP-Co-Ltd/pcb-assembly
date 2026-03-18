"""基板表面のbed meshを計測する."""

import logging

import numpy as np

from pcb_assembly import gcode
from pcb_assembly.control.probe import ProbeExecutor
from pcb_assembly.geometry import HeightMap, Move, Point2d, Transform
from pcb_assembly.hal import Klipper, XYZStage
from pcb_assembly.pcb import Outline
from pcb_assembly.utils import get_class_module_path


class HeightTransformMeasurer:
    """プローブを用いて基板表面のbed meshを計測し、HeightMapを返すクラス.

    Board座標系のグリッド上で高さを計測し、双線形補間による
    Z補正変換を構築する。

    Example:
        probe_executor = ProbeExecutor(klipper=klipper, probe=probe)
        measurer = HeightTransformMeasurer(probe_executor=probe_executor, klipper=klipper, stage=stage)
        height_map = measurer.measure(outline=pcb.outline, board_to_machine=board_transform)
        corrected = height_map.apply(Point3d(5.0, 10.0, 0.1))
    """

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
        self._probe_executor = probe_executor
        self._klipper = klipper
        self._stage = stage
        self._grid_size = grid_size
        self._inset = inset
        self._move_settle_time = move_settle_time
        self._move_velocity_ratio = move_velocity_ratio
        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def measure(
        self,
        outline: Outline,
        board_to_machine: Transform,
    ) -> HeightMap:
        """Board上のグリッドで高さ計測し、HeightMapを返す.

        Args:
            outline: 基板のアウトライン
            board_to_machine: Board座標→機械座標の変換

        Returns:
            Board座標系のHeightMap
        """
        rows, cols = self._grid_size
        x_min = self._inset
        x_max = outline.width - self._inset
        y_min = self._inset
        y_max = outline.height - self._inset

        self._logger.info(
            f"Bed mesh計測開始: {rows}x{cols}グリッド, "
            f"Board範囲: ({x_min:.1f}, {y_min:.1f}) - ({x_max:.1f}, {y_max:.1f})"
        )

        # Generate grid points in board coordinates
        xs = np.linspace(x_min, x_max, cols)
        ys = np.linspace(y_min, y_max, rows)

        z_values = np.zeros((rows, cols))
        move_velocity = self._stage.max_velocity * self._move_velocity_ratio

        for i, y in enumerate(ys):
            for j, x in enumerate(xs):
                board_pt = Point2d(float(x), float(y))
                machine_pt = board_to_machine.apply(board_pt)

                self._logger.info(
                    f"計測点 ({i},{j}): Board({board_pt.x:.1f}, {board_pt.y:.1f}) "
                    f"-> Machine({machine_pt.x:.3f}, {machine_pt.y:.3f})"
                )

                # Move to XY position
                self._klipper.send_gcode(
                    self._stage.to_gcode(Move.from_point(machine_pt, v=move_velocity))
                    + gcode.wait(self._move_settle_time)
                    + gcode.wait_for_done()
                )

                # Probe
                z = self._probe_executor.probe()
                z_values[i, j] = z
                self._logger.info(f"Z={z:.4f}mm")

        self._logger.info("Bed mesh計測完了")

        return HeightMap(
            z_values=z_values,
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
        )
