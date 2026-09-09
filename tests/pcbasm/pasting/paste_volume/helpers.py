"""paste_volume のテスト用ヘルパー.

``tests.helpers`` は module 冒頭で ``pcbnew`` と ``picamera2`` を import するため、
それらが無い学習機・学習コンテナでは読めない。paste_volume のテストは装置を要求しないので、
ここへ必要なものだけを置いて ``tests.helpers`` から独立させる。この分離は
``test_architecture.py`` が機械検証する。

合成 session は :class:`~pcbasm.pasting.dataset.metadata.PasteDatasetMetadata` を組んで
書き出す。手書きの dict にすると収集 schema が変わったことに気づけない。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import attrs
import pytest
import torch
from torchvision.io import write_png

from pcbasm.geometry import Point2d
from pcbasm.geometry.packing import Rect
from pcbasm.pasting.dataset.metadata import (
    METADATA_KIND,
    METADATA_SCHEMA_VERSION,
    DatasetCapturedView,
    PasteDatasetBlank,
    PasteDatasetCamera,
    PasteDatasetConfig,
    PasteDatasetLabel,
    PasteDatasetMachine,
    PasteDatasetMetadata,
    PasteDatasetNozzle,
    PasteDatasetPaste,
    PasteDatasetPlate,
    PasteDatasetPurge,
    PasteDatasetSample,
    PasteDatasetTotal,
)
from pcbasm.pasting.dispense import DispenseSummary

PROJECT_ROOT = Path(__file__).resolve().parents[4]
PASTE_VOLUME_DATASET_DIR = PROJECT_ROOT / "data" / "paste-volume-datasets"

# 実データと同じ crop 寸法を使う。小さくすると ImageConstraints の下限に掛かって
# production と違う経路のテストになる。
CROP_SIZE_PX = 53

# 実データと同じ「周辺 view 数」。実際の view 数は中心 1 点を足した 5 になる。
PERIPHERAL_VIEW_COUNT = 4
VIEW_COUNT = PERIPHERAL_VIEW_COUNT + 1

PIXEL_PER_MM = 28.677782176153425
CELL_SIZE_MM = 1.8
CELL_GAP_MM = 0.4
VIEW_OFFSET_MM = 1.0

skip_if_no_real_sessions = pytest.mark.skipif(
    not PASTE_VOLUME_DATASET_DIR.is_dir()
    or not any(PASTE_VOLUME_DATASET_DIR.glob("*/metadata.json")),
    reason="実収集 session が無い（data/paste-volume-datasets は git 管理外）",
)


@attrs.frozen
class SyntheticCell:
    """合成 session に置く 1 セルの指定.

    ``commanded_volume_ul`` が ``None`` なら blank として書き出す。

    ``uniform`` は全 channel・全 view を同じ定数で埋める。前処理が分散 0 を理由に
    拒否する経路を試すために使う。
    """

    index: int
    commanded_volume_ul: float | None = 0.2
    x_mm: float = 0.5
    uniform: bool = False

    @property
    def is_blank(self) -> bool:
        return self.commanded_volume_ul is None


def write_session(
    root: Path,
    *,
    cells: tuple[SyntheticCell, ...],
    machine_id: str = "synthetic001",
    created_at: str = "2026-09-09T10:00:00+09:00",
    pixel_per_mm: float = PIXEL_PER_MM,
    crop_size_px: int = CROP_SIZE_PX,
    view_count: int = PERIPHERAL_VIEW_COUNT,
) -> Path:
    """合成 session を ``root`` へ書き出し、その path を返す."""

    (root / "pre").mkdir(parents=True, exist_ok=True)
    (root / "post").mkdir(parents=True, exist_ok=True)
    views = _view_geometry(view_count, crop_size_px)
    samples: list[PasteDatasetSample] = []
    blanks: list[PasteDatasetBlank] = []
    for cell in cells:
        captured = _write_cell_images(
            root, cell, views=views, crop_size_px=crop_size_px
        )
        rect = Rect(x=cell.x_mm, y=0.5, width=CELL_SIZE_MM, height=CELL_SIZE_MM)
        center = Point2d(rect.x + CELL_SIZE_MM / 2, rect.y + CELL_SIZE_MM / 2)
        if cell.is_blank:
            blanks.append(
                PasteDatasetBlank(
                    index=cell.index,
                    cell=rect,
                    center=center,
                    measured_volume_ul=0.0,
                    views=captured,
                )
            )
            continue
        commanded = cell.commanded_volume_ul
        assert commanded is not None
        samples.append(
            PasteDatasetSample(
                index=cell.index,
                order=len(samples) + 1,
                cell=rect,
                center=center,
                commanded_volume_ul=commanded,
                volume_index=len(samples),
                execution=_execution(commanded),
                measured_volume_ul=commanded,
                views=captured,
            )
        )
    metadata = _metadata(
        machine_id=machine_id,
        created_at=created_at,
        pixel_per_mm=pixel_per_mm,
        crop_size_px=crop_size_px,
        view_count=view_count,
        samples=tuple(samples),
        blanks=tuple(blanks),
    )
    (root / "metadata.json").write_text(
        json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return root


def corrupt_metadata(root: Path, mutate: Callable[[dict[str, Any]], None]) -> Path:
    """書き出し済み session の metadata.json を書き換える.

    壊れた session を作るのに使う。DTO を経由せず生の dict を触るのは、DTO では 表現できない不整合（存在しない
    path、重複した index）を作るため。
    """

    path = root / "metadata.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return root


def _view_geometry(
    view_count: int, crop_size_px: int
) -> tuple[tuple[int, float, float, tuple[int, int, int, int]], ...]:
    """中心 1 点 + 周辺 n 方向の view 番号・offset・pixel_rect を作る."""

    base_x, base_y = 600, 360
    geometry = [(0, 0.0, 0.0, _rect(base_x, base_y, crop_size_px))]
    for number in range(1, view_count + 1):
        radians = 2 * math.pi * (number - 1) / view_count
        offset_x = VIEW_OFFSET_MM * math.cos(radians)
        offset_y = VIEW_OFFSET_MM * math.sin(radians)
        left = base_x - round(offset_x * PIXEL_PER_MM)
        top = base_y - round(offset_y * PIXEL_PER_MM)
        geometry.append((number, offset_x, offset_y, _rect(left, top, crop_size_px)))
    return tuple(geometry)


def _rect(left: int, top: int, size: int) -> tuple[int, int, int, int]:
    return (left, top, left + size, top + size)


def _write_cell_images(
    root: Path,
    cell: SyntheticCell,
    *,
    views: tuple[tuple[int, float, float, tuple[int, int, int, int]], ...],
    crop_size_px: int,
) -> tuple[DatasetCapturedView, ...]:
    captured: list[DatasetCapturedView] = []
    for number, offset_x, offset_y, pixel_rect in views:
        pre_name = f"pre/{cell.index:06d}.{number:02d}.png"
        post_name = f"post/{cell.index:06d}.{number:02d}.png"
        write_png(_pre_image(cell, crop_size_px), str(root / pre_name))
        write_png(_post_image(cell, crop_size_px), str(root / post_name))
        captured.append(
            DatasetCapturedView(
                number=number,
                offset_x_mm=offset_x,
                offset_y_mm=offset_y,
                pixel_rect=pixel_rect,
                pre=pre_name,
                post=post_name,
            )
        )
    return tuple(captured)


def _pre_image(cell: SyntheticCell, size: int) -> torch.Tensor:
    """R だけが水平方向に変化し、G と B は定数の画像を返す.

    post と合わせて、channel 連結の順序（pre RGB が 0-2、post RGB が 3-5）を
    テストから観測できるようにする。
    """

    if cell.uniform:
        return torch.full((3, size, size), 128, dtype=torch.uint8)
    image = torch.empty((3, size, size), dtype=torch.uint8)
    columns = torch.arange(size, dtype=torch.uint8).expand(size, size)
    image[0] = columns
    image[1] = 64
    image[2] = 96
    return image


def _post_image(cell: SyntheticCell, size: int) -> torch.Tensor:
    """B だけが垂直方向に変化し、R と G は定数の画像を返す."""

    if cell.uniform:
        return torch.full((3, size, size), 128, dtype=torch.uint8)
    image = torch.empty((3, size, size), dtype=torch.uint8)
    rows = torch.arange(size, dtype=torch.uint8).unsqueeze(1).expand(size, size)
    image[0] = 32
    image[1] = 160
    image[2] = rows
    return image


def _execution(commanded_volume_ul: float) -> DispenseSummary:
    return DispenseSummary(
        applied_mode="dot",
        path_length_mm=0.0,
        commanded_volume_ul=commanded_volume_ul,
        prime_extra_volume_ul=0.0,
        effective_rate_ul_s=0.05,
        rotations=commanded_volume_ul * 15.0,
    )


def _metadata(
    *,
    machine_id: str,
    created_at: str,
    pixel_per_mm: float,
    crop_size_px: int,
    view_count: int,
    samples: tuple[PasteDatasetSample, ...],
    blanks: tuple[PasteDatasetBlank, ...],
) -> PasteDatasetMetadata:
    commanded_total = sum(sample.commanded_volume_ul for sample in samples)
    return PasteDatasetMetadata(
        kind=METADATA_KIND,
        schema_version=METADATA_SCHEMA_VERSION,
        created_at=created_at,
        machine=PasteDatasetMachine(machine_id=machine_id, name=None),
        plate=PasteDatasetPlate(
            width_mm=47.5, height_mm=20.0, edge_margin_mm=0.5, height_plane_z_mm=-34.2
        ),
        paste=PasteDatasetPaste(
            paste_id="S3X70-E150DN", lot=None, density_mg_per_ul=3.78
        ),
        camera=PasteDatasetCamera(
            pixel_per_mm=pixel_per_mm,
            resolution=(1280, 800),
            calibrated_at="2026-08-26T15:24:26.747567",
            z_position_mm=-25.0,
        ),
        nozzle=PasteDatasetNozzle(diameter_mm=0.3),
        config=PasteDatasetConfig(
            rotations_per_ul=15.0,
            max_dispense_rate_ul_s=0.05,
            dispense_accel_ul_s2=0.3,
            retract_amount_ul=0.03,
            retract_rate_ul_s=0.1,
            initial_purge_ul=0.2,
            paste_height_mm=0.2,
            prime_extra_delay_s=0.0,
            cell_size_mm=CELL_SIZE_MM,
            cell_gap_mm=CELL_GAP_MM,
            crop_size_mm=CELL_SIZE_MM,
            crop_size_px=crop_size_px,
            purge_cell_size_mm=CELL_SIZE_MM,
            volume_min_ul=0.05,
            volume_max_ul=0.35,
            volume_divisions=max(1, len(samples)),
            samples_per_volume=1,
            blank_count=len(blanks),
            shuffle_seed=1,
            view_count=view_count,
            view_offset_mm=VIEW_OFFSET_MM,
            capture_order="phased",
        ),
        label=PasteDatasetLabel(kind="rotation_allocated"),
        total=PasteDatasetTotal(
            measured_mass_mg=commanded_total * 3.78,
            measured_volume_ul=commanded_total,
            rotations=commanded_total * 15.0,
        ),
        purge=PasteDatasetPurge(
            cell=Rect(x=0.5, y=0.5, width=CELL_SIZE_MM, height=CELL_SIZE_MM),
            center=Point2d(1.4, 1.4),
            execution=_execution(0.2),
            measured_volume_ul=0.2,
        ),
        samples=samples,
        blanks=blanks,
    )


__all__ = [
    "CROP_SIZE_PX",
    "PASTE_VOLUME_DATASET_DIR",
    "PERIPHERAL_VIEW_COUNT",
    "PIXEL_PER_MM",
    "PROJECT_ROOT",
    "VIEW_COUNT",
    "SyntheticCell",
    "corrupt_metadata",
    "skip_if_no_real_sessions",
    "write_session",
]
