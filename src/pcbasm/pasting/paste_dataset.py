"""ペースト塗布画像datasetの生成と永続化."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

import attrs
import cattrs
import cv2
import numpy as np
from shapely import Polygon
from shapely.coords import CoordinateSequence

from pcbasm.utils import is_finite_number
from pcbasm.vision import Image, ImageArray

type CapturePhase = Literal["pre", "post"]
type PixelRect = tuple[int, int, int, int]


@attrs.frozen
class DatasetView:
    """同一padを撮影するview番号と基準位置からのoffset."""

    number: int
    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0

    def __attrs_post_init__(self) -> None:
        if type(self.number) is not int or self.number < 0:
            raise ValueError(f"view numberは0以上の整数が必要です: {self.number!r}")
        for name, value in (
            ("offset_x_mm", self.offset_x_mm),
            ("offset_y_mm", self.offset_y_mm),
        ):
            if not is_finite_number(value):
                raise ValueError(f"{name}は有限値が必要です: {value!r}")


@attrs.frozen
class PadImageCrop:
    """pad周辺のRGB画像、同寸法mask、全frame上のcrop矩形."""

    image: ImageArray = attrs.field(eq=False)
    mask: ImageArray = attrs.field(eq=False)
    pixel_rect: PixelRect


def _ring_pixels(
    coordinates: CoordinateSequence,
    matrix: ImageArray,
    shift: ImageArray,
    origin: tuple[int, int],
) -> ImageArray:
    points = np.asarray(coordinates, dtype=np.float64)
    pixels = points @ matrix.T + shift - np.asarray(origin, dtype=np.float64)
    return np.round(pixels).astype(np.int32).reshape(-1, 1, 2)


def validate_dataset_image_margins(
    crop_margin_mm: float, mask_margin_mm: float
) -> str | None:
    """画像cropとpad maskの余白設定を検証する."""
    for name, value in (
        ("crop_margin_mm", crop_margin_mm),
        ("mask_margin_mm", mask_margin_mm),
    ):
        if not is_finite_number(value) or value < 0:
            return f"{name}は0以上の有限値が必要です: {value!r}"
    if mask_margin_mm > crop_margin_mm:
        return "mask_margin_mmはcrop_margin_mm以下にしてください"
    return None


def crop_pad_image(
    image: Image | ImageArray,
    polygon: Polygon,
    matrix: ImageArray,
    shift: ImageArray,
    *,
    margin_mm: float,
    mask_margin_mm: float,
) -> PadImageCrop:
    """F.Paste polygonのAABBにmarginを足し、buffer付きmaskとRGB cropを返す.

    ``matrix`` / ``shift`` は
    :meth:`pcbasm.posctrl.CopperProjector.board_to_pixel_affine` の戻り値を
    そのまま受け取る。cropがframe外へ出る場合はpaddingせず失敗させる。
    """
    margin_error = validate_dataset_image_margins(margin_mm, mask_margin_mm)
    if margin_error is not None:
        raise ValueError(margin_error)
    if polygon.is_empty or not polygon.is_valid:
        raise ValueError("crop対象polygonが空または不正です")

    source = image.numpy() if isinstance(image, Image) else np.asarray(image)
    if source.ndim != 3 or source.shape[2] != 3:
        raise ValueError(f"RGB画像は3 channelが必要です: shape={source.shape}")
    affine = np.asarray(matrix, dtype=np.float64)
    translation = np.asarray(shift, dtype=np.float64)
    if affine.shape != (2, 2) or translation.shape != (2,):
        raise ValueError("board→pixel affineはmatrix=(2, 2), shift=(2,)が必要です")
    if not np.isfinite(affine).all() or not np.isfinite(translation).all():
        raise ValueError("board→pixel affineには有限値が必要です")

    min_x, min_y, max_x, max_y = polygon.bounds
    bounds = np.asarray(
        [
            [min_x - margin_mm, min_y - margin_mm],
            [max_x + margin_mm, min_y - margin_mm],
            [max_x + margin_mm, max_y + margin_mm],
            [min_x - margin_mm, max_y + margin_mm],
        ],
        dtype=np.float64,
    )
    projected = bounds @ affine.T + translation
    x0 = math.floor(float(projected[:, 0].min()))
    y0 = math.floor(float(projected[:, 1].min()))
    x1 = math.ceil(float(projected[:, 0].max()))
    y1 = math.ceil(float(projected[:, 1].max()))
    height, width = source.shape[:2]
    if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
        raise ValueError(
            f"pad crop {x0, y0, x1, y1} がcamera frame "
            f"{width, height} に収まりません"
        )
    if x0 >= x1 or y0 >= y1:
        raise ValueError(f"pad cropが空です: {x0, y0, x1, y1}")

    crop = source[y0:y1, x0:x1].copy()
    mask = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    mask_polygon = polygon if mask_margin_mm == 0 else polygon.buffer(mask_margin_mm)
    if not isinstance(mask_polygon, Polygon):
        raise ValueError("buffer後のmask polygonが不正です")
    exterior = _ring_pixels(mask_polygon.exterior.coords, affine, translation, (x0, y0))
    cv2.fillPoly(mask, [exterior], 255)
    holes = [
        _ring_pixels(ring.coords, affine, translation, (x0, y0))
        for ring in mask_polygon.interiors
    ]
    if holes:
        cv2.fillPoly(mask, holes, 0)
    return PadImageCrop(image=crop, mask=mask, pixel_rect=(x0, y0, x1, y1))


def allocate_volume_by_rotations(
    total_volume_ul: float, rotations: Mapping[str, float]
) -> dict[str, float]:
    """計量した総体積をpurgeを含む正の吐出回転数比で配分する."""
    if not is_finite_number(total_volume_ul) or total_volume_ul <= 0:
        raise ValueError(f"total_volume_ulは正の有限値が必要です: {total_volume_ul!r}")
    if not rotations:
        raise ValueError("rotationsが空です")
    checked: dict[str, float] = {}
    for key, value in rotations.items():
        if not key:
            raise ValueError("rotationの識別子を空にできません")
        if not is_finite_number(value) or value <= 0:
            raise ValueError(f"rotationは正の有限値が必要です: {key}={value!r}")
        checked[key] = float(value)
    total_rotations = sum(checked.values())
    return {
        key: float(total_volume_ul) * value / total_rotations
        for key, value in checked.items()
    }


@attrs.frozen
class DatasetPolygon:
    """JSON保存可能なpad polygon."""

    exterior: tuple[tuple[float, float], ...]
    holes: tuple[tuple[tuple[float, float], ...], ...] = ()

    @classmethod
    def from_polygon(cls, polygon: Polygon) -> DatasetPolygon:
        return cls(
            exterior=tuple((float(x), float(y)) for x, y in polygon.exterior.coords),
            holes=tuple(
                tuple((float(x), float(y)) for x, y in ring.coords)
                for ring in polygon.interiors
            ),
        )


@attrs.frozen
class DatasetResolvedPaste:
    """padへ適用した解決済み塗布設定snapshot."""

    dispense_mode: str
    line_direction: str
    paste_height: float | str
    ul_per_mm2: float
    prime_extra_delay: float
    bead_width_factor: float
    overlap: float
    boundary_margin: float


@attrs.frozen
class DatasetExecution:
    """1 padまたはpurgeの塗布指令結果."""

    applied_mode: str | None
    path_length_mm: float
    commanded_volume_ul: float
    prime_extra_volume_ul: float
    effective_rate_ul_s: float
    rotations: float


@attrs.frozen
class DatasetCapturedView:
    """1 viewの撮影位置、crop矩形、保存相対path."""

    number: int
    offset_x_mm: float
    offset_y_mm: float
    pixel_rect: PixelRect
    pre: str
    post: str
    mask: str


@attrs.frozen
class PasteDatasetPad:
    """学習sampleとなる1 padのmetadata."""

    index: int
    pad_id: str
    source_pad_id: str
    polygon: DatasetPolygon
    resolved: DatasetResolvedPaste
    execution: DatasetExecution
    measured_volume_ul: float
    views: tuple[DatasetCapturedView, ...]


@attrs.frozen
class PasteDatasetPurge:
    """画像sampleに含めないpurgeのmetadata."""

    pad_id: str
    source_pad_id: str
    execution: DatasetExecution
    measured_volume_ul: float


@attrs.frozen
class PasteDatasetMachine:
    machine_id: str
    name: str | None


@attrs.frozen
class PasteDatasetBoard:
    filename: str
    source_pcb: str
    signature: str


@attrs.frozen
class PasteDatasetPaste:
    paste_id: str
    lot: str | None
    density_mg_per_ul: float


@attrs.frozen
class PasteDatasetCamera:
    pixel_per_mm: float
    resolution: tuple[int, int]
    calibrated_at: str
    z_position_mm: float | None


@attrs.frozen
class PasteDatasetNozzle:
    diameter_mm: float


@attrs.frozen
class PasteDatasetConfig:
    rotations_per_ul: float
    max_fill_speed_mm_s: float
    max_dispense_rate_ul_s: float
    dispense_accel_ul_s2: float
    retract_amount_ul: float
    retract_rate_ul_s: float
    initial_purge_ul: float
    crop_margin_mm: float
    mask_margin_mm: float


@attrs.frozen
class PasteDatasetTotal:
    measured_mass_mg: float
    measured_volume_ul: float
    rotations: float


@attrs.frozen
class PasteDatasetMetadata:
    """Paste-volume-dataset metadata schema v1."""

    kind: Literal["pcbasm-paste-volume-dataset"]
    schema_version: Literal[1]
    created_at: str
    machine: PasteDatasetMachine
    board: PasteDatasetBoard
    paste: PasteDatasetPaste
    camera: PasteDatasetCamera
    nozzle: PasteDatasetNozzle
    config: PasteDatasetConfig
    total: PasteDatasetTotal
    purge: PasteDatasetPurge
    pads: tuple[PasteDatasetPad, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON互換dictへ変換する."""
        return attrs.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PasteDatasetMetadata:
        """Schema v1を暗黙変換せず復元する（未知keyも拒否）."""
        try:
            return _METADATA_CONVERTER.structure(data, cls)
        except Exception as error:
            raise ValueError(f"metadata schema v1が不正です: {error}") from error


