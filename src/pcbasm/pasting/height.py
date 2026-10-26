"""基板表面の高さをプローブ計測し、計画点と高さ面を描く."""

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.axes import Axes
from shapely.geometry import MultiPoint, Polygon

from pcbasm.board import Board, Copper, Layer
from pcbasm.config.probe import ProbeConfig
from pcbasm.geometry import (
    HeightPlane,
    Identity,
    Point2d,
    Point3d,
    Transform,
    sample_points_in_polygons,
    sort_by_nearest,
    transform_polygon,
)
from pcbasm.hal import KlipperClient, Speed, XYZStage
from pcbasm.pasting.probe import ProbeExecutor
from pcbasm.plotting import outline_patch, polygon_with_holes_patch, save_png

logger = logging.getLogger(__name__)

_MESH_RESOLUTION = 50


def plan_probe_points(
    coppers: Iterable[Copper], outline: Polygon | None, *, config: ProbeConfig
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
        klipper: KlipperClient,
        stage: XYZStage,
        *,
        settle_sec: float,
        move_velocity_ratio: float,
    ) -> None:
        self._probe_executor = probe_executor
        self._klipper = klipper
        self._stage = stage
        self._settle_sec = settle_sec
        self._move_velocity = stage.max_velocity * move_velocity_ratio

    def probe_at(
        self, board_pt: Point2d, board_to_machine: Transform, label: str
    ) -> Point3d:
        """Board 座標 board_pt をプローブし、機械座標 XY と Z を返す."""
        probe_pt = board_to_machine.apply(board_pt)
        logger.info(
            f"計測点 {label}: Board({board_pt.x:.1f}, {board_pt.y:.1f}) "
            f"-> Machine({probe_pt.x:.3f}, {probe_pt.y:.3f})"
        )

        self._klipper.send_gcode(
            self._stage.move_settled(
                x=probe_pt.x,
                y=probe_pt.y,
                speed=Speed.absolute(self._move_velocity),
                settle_sec=self._settle_sec,
            )
        )

        z = self._probe_executor.probe()
        logger.info(f"Z={z:.4f}mm")
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
        klipper: KlipperClient,
        stage: XYZStage,
        *,
        min_radius: float,
        board_edge_margin: float,
        min_samples: int,
        max_samples: int,
        settle_sec: float,
        move_velocity_ratio: float = 0.9,
    ) -> None:
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
        )

    def measure(
        self,
        coppers: Iterable[Copper],
        board_to_machine: Transform,
        outline: Polygon | None = None,
    ) -> HeightPlane:
        """銅箔島内の Board 点で高さ計測し、機械 XY の HeightPlane を返す."""
        planned = _sample_points(
            coppers,
            outline,
            min_radius=self._min_radius,
            board_edge_margin=self._board_edge_margin,
            min_samples=self._min_samples,
            max_samples=self._max_samples,
        )
        board_points = self._point_prober.route_points(planned, board_to_machine)
        coord_str = ", ".join(f"({p.x:.1f}, {p.y:.1f})" for p in board_points)
        logger.info(f"Probe点 {len(board_points)}個: {coord_str}")

        results = [
            self._point_prober.probe_at(board_pt, board_to_machine, label=str(idx))
            for idx, board_pt in enumerate(board_points)
        ]

        logger.info("Height plane計測完了")
        return HeightPlane(points=tuple(results))


def render_height_plane(
    height_plane: HeightPlane,
    pcb: Board,
    title: str,
    output_path: Path,
    pcb_to_plane: Transform = Identity(),
) -> None:
    """HeightPlane の高さを 2D ヒートマップ・基板背景と重ねて PNG 保存する.

    ヒートマップの値は z=0 の点に ``apply`` した結果、すなわち曲面の高さ [mm]。

    ``pcb_to_plane`` は基板座標の背景を HeightPlane と同じ XY 座標系（通常は機械座標）へ写す変換。

    図の Y 軸は上向きで、``render_pcb`` とは上下の向きが逆になる。
    """
    xs = [p.x for p in height_plane.points]
    ys = [p.y for p in height_plane.points]
    zs = [p.z for p in height_plane.points]

    # z=0 で入力すると apply 後の z 値がフィットした曲面の高さそのものになる
    plane_outline = transform_polygon(pcb.outline.polygon, pcb_to_plane)
    minx, miny, maxx, maxy = plane_outline.bounds
    grid_x = np.linspace(minx, maxx, _MESH_RESOLUTION)
    grid_y = np.linspace(miny, maxy, _MESH_RESOLUTION)
    mesh_z = np.array(
        [
            [height_plane.apply(Point3d(float(x), float(y), 0.0)).z for x in grid_x]
            for y in grid_y
        ]
    )

    fig, ax = plt.subplots(figsize=(10, 8))
    heatmap = ax.imshow(
        mesh_z,
        extent=(minx, maxx, miny, maxy),
        origin="lower",
        cmap="viridis",
        aspect="equal",
    )

    _draw_pcb_background(ax, pcb, pcb_to_plane=pcb_to_plane)

    ax.scatter(
        xs, ys, c=zs, cmap="viridis", edgecolor="white", s=60, label="Probe points"
    )

    ax.set_title(title)
    ax.legend(loc="upper right")
    fig.colorbar(heatmap, ax=ax, label="Z [mm]")
    save_png(fig, output_path)


def render_planned_points(
    planned_points: Sequence[Point2d],
    pcb: Board,
    title: str,
    output_path: Path,
) -> None:
    """計測予定の probe 点を基板背景に重ねて PNG 保存する.

    ``planned_points`` は基板座標 [mm]。図の Y 軸は上向き。
    """
    fig, ax = plt.subplots(figsize=(10, 8))
    _draw_pcb_background(ax, pcb)
    _draw_planned_hull(ax, planned_points)
    ax.scatter(
        [p.x for p in planned_points],
        [p.y for p in planned_points],
        c="red",
        marker="x",
        s=80,
        label="Planned probe points",
    )
    for idx, p in enumerate(planned_points):
        ax.annotate(
            str(idx),
            (p.x, p.y),
            textcoords="offset points",
            xytext=(5, 5),
            fontsize=8,
            color="darkred",
        )
    ax.set_title(title)
    ax.legend(loc="upper right")
    save_png(fig, output_path)


def _draw_pcb_background(
    ax: Axes, pcb: Board, pcb_to_plane: Transform = Identity()
) -> None:
    """銅箔 TOP 層・基板アウトライン・軸範囲/ラベルを ax に描画する."""
    for cu in pcb.copper:
        if cu.layer != Layer.TOP:
            continue
        ax.add_patch(
            polygon_with_holes_patch(
                transform_polygon(cu.polygon, pcb_to_plane),
                facecolor="#cc8844",
                edgecolor="#cc8844",
                alpha=0.3,
                linewidth=0.3,
            )
        )

    outline = transform_polygon(pcb.outline.polygon, pcb_to_plane)
    ax.add_patch(outline_patch(outline))

    minx, miny, maxx, maxy = outline.bounds
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_aspect("equal")
    ax.set_xlabel("X [mm]")
    ax.set_ylabel("Y [mm]")


def _draw_planned_hull(ax: Axes, planned_points: Sequence[Point2d]) -> None:
    """計測予定点の凸包を薄線で描く."""
    if len(planned_points) < 3:
        return
    hull = MultiPoint([(p.x, p.y) for p in planned_points]).convex_hull
    if not isinstance(hull, Polygon):
        return
    xs, ys = hull.exterior.xy
    ax.plot(xs, ys, color="red", alpha=0.35, linewidth=1.0, label="Probe hull")
