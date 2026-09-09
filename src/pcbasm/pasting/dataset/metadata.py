"""ペースト塗布画像 dataset の metadata.json（schema v2）.

DTO と strict な cattrs converter、:func:`parse_metadata` を置く。

永続化の判断:
    v2 は点塗布・銅板・セル格子前提の破壊的変更版で、v1（KiCad PCB の pad polygon +
    mask 前提）からの移行関数は用意しない。:func:`parse_metadata` は
    ``schema_version`` が現版と異なる doc を ``(None, 理由)`` で拒否する。将来キー/型を
    変える版が出たら旧版 dict を純関数 ``_migrate_vN(doc) -> dict`` で新版 dict へ
    写してから structure する。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Literal, get_args, get_origin, get_type_hints

import attrs
import cattrs

from pcbasm.geometry import Point2d
from pcbasm.geometry.packing import Rect
from pcbasm.pasting.dispense import DispenseSummary
from pcbasm.pasting.fill_path import AppliedDispenseMode
from pcbasm.utils import is_finite_number
from pcbasm.vision.image import PixelRect

type CapturePhase = Literal["pre", "post"]

type CaptureOrder = Literal["interleaved", "phased"]

# 現版の撮影順序。全点 pre 撮影 → パージ → 全点塗布 → 全点 post 撮影の 3 パスで回す。
# 分岐は持たないので固定値として記録するが、点ごとの interleave で収集した既存
# dataset も読めるよう、型としては両方を受ける
CAPTURE_ORDER: CaptureOrder = "phased"

METADATA_KIND = "pcbasm-paste-volume-dataset"
METADATA_SCHEMA_VERSION = 2


@attrs.frozen
class DatasetView:
    """同一セルを撮影する view 番号と基準位置からの offset."""

    number: int
    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0

    def validate(self) -> str | None:
        """View 番号が 0 以上の整数で offset が有限値かを検証する."""
        if type(self.number) is not int or self.number < 0:
            return f"view numberは0以上の整数が必要です: {self.number!r}"
        for name, value in (
            ("offset_x_mm", self.offset_x_mm),
            ("offset_y_mm", self.offset_y_mm),
        ):
            if not is_finite_number(value):
                return f"{name}は有限値が必要です: {value!r}"
        return None


def allocate_volume_by_rotations(
    total_volume_ul: float, rotations: Mapping[str, float]
) -> dict[str, float]:
    """計量した総体積を purge を含む正の吐出回転数比で配分する.

    Raises:
        ValueError: 総体積が正でない、回転数が空または正でない（呼び出し側の invariant）
    """
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
class DatasetCapturedView:
    """1 view の撮影位置、crop 矩形、保存相対 path."""

    number: int
    offset_x_mm: float
    offset_y_mm: float
    pixel_rect: PixelRect
    pre: str
    post: str


@attrs.frozen
class PasteDatasetSample:
    """学習 sample となる 1 セルの metadata.

    ``execution`` は塗布指令の実績集計（:class:`DispenseSummary`）、``order`` は
    セッション中の塗布実行順（流量ドリフトの post hoc 検出用）。
    """

    index: int
    order: int
    cell: Rect
    center: Point2d
    commanded_volume_ul: float
    volume_index: int
    execution: DispenseSummary
    measured_volume_ul: float
    views: tuple[DatasetCapturedView, ...]


@attrs.frozen
class PasteDatasetBlank:
    """塗布しない blank セルの metadata（真値 0 の sample）.

    塗布指令が無いので ``execution`` を持たず、``measured_volume_ul`` は常に 0.0。
    回転数比の体積配分にも含めない。
    """

    index: int
    cell: Rect
    center: Point2d
    measured_volume_ul: float
    views: tuple[DatasetCapturedView, ...]


@attrs.frozen
class PasteDatasetPurge:
    """画像 sample に含めない purge の metadata."""

    cell: Rect
    center: Point2d
    execution: DispenseSummary
    measured_volume_ul: float


@attrs.frozen
class PasteDatasetMachine:
    machine_id: str
    name: str | None


@attrs.frozen
class PasteDatasetPlate:
    """収集に使った銅板の寸法・外周余白と計測した板面 Z.

    ``camera.pixel_per_mm`` は camera calibration 時の Z のものなので、板面 Z との差
    （銅板厚み）から実効スケールを学習側で補正できるようにする。
    """

    width_mm: float
    height_mm: float
    edge_margin_mm: float
    height_plane_z_mm: float


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
    """装置の吐出設定と、点塗布収集固有の設定（セル格子・量スイープ・view・crop）."""

    rotations_per_ul: float
    max_dispense_rate_ul_s: float
    dispense_accel_ul_s2: float
    retract_amount_ul: float
    retract_rate_ul_s: float
    initial_purge_ul: float
    paste_height_mm: float
    prime_extra_delay_s: float
    cell_size_mm: float
    cell_gap_mm: float
    crop_size_mm: float
    crop_size_px: int
    purge_cell_size_mm: float
    volume_min_ul: float
    volume_max_ul: float
    volume_divisions: int
    samples_per_volume: int
    blank_count: int
    shuffle_seed: int
    view_count: int
    view_offset_mm: float
    capture_order: CaptureOrder


@attrs.frozen
class PasteDatasetLabel:
    """教師体積ラベルの作り方.

    ``rotation_allocated`` は「総質量を purge を含む指令回転数比で配分した」ラベルで、
    点ごとの実際のばらつきは含まない。将来点ごとの直接計量を入れる場合に区別できる
    ようにここへ記録する。
    """

    kind: Literal["rotation_allocated"]


@attrs.frozen
class PasteDatasetTotal:
    measured_mass_mg: float
    measured_volume_ul: float
    rotations: float


@attrs.frozen
class PasteDatasetMetadata:
    """Paste-volume-dataset metadata schema v2."""

    kind: Literal["pcbasm-paste-volume-dataset"]
    schema_version: Literal[2]
    created_at: str
    machine: PasteDatasetMachine
    plate: PasteDatasetPlate
    paste: PasteDatasetPaste
    camera: PasteDatasetCamera
    nozzle: PasteDatasetNozzle
    config: PasteDatasetConfig
    label: PasteDatasetLabel
    total: PasteDatasetTotal
    purge: PasteDatasetPurge
    samples: tuple[PasteDatasetSample, ...]
    blanks: tuple[PasteDatasetBlank, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON 互換 dict へ変換する."""
        return attrs.asdict(self)