def _make_metadata_converter() -> cattrs.Converter:
    """schema型定義を使い、暗黙変換なしで復元するconverterを構成する."""
    converter = cattrs.Converter(
        forbid_extra_keys=True,
        detailed_validation=False,
    )

    def strict_float(value: object, _: object) -> float:
        if type(value) is not float or not math.isfinite(value):
            raise ValueError(f"有限なfloatが必要です: {value!r}")
        return value

    def strict_int(value: object, _: object) -> int:
        if type(value) is not int:
            raise ValueError(f"intが必要です: {value!r}")
        return value

    def strict_string(value: object, _: object) -> str:
        if type(value) is not str:
            raise ValueError(f"strが必要です: {value!r}")
        return value

    def strict_literal(value: object, target: object) -> object:
        if any(
            type(value) is type(expected) and value == expected
            for expected in get_args(target)
        ):
            return value
        raise ValueError(f"{target!r}の値が必要です: {value!r}")

    def strict_tuple(value: object, target: object) -> tuple[object, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"arrayが必要です: {value!r}")
        item_types = get_args(target)
        if len(item_types) == 2 and item_types[1] is Ellipsis:
            return tuple(converter.structure(item, item_types[0]) for item in value)
        if len(value) != len(item_types):
            raise ValueError(f"{len(item_types)}要素のarrayが必要です: {value!r}")
        return tuple(
            converter.structure(item, item_type)
            for item, item_type in zip(value, item_types, strict=True)
        )

    def strict_paste_height(value: object, _: object) -> float | str:
        if type(value) is float and math.isfinite(value):
            return value
        if type(value) is str and value == "auto":
            return value
        raise ValueError(f"paste_heightは有限なfloatまたはautoが必要です: {value!r}")

    converter.register_structure_hook(float, strict_float)
    converter.register_structure_hook(int, strict_int)
    converter.register_structure_hook(str, strict_string)
    converter.register_structure_hook(float | str, strict_paste_height)
    converter.register_structure_hook_func(
        lambda target: get_origin(target) is Literal,
        strict_literal,
    )
    converter.register_structure_hook_func(
        lambda target: get_origin(target) is tuple,
        strict_tuple,
    )
    return converter


