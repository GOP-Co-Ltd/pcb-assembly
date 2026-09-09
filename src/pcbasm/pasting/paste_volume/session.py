"""1 収集 session の読み込みと構造の検証.

``metadata.json`` の解析、画像 path の解決、session fingerprint の算出までを担う。
画像そのものの検証（寸法の一致、前処理を通せるか）は :mod:`pcbasm.pasting.paste_volume.index`
が行う。ここで decode すると index の判定で同じ PNG を二度読むことになる。

収集 schema は :mod:`pcbasm.pasting.dataset.metadata` が唯一の出典で、ここでは再定義しない。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

import attrs

from ml.artifact.fingerprint import fingerprint_json, sha256_file
from pcbasm.geometry.packing import Rect
from pcbasm.pasting.dataset.metadata import (
    DatasetCapturedView,
    PasteDatasetBlank,
    PasteDatasetMetadata,
    PasteDatasetSample,
    parse_metadata,
)

METADATA_FILE_NAME = "metadata.json"


@attrs.frozen
class PasteVolumeCell:
    """学習 sample 1 件に対応する収集セル.

    ``samples`` と ``blanks`` を同じ形で扱うための正規化。blank は塗布指令が無いので
    ``order`` と ``commanded_volume_ul`` を持たない。
    """

    index: int
    order: int | None
    is_blank: bool
    commanded_volume_ul: float | None
    measured_volume_ul: float
    cell: Rect
    views: tuple[DatasetCapturedView, ...]

    @classmethod
    def from_sample(cls, sample: PasteDatasetSample) -> Self:
        """塗布した sample から作る."""

        return cls(
            index=sample.index,
            order=sample.order,
            is_blank=False,
            commanded_volume_ul=sample.commanded_volume_ul,
            measured_volume_ul=sample.measured_volume_ul,
            cell=sample.cell,
            views=sample.views,
        )

    @classmethod
    def from_blank(cls, blank: PasteDatasetBlank) -> Self:
        """塗布しなかった blank から作る."""

        return cls(
            index=blank.index,
            order=None,
            is_blank=True,
            commanded_volume_ul=None,
            measured_volume_ul=blank.measured_volume_ul,
            cell=blank.cell,
            views=blank.views,
        )


@attrs.frozen
class PasteVolumeSession:
    """検証済みの 1 収集 session.

    ``session_fingerprint`` は metadata と全画像の内容だけから決まる。session を別の
    場所へ展開しても directory を rename しても変わらない。
    """

    root: Path
    label: str
    metadata: PasteDatasetMetadata
    cells: tuple[PasteVolumeCell, ...]
    session_fingerprint: str

    @classmethod
    def load(cls, root: Path) -> tuple[Self | None, str | None]:
        """収集 session を読み、構造を検証して返す."""

        metadata, error = _load_metadata(root)
        if metadata is None:
            return None, error
        cells = tuple(
            [PasteVolumeCell.from_sample(sample) for sample in metadata.samples]
            + [PasteVolumeCell.from_blank(blank) for blank in metadata.blanks]
        )
        if error := _validate_cells(cells, pixel_per_mm=metadata.camera.pixel_per_mm):
            return None, error
        relative_paths, error = _resolved_image_paths(root, cells)
        if relative_paths is None:
            return None, error
        return (
            cls(
                root=root,
                label=root.name,
                metadata=metadata,
                cells=cells,
                session_fingerprint=_session_fingerprint(root, relative_paths),
            ),
            None,
        )

    def image_path(self, relative: str) -> Path:
        """相対 path を session 内の絶対 path へ直す.

        安全性と実在は :meth:`load` が確認済みなので、ここでは連結だけを行う。
        """

        return self.root / relative

    @property
    def pixel_per_mm(self) -> float:
        """収集時の解像度 [px/mm]."""

        return self.metadata.camera.pixel_per_mm


def _load_metadata(root: Path) -> tuple[PasteDatasetMetadata | None, str | None]:
    path = root / METADATA_FILE_NAME
    if not path.is_file():
        return None, f"{METADATA_FILE_NAME} が見つかりません: {root}"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, f"{METADATA_FILE_NAME} を読めません: {error}"
    if not isinstance(document, dict):
        return (
            None,
            f"{METADATA_FILE_NAME} の最上位は object が必要です: {type(document)}",
        )
    return parse_metadata(document)


def _validate_cells(
    cells: tuple[PasteVolumeCell, ...], *, pixel_per_mm: float
) -> str | None:
    if not cells:
        return "学習に使える cell がありません"
    if pixel_per_mm <= 0:
        return f"camera.pixel_per_mm は正の値が必要です: {pixel_per_mm}"
    indices = [cell.index for cell in cells]
    if len(set(indices)) != len(indices):
        duplicated = sorted({index for index in indices if indices.count(index) > 1})
        return f"cell の index が重複しています: {duplicated}"
    for cell in cells:
        if not cell.views:
            return f"cell {cell.index} に view がありません"
        numbers = [view.number for view in cell.views]
        if len(set(numbers)) != len(numbers):
            return f"cell {cell.index} の view number が重複しています: {numbers}"
        if cell.is_blank and cell.measured_volume_ul != 0.0:
            return (
                f"blank cell {cell.index} の measured_volume_ul は 0.0 が必要です: "
                f"{cell.measured_volume_ul}"
            )
    return None


def _resolved_image_paths(
    root: Path, cells: tuple[PasteVolumeCell, ...]
) -> tuple[tuple[str, ...] | None, str | None]:
    """全画像の session 相対 path を返す.

    絶対 path、session の外を指す path、実在しない path を拒否する。

    並び順は揃えない。fingerprint は :func:`canonical_json` がキー順を正規化するので、
    ここで並べても値は変わらない。
    """

    base = root.resolve()
    relative_paths: list[str] = []
    for cell in cells:
        for view in cell.views:
            for relative in (view.pre, view.post):
                if error := _validate_image_path(base, root, relative):
                    return None, f"cell {cell.index} view {view.number}: {error}"
                relative_paths.append(relative)
    return tuple(relative_paths), None


def _validate_image_path(base: Path, root: Path, relative: str) -> str | None:
    candidate = Path(relative)
    if candidate.is_absolute():
        return f"画像 path は相対 path が必要です: {relative}"
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(base):
        return f"画像 path が session の外を指しています: {relative}"
    if not resolved.is_file():
        return f"画像が見つかりません: {relative}"
    return None


def _session_fingerprint(root: Path, relative_paths: tuple[str, ...]) -> str:
    """内容だけから session fingerprint を作る.

    絶対 path も directory 名も含めないので、展開先を変えても値が変わらない。
    """

    return fingerprint_json(
        {
            "metadata": sha256_file(root / METADATA_FILE_NAME),
            "images": {
                relative: sha256_file(root / relative) for relative in relative_paths
            },
        }
    )


__all__ = [
    "METADATA_FILE_NAME",
    "PasteVolumeCell",
    "PasteVolumeSession",
]
