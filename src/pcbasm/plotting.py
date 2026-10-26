"""Matplotlib で PNG を描く部品（Agg の設定、穴付きポリゴン、基板外形、保存）."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.patches import PathPatch, Polygon as MplPolygon
from matplotlib.path import Path as MplPath
from shapely import Polygon


def polygon_with_holes_patch(
    polygon: Polygon,
    *,
    facecolor: str,
    edgecolor: str,
    alpha: float,
    linewidth: float,
) -> PathPatch:
    """穴付きポリゴンを PathPatch 化する.

    matplotlib.Polygon は hole をサポートしないため Path 経由で生成する。 matplotlib は
    exterior と逆向きの巻きを穴と解釈するため、interiors は反転する。
    """
    move_to, line_to, close_poly = (
        int(MplPath.MOVETO),
        int(MplPath.LINETO),
        int(MplPath.CLOSEPOLY),
    )
    verts: list[tuple[float, float]] = []
    codes: list[int] = []
    rings = [list(polygon.exterior.coords)] + [
        list(h.coords)[::-1] for h in polygon.interiors
    ]
    for ring in rings:
        if len(ring) < 3:
            continue
        verts.extend(ring)
        verts.append(ring[0])
        codes.append(move_to)
        codes.extend([line_to] * (len(ring) - 1))
        codes.append(close_poly)

    return PathPatch(
        MplPath(verts, codes),
        facecolor=facecolor,
        edgecolor=edgecolor,
        alpha=alpha,
        linewidth=linewidth,
    )


def outline_patch(polygon: Polygon) -> MplPolygon:
    """基板外形を白の破線で描く patch."""
    return MplPolygon(
        list(polygon.exterior.coords),
        closed=True,
        facecolor="none",
        edgecolor="#ffffff",
        linewidth=1.5,
        linestyle="--",
    )


def save_png(
    fig: Figure, output_path: Path, *, dpi: int = 120, facecolor: str = "auto"
) -> None:
    """余白を詰めて PNG で保存し、figure を閉じる."""
    fig.tight_layout()
    fig.savefig(output_path, dpi=dpi, facecolor=facecolor)
    plt.close(fig)
