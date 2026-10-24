"""ペースト塗布画像 dataset の metadata.json（schema v3）.

DTO と :func:`parse_metadata` を置く。strict な変換は :mod:`pcbasm.document` が持つ。

永続化の判断:
    v3 はパージ廃止版で、収集の初回パージをインタラクティブローディングへ置き換えた。
    パージ領域が無くなったので格子セルはすべて計測可能点になり、教師体積の配分も
    塗布セルだけの回転数比になる。v2（パージあり）および v1（KiCad PCB の pad polygon +
    mask 前提）からの移行関数は用意しない。:func:`parse_metadata` は
    ``schema_version`` が現版と異なる doc を ``(None, 理由)`` で拒否する。将来キー/型を
    変える版が出たら旧版 dict を純関数 ``_migrate_vN(doc) -> dict`` で新版 dict へ
    写してから structure する。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import attrs

from pcbasm.document import parse_document
from pcbasm.geometry import Point2d
from pcbasm.geometry.packing import Rect
from pcbasm.pasting.dispense import DispenseSummary
from pcbasm.validators import is_finite_number
from pcbasm.vision.image import PixelRect

type CapturePhase = Literal["pre", "post"]

type CaptureOrder = Literal["phased"]

# 撮影順序。全点 pre 撮影 → ローディング → 全点塗布 → 全点 post 撮影の 3 パスで回す。
# 分岐は持たないので固定値として記録する
CAPTURE_ORDER: CaptureOrder = "phased"

METADATA_KIND = "pcbasm-paste-volume-dataset"
METADATA_SCHEMA_VERSION = 3


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
    """計量した総体積を正の吐出回転数比で配分する.

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
class PasteDatasetLoading:
    """塗布パス先頭で運転者が行ったインタラクティブローディングの実績.

    押し出したペーストは銅板の外へ廃棄するため、計量した増加質量には含まれない。
    教師体積の配分にも入らず、収集条件を後から追えるようにする記録として持つ。
    """

    total_ul: float
    total_rotations: float


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
    paste_height_mm: float
    prime_extra_delay_s: float
    cell_size_mm: float
    cell_gap_mm: float
    crop_size_mm: float
    crop_size_px: int
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

    ``rotation_allocated`` は「総質量を塗布セルの指令回転数比で配分した」ラベルで、
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
    """Paste-volume-dataset metadata schema v3."""

    kind: Literal["pcbasm-paste-volume-dataset"]
    schema_version: Literal[3]
    created_at: str
    machine: PasteDatasetMachine
    plate: PasteDatasetPlate
    paste: PasteDatasetPaste
    camera: PasteDatasetCamera
    nozzle: PasteDatasetNozzle
    config: PasteDatasetConfig
    label: PasteDatasetLabel
    total: PasteDatasetTotal
    loading: PasteDatasetLoading
    samples: tuple[PasteDatasetSample, ...]
    blanks: tuple[PasteDatasetBlank, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON 互換 dict へ変換する."""
        return attrs.asdict(self)


def parse_metadata(
    data: Mapping[str, object],
) -> tuple[PasteDatasetMetadata | None, str | None]:
    """metadata.json の dict を strict に復元する.

    暗黙の型変換と未知 key は受理しない。現版（v3）以外の版は移行関数が無いため
    ``(None, 理由)`` を返す（将来版は ``_migrate_vN`` を追加して現版 dict に写す）。
    """
    return parse_document(
        data,
        PasteDatasetMetadata,
        kind=METADATA_KIND,
        version=METADATA_SCHEMA_VERSION,
        label="dataset",
    )