_METADATA_CONVERTER = _make_metadata_converter()


class PasteDatasetWriter:
    """一時sessionへ画像を書き、完成またはincompleteへatomic確定する."""

    def __init__(
        self,
        root: Path,
        *,
        board_name: str,
        started_at: datetime | None = None,
    ) -> None:
        timestamp = started_at or datetime.now().astimezone()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("started_atにはtimezoneが必要です")
        if not board_name:
            raise ValueError("board_nameは空にできません")
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        milliseconds = timestamp.microsecond // 1000
        stem = (
            f"{board_name}-{timestamp.strftime('%Y%m%dT%H%M%S')}"
            f".{milliseconds:03d}{timestamp.strftime('%z')}"
        )
        self._stem = self._available_stem(stem)
        self._working_path = self._root / f".{self._stem}.tmp"
        self._working_path.mkdir()
        for phase in ("pre", "post", "mask"):
            (self._working_path / phase).mkdir()
        self._finished_path: Path | None = None
        self._captures: set[tuple[int, int, CapturePhase]] = set()

    @property
    def working_path(self) -> Path:
        """書き込み中のsession path."""
        return self._working_path

    def write_capture(
        self,
        pad_index: int,
        view: DatasetView,
        phase: CapturePhase,
        crop: PadImageCrop,
    ) -> DatasetCapturedView:
        """Lossless PNGを書き、metadata用のview記述を返す."""
        self._ensure_open()
        if type(pad_index) is not int or pad_index < 1:
            raise ValueError(f"pad_indexは1以上の整数が必要です: {pad_index!r}")
        if phase not in ("pre", "post"):
            raise ValueError(f"未知のcapture phaseです: {phase!r}")
        key = (pad_index, view.number, phase)
        if key in self._captures:
            raise ValueError(f"captureが重複しています: {key}")
        self._validate_crop(crop)

        filename = f"{pad_index:06d}.{view.number:02d}.png"
        relative = Path(phase) / filename
        mask_relative = Path("mask") / filename
        mask_path = self._working_path / mask_relative
        if mask_path.exists():
            existing = cv2.imread(str(mask_path), cv2.IMREAD_UNCHANGED)
            if existing is None or not np.array_equal(existing, crop.mask):
                raise ValueError(f"同一viewのmaskが一致しません: {filename}")
        else:
            self._write_png(mask_path, crop.mask)
        self._write_png(self._working_path / relative, crop.image)
        self._captures.add(key)
        return DatasetCapturedView(
            number=view.number,
            offset_x_mm=view.offset_x_mm,
            offset_y_mm=view.offset_y_mm,
            pixel_rect=crop.pixel_rect,
            pre=(Path("pre") / filename).as_posix(),
            post=(Path("post") / filename).as_posix(),
            mask=mask_relative.as_posix(),
        )

    def finalize(self, metadata: PasteDatasetMetadata) -> Path:
        """Metadataを書き、完成session名へatomic renameする."""
        self._ensure_open()
        expected: set[tuple[int, int, CapturePhase]] = set()
        pad_indices: set[int] = set()
        for pad in metadata.pads:
            if pad.index in pad_indices:
                raise ValueError(f"metadataのpad indexが重複しています: {pad.index}")
            pad_indices.add(pad.index)
            view_numbers: set[int] = set()
            for view in pad.views:
                if view.number in view_numbers:
                    raise ValueError(
                        f"metadataのview numberが重複しています: "
                        f"pad={pad.index}, view={view.number}"
                    )
                view_numbers.add(view.number)
                expected.add((pad.index, view.number, "pre"))
                expected.add((pad.index, view.number, "post"))
                filename = f"{pad.index:06d}.{view.number:02d}.png"
                if (
                    view.pre != f"pre/{filename}"
                    or view.post != f"post/{filename}"
                    or view.mask != f"mask/{filename}"
                ):
                    raise ValueError(
                        f"metadataのcapture pathが命名規則と一致しません: {filename}"
                    )
        missing = expected - self._captures
        if missing:
            raise ValueError(
                f"metadataが参照するcaptureが不足しています: {sorted(missing)}"
            )
        metadata_path = self._working_path / "metadata.json"
        metadata_path.write_text(
            json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        destination = self._root / self._stem
        os.replace(self._working_path, destination)
        self._finished_path = destination
        return destination

    def mark_incomplete(self) -> Path:
        """取得済みファイルを保持したままincomplete sessionへ確定する."""
        if self._finished_path is not None:
            return self._finished_path
        destination = self._root / f"{self._stem}.incomplete"
        os.replace(self._working_path, destination)
        self._finished_path = destination
        return destination

    def _available_stem(self, base: str) -> str:
        suffix = 0
        while True:
            stem = base if suffix == 0 else f"{base}-{suffix}"
            candidates = (
                self._root / stem,
                self._root / f"{stem}.incomplete",
                self._root / f".{stem}.tmp",
            )
            if not any(path.exists() for path in candidates):
                return stem
            suffix += 1

    def _ensure_open(self) -> None:
        if self._finished_path is not None:
            raise RuntimeError(f"dataset sessionは確定済みです: {self._finished_path}")

    @staticmethod
    def _validate_crop(crop: PadImageCrop) -> None:
        if crop.image.ndim != 3 or crop.image.shape[2] != 3:
            raise ValueError(f"capture画像は3 channelが必要です: {crop.image.shape}")
        if crop.image.dtype != np.uint8:
            raise ValueError(f"capture画像はuint8が必要です: {crop.image.dtype}")
        if crop.mask.shape != crop.image.shape[:2] or crop.mask.dtype != np.uint8:
            raise ValueError("maskはcapture画像と同寸法のuint8が必要です")
        if not np.isin(crop.mask, (0, 255)).all():
            raise ValueError("maskは0/255だけで構成する必要があります")

    @staticmethod
    def _write_png(path: Path, data: ImageArray) -> None:
        if not cv2.imwrite(str(path), data, [cv2.IMWRITE_PNG_COMPRESSION, 3]):
            raise OSError(f"PNGを書き込めません: {path}")
