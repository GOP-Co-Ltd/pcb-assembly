"""Dataset session directory への lossless PNG 書き込みと atomic 確定."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Self

import cv2
import numpy as np

from pcbasm.pasting.dataset.metadata import (
    CapturePhase,
    DatasetCapturedView,
    DatasetView,
    PasteDatasetMetadata,
)
from pcbasm.vision.crop import PolygonCrop
from pcbasm.vision.image import ImageArray


class PasteDatasetWriter:
    """一時 session へ画像を書き、完成または incomplete へ atomic 確定する.

    :meth:`open` で session directory を採番・作成して構築する（ctor は副作用を持たない）。
    コンテキストマネージャとして使うと、finalize せずに抜けたとき
    :meth:`mark_incomplete` で取得済みファイルを保持する。
    """

    def __init__(self, root: Path, stem: str) -> None:
        """:meth:`open` が採番した session を包む（directory は作成済みであること）."""
        self._root = root
        self._stem = stem
        self._working_path = root / f".{stem}.tmp"
        self._finished_path: Path | None = None
        self._captures: set[tuple[int, int, CapturePhase]] = set()

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        board_name: str,
        started_at: datetime | None = None,
    ) -> Self:
        """``root`` 直下に一時 session directory を作って writer を返す.

        Raises:
            ValueError: ``started_at`` が timezone を持たない、``board_name`` が空
        """
        timestamp = started_at or datetime.now().astimezone()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("started_atにはtimezoneが必要です")
        if not board_name:
            raise ValueError("board_nameは空にできません")
        root.mkdir(parents=True, exist_ok=True)
        milliseconds = timestamp.microsecond // 1000
        base = (
            f"{board_name}-{timestamp.strftime('%Y%m%dT%H%M%S')}"
            f".{milliseconds:03d}{timestamp.strftime('%z')}"
        )
        writer = cls(root, _available_stem(root, base))
        writer._working_path.mkdir()
        for phase in ("pre", "post", "mask"):
            (writer._working_path / phase).mkdir()
        return writer

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        if self._finished_path is None:
            self.mark_incomplete()

    @property
    def root(self) -> Path:
        """Session を格納する dataset root."""
        return self._root

    @property
    def working_path(self) -> Path:
        """書き込み中の session path."""
        return self._working_path

    def write_capture(
        self,
        pad_index: int,
        view: DatasetView,
        phase: CapturePhase,
        crop: PolygonCrop,
    ) -> DatasetCapturedView:
        """Lossless PNG を書き、metadata 用の view 記述を返す.

        Raises:
            ValueError: pad_index / phase / crop が不正、または同一 capture の重複
            RuntimeError: session が確定済み
        """
        self._ensure_open()
        if type(pad_index) is not int or pad_index < 1:
            raise ValueError(f"pad_indexは1以上の整数が必要です: {pad_index!r}")
        if phase not in ("pre", "post"):
            raise ValueError(f"未知のcapture phaseです: {phase!r}")
        key = (pad_index, view.number, phase)
        if key in self._captures:
            raise ValueError(f"captureが重複しています: {key}")
        _validate_crop(crop)

        filename = f"{pad_index:06d}.{view.number:02d}.png"
        relative = Path(phase) / filename
        mask_relative = Path("mask") / filename
        mask_path = self._working_path / mask_relative
        if mask_path.exists():
            existing = cv2.imread(str(mask_path), cv2.IMREAD_UNCHANGED)
            if existing is None or not np.array_equal(existing, crop.mask):
                raise ValueError(f"同一viewのmaskが一致しません: {filename}")
        else:
            _write_png(mask_path, crop.mask)
        _write_png(self._working_path / relative, crop.image)
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
        """Metadata を書き、完成 session 名へ atomic rename する.

        Raises:
            ValueError: metadata が参照する capture が不足・重複・命名規則不一致
            RuntimeError: session が確定済み
        """
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
        """取得済みファイルを保持したまま incomplete session へ確定する."""
        if self._finished_path is not None:
            return self._finished_path
        destination = self._root / f"{self._stem}.incomplete"
        os.replace(self._working_path, destination)
        self._finished_path = destination
        return destination

    def _ensure_open(self) -> None:
        if self._finished_path is not None:
            raise RuntimeError(f"dataset sessionは確定済みです: {self._finished_path}")


def _available_stem(root: Path, base: str) -> str:
    suffix = 0
    while True:
        stem = base if suffix == 0 else f"{base}-{suffix}"
        candidates = (
            root / stem,
            root / f"{stem}.incomplete",
            root / f".{stem}.tmp",
        )
        if not any(path.exists() for path in candidates):
            return stem
        suffix += 1


def _validate_crop(crop: PolygonCrop) -> None:
    if crop.image.ndim != 3 or crop.image.shape[2] != 3:
        raise ValueError(f"capture画像は3 channelが必要です: {crop.image.shape}")
    if crop.image.dtype != np.uint8:
        raise ValueError(f"capture画像はuint8が必要です: {crop.image.dtype}")
    if crop.mask.shape != crop.image.shape[:2] or crop.mask.dtype != np.uint8:
        raise ValueError("maskはcapture画像と同寸法のuint8が必要です")
    if not np.isin(crop.mask, (0, 255)).all():
        raise ValueError("maskは0/255だけで構成する必要があります")


def _write_png(path: Path, data: ImageArray) -> None:
    if not cv2.imwrite(str(path), data, [cv2.IMWRITE_PNG_COMPRESSION, 3]):
        raise OSError(f"PNGを書き込めません: {path}")
