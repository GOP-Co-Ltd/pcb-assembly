"""完成した収集 session の列挙と読み出し（:mod:`.writer` の対）.

``writer`` は書き込み中（``.<stem>.tmp``）・未確定（``<stem>.incomplete``）・完成
（``<stem>``）の 3 状態を作る。読み出し側が要るのは完成 session だけなので、
``metadata.json`` を持つ directory だけを候補にする。

版が違う session は読み飛ばさず ``(None, 理由)`` で拒否する。収集した版を跨いで
黙って混ぜると、教師体積の作り方が違うものが 1 つの校正へ混ざる。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

import attrs

from pcbasm.pasting.dataset.metadata import (
    PasteDatasetBlank,
    PasteDatasetMetadata,
    PasteDatasetSample,
    parse_metadata,
)
from pcbasm.pasting.dataset.writer import METADATA_FILENAME


def completed_sessions(root: Path) -> tuple[Path, ...]:
    """``metadata.json`` を持つ完成 session を名前順に返す.

    書き込み中（``.<stem>.tmp``）と未確定（``<stem>.incomplete``）は名前で外す。

    ``finalize_incomplete`` は ``<stem>.incomplete`` の中へ ``metadata.json`` を
    書いてから rename するので、その窓と rename 失敗時は中身だけでは区別できない。
    """
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            path
            for path in root.iterdir()
            if path.is_dir()
            and not path.name.startswith(".")
            and not path.name.endswith(".incomplete")
            and (path / METADATA_FILENAME).is_file()
        )
    )


@attrs.frozen
class DatasetSession:
    """1 つの完成 session（metadata と、画像を解決するための root）."""

    root: Path
    metadata: PasteDatasetMetadata

    @classmethod
    def load(cls, root: Path) -> tuple[Self | None, str | None]:
        """``metadata.json`` を読んで復元する（不正・未対応版は理由を返す）."""
        path = root / METADATA_FILENAME
        if not path.is_file():
            return None, f"metadata.jsonがありません: {root}"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return None, f"metadata.jsonを読めません: {error}"
        if not isinstance(document, dict):
            return None, f"metadata.jsonがobjectではありません: {root}"
        metadata, error_text = parse_metadata(document)
        if metadata is None:
            return None, f"{root.name}: {error_text}"
        return cls(root=root, metadata=metadata), None

    @property
    def label(self) -> str:
        """Session directory 名（表示・記録用）."""
        return self.root.name

    @property
    def pixel_per_mm(self) -> float:
        """収集時の camera calibration の pixel/mm."""
        return self.metadata.camera.pixel_per_mm

    def image_path(self, relative: str) -> tuple[Path | None, str | None]:
        """Metadata の相対 path を実 path へ解決する.

        絶対 path・session 外への脱出・不在はいずれも拒否する。

        session directory は人が動かせるので、書かれた相対 path を信用しない。
        """
        if not relative or Path(relative).is_absolute():
            return None, f"画像pathが相対pathではありません: {relative!r}"
        resolved = (self.root / relative).resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            return None, f"画像pathがsession外を指しています: {relative!r}"
        if not resolved.is_file():
            return None, f"画像がありません: {relative!r}"
        return resolved, None

    def cells(self) -> tuple[PasteDatasetSample | PasteDatasetBlank, ...]:
        """塗布 sample と blank を index 昇順で返す."""
        merged: list[PasteDatasetSample | PasteDatasetBlank] = [
            *self.metadata.samples,
            *self.metadata.blanks,
        ]
        return tuple(sorted(merged, key=lambda cell: cell.index))
