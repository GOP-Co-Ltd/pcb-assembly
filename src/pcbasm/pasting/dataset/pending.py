"""計量質量だけが未確定の dataset metadata（``pending.json``、schema v1）.

収集は「全点 pre 撮影 → ローディング → 全点塗布 → 全点 post 撮影 → 計量質量を入力」の
順で進み、最後の入力に応答できないと :class:`~.metadata.PasteDatasetMetadata` を組めない。
セル配置・量割り当て（``shuffle_seed``）・実行 rotations はプロセスメモリ上にしか無いので、
そこで落ちると撮影済み画像が教師値を失う。

``pending.json`` は metadata v3 から「計量質量に依存する 2 つの値」だけを抜いた doc で、
入力を待つ前に session へ書く。抜くのは ``total`` と sample の ``measured_volume_ul``
（blank は常に 0.0 なので質量に依存しない）。
:func:`finalize_pending` が計量質量 1 つでこれを metadata v3 へ戻す。

永続化の判断:
    metadata v3 と同じ DTO / converter を共有し、pending 固有の型は「質量依存の値を
    持たない sample」の 1 つだけにする。復元経路が本経路と同じ組立を通るので、
    救出した dataset が本経路の出力と一致することを型で担保できる。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import attrs

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
    PasteDatasetLoading,
    PasteDatasetMachine,
    PasteDatasetMetadata,
    PasteDatasetNozzle,
    PasteDatasetPaste,
    PasteDatasetPlate,
    PasteDatasetSample,
    PasteDatasetTotal,
    allocate_volume_by_rotations,
    structure_document,
)
from pcbasm.pasting.dispense import DispenseSummary
from pcbasm.utils import is_finite_number

# 未完了 session に残す pending doc のファイル名
PENDING_FILENAME = "pending.json"

PENDING_KIND = "pcbasm-paste-volume-dataset-pending"
PENDING_SCHEMA_VERSION = 1

# 体積配分の識別子。sample の 1 起点 index から作る。


def sample_key(index: int) -> str:
    """体積配分での sample 識別子."""
    return f"sample-{index}"


@attrs.frozen
class PasteDatasetPendingSample:
    """計量質量が未確定な 1 sample.

    :class:`~.metadata.PasteDatasetSample` から ``measured_volume_ul`` を除いたもの。
    """

    index: int
    order: int
    cell: Rect
    center: Point2d
    commanded_volume_ul: float
    volume_index: int
    execution: DispenseSummary
    views: tuple[DatasetCapturedView, ...]


@attrs.frozen
class PasteDatasetPending:
    """Pending schema v1（計量質量を待つ dataset の全内容）."""

    kind: Literal["pcbasm-paste-volume-dataset-pending"]
    schema_version: Literal[1]
    created_at: str
    machine: PasteDatasetMachine
    plate: PasteDatasetPlate
    paste: PasteDatasetPaste
    camera: PasteDatasetCamera
    nozzle: PasteDatasetNozzle
    config: PasteDatasetConfig
    label: PasteDatasetLabel
    loading: PasteDatasetLoading
    samples: tuple[PasteDatasetPendingSample, ...]
    blanks: tuple[PasteDatasetBlank, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON 互換 dict へ変換する."""
        return attrs.asdict(self)


def parse_pending(
    data: Mapping[str, object],
) -> tuple[PasteDatasetPending | None, str | None]:
    """``pending.json`` の dict を復元する（暗黙の型変換と未知 key は受理しない）."""
    version = data.get("schema_version")
    if version != PENDING_SCHEMA_VERSION:
        return None, f"未対応のpending schema_versionです: {version!r}"
    try:
        return structure_document(data, PasteDatasetPending), None
    except Exception as error:
        return None, f"pending schema v1が不正です: {error}"


def finalize_pending(
    pending: PasteDatasetPending, *, measured_mass_mg: float
) -> tuple[PasteDatasetMetadata | None, str | None]:
    """計量質量を回転数比で配分し、metadata v3 を確定する.

    配分は塗布セルの指令回転数比で行う（収集本経路と同じ規則）。

    ローディング分は銅板の外へ廃棄するので計量値に含まれず、配分にも入らない。

    blank は塗布指令が無いので配分に加わらず、真値 0.0 のままになる。
    """
    if not is_finite_number(measured_mass_mg) or measured_mass_mg <= 0:
        return None, f"計量質量は正の有限値が必要です: {measured_mass_mg!r}"
    density = pending.paste.density_mg_per_ul
    if density <= 0:
        return None, f"ペースト密度は正の値が必要です: {density!r}"
    measured_volume_ul = measured_mass_mg / density
    rotations = {
        sample_key(sample.index): sample.execution.rotations
        for sample in pending.samples
    }
    try:
        allocated = allocate_volume_by_rotations(measured_volume_ul, rotations)
    except ValueError as error:
        return None, f"計量質量を配分できません: {error}"
    return (
        PasteDatasetMetadata(
            kind=METADATA_KIND,
            schema_version=METADATA_SCHEMA_VERSION,
            created_at=pending.created_at,
            machine=pending.machine,
            plate=pending.plate,
            paste=pending.paste,
            camera=pending.camera,
            nozzle=pending.nozzle,
            config=pending.config,
            label=pending.label,
            loading=pending.loading,
            total=PasteDatasetTotal(
                measured_mass_mg=measured_mass_mg,
                measured_volume_ul=measured_volume_ul,
                rotations=sum(rotations.values()),
            ),
            samples=tuple(
                PasteDatasetSample(
                    index=sample.index,
                    order=sample.order,
                    cell=sample.cell,
                    center=sample.center,
                    commanded_volume_ul=sample.commanded_volume_ul,
                    volume_index=sample.volume_index,
                    execution=sample.execution,
                    measured_volume_ul=allocated[sample_key(sample.index)],
                    views=sample.views,
                )
                for sample in pending.samples
            ),
            blanks=pending.blanks,
        ),
        None,
    )