def parse_metadata(
    data: Mapping[str, object],
) -> tuple[PasteDatasetMetadata | None, str | None]:
    """metadata.json の dict を schema_version で分岐して復元する.

    暗黙の型変換と未知 key は受理しない。現版（v2）以外の版は移行関数が無いため
    ``(None, 理由)`` を返す（将来版は ``_migrate_vN`` を追加して現版 dict に写す）。
    """
    version = data.get("schema_version")
    if version != METADATA_SCHEMA_VERSION:
        return None, f"未対応のmetadata schema_versionです: {version!r}"
    try:
        return _METADATA_CONVERTER.structure(data, PasteDatasetMetadata), None
    except Exception as error:
        return None, f"metadata schema v2が不正です: {error}"


def structure_document[T](data: Mapping[str, object], target: type[T]) -> T:
    """Schema DTO を暗黙変換・未知 key なしで復元する（不正は例外）.

    metadata v2 と、その質量未確定版（:mod:`pcbasm.pasting.dataset.pending`）が
    同じ strict converter を共有するための入口。

    Raises:
        Exception: cattrs の structure が失敗した場合（呼び出し側が理由文へ変換する）
    """
    return _METADATA_CONVERTER.structure(data, target)


def _make_metadata_converter() -> cattrs.Converter:
    """Schema 型定義を使い、暗黙変換なしで復元する converter を構成する."""
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

    def strict_applied_mode(value: object, _: object) -> object:
        # DispenseSummary.applied_mode: 塗布方式 literal / "mixed" / None
        if value is None or value in (*get_args(AppliedDispenseMode), "mixed"):
            return value
        raise ValueError(f"applied_modeが不正です: {value!r}")

    converter.register_structure_hook(float, strict_float)
    converter.register_structure_hook(int, strict_int)
    converter.register_structure_hook(str, strict_string)
    converter.register_structure_hook(
        get_type_hints(DispenseSummary)["applied_mode"], strict_applied_mode
    )
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
