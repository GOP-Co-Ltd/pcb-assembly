"""HeightPlane ヒートマップと probe 計画点の PNG レンダリング."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.axes import Axes
from matplotlib.patches import Polygon as MplPolygon
from shapely.geometry import MultiPoint, Polygon as ShapelyPolygon

from pcbasm.geometry import (
    HeightPlane,
    Identity,
    Point2d,
    Point3d,
    Transform,
    transform_polygon,
)
from pcbasm.pcb import Layer, PcbFile

from .patches import polygon_with_holes_patch

_MESH_RESOLUTION = 50


def render_height_plane(
    height_plane: HeightPlane,
    pcb: PcbFile,
    title: str,
    output_path: Path,
    pcb_to_plane: Transform = Identity(),
) -> None:
    """HeightPlane の高さを 2D ヒートマップ・基板背景と重ねて PNG 保存する.

    ``pcb_to_plane`` は PCB 背景を HeightPlane と同じ XY 座標系へ写す変換。
    """
    xs = [p.x for p in height_plane.points]
    ys = [p.y for p in height_plane.points]
    zs = [p.z for p in height_plane.points]

    # z=0で入力するとapply後のz値が補間値そのものになる
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
    plt.tight_layout()
    fig.savefig(output_path, dpi=120)
    plt.close(fig)


def render_planned_points(
    planned_points: Sequence[Point2d],
    pcb: PcbFile,
    title: str,
    output_path: Path,
) -> None:
    """計測予定の probe 点を基板背景に重ねて PNG 保存する."""
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
    plt.tight_layout()
    fig.savefig(output_path, dpi=120)
    plt.close(fig)


def _draw_pcb_background(
    ax: Axes, pcb: PcbFile, pcb_to_plane: Transform = Identity()
) -> None:
    """銅箔TOP層・基板アウトライン・軸範囲/ラベルを ax に描画する."""
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
    outline_coords = list(outline.exterior.coords)
    ax.add_patch(
        MplPolygon(
            outline_coords,
            closed=True,
            facecolor="none",
            edgecolor="#ffffff",
            linewidth=1.5,
            linestyle="--",
        )
    )

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
    if not isinstance(hull, ShapelyPolygon):
        return
    xs, ys = hull.exterior.xy
    ax.plot(xs, ys, color="red", alpha=0.35, linewidth=1.0, label="Probe hull")
