"""Dataset session directory への lossless PNG 書き込みと atomic 確定."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
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
from pcbasm.pasting.dataset.pending import PENDING_FILENAME, PasteDatasetPending
from pcbasm.vision.crop import RectCrop
from pcbasm.vision.image import ImageArray

# 完成 session が持つ metadata のファイル名
METADATA_FILENAME = "metadata.json"


class PasteDatasetWriter:
    """一時 session へ画像を書き、完成または incomplete へ atomic 確定する.

    :meth:`open` で session directory を採番・作成して構築する（ctor は副作用を持たない）。
    コンテキストマネージャとして使うと、finalize せずに抜けたとき
    :meth:`mark_incomplete` で取得済みファイルを保持する。
    """

    def __init__(self, root: Path, stem: str, crop_size_px: int) -> None:
        """:meth:`open` が採番した session を包む（directory は作成済みであること）.

        ``crop_size_px`` は収集開始時に 1 回だけ決めた crop の一辺で、書き込む全画像が
        ``(n, n, 3)`` の uint8 であることをここで強制する（「全画像同一寸法」の
        永続化境界）。
        """
        self._root = root
        self._stem = stem
        self._crop_size_px = crop_size_px
        self._working_path = root / f".{stem}.tmp"
        self._finished_path: Path | None = None
        self._captures: set[tuple[int, int, CapturePhase]] = set()

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        plate_name: str,
        crop_size_px: int,
        started_at: datetime | None = None,
    ) -> Self:
        """``root`` 直下に一時 session directory を作って writer を返す.

        Raises:
            ValueError: ``started_at`` が timezone を持たない、``plate_name`` が空、
                ``crop_size_px`` が 1 未満
        """
        timestamp = started_at or datetime.now().astimezone()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("started_atにはtimezoneが必要です")
        if not plate_name:
            raise ValueError("plate_nameは空にできません")
        if type(crop_size_px) is not int or crop_size_px < 1:
            raise ValueError(f"crop_size_pxは1以上の整数が必要です: {crop_size_px!r}")
        root.mkdir(parents=True, exist_ok=True)
        milliseconds = timestamp.microsecond // 1000
        base = (
            f"{plate_name}-{timestamp.strftime('%Y%m%dT%H%M%S')}"
            f".{milliseconds:03d}{timestamp.strftime('%z')}"
        )
        writer = cls(root, _available_stem(root, base), crop_size_px)
        writer._working_path.mkdir()
        for phase in ("pre", "post"):
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
        sample_index: int,
        view: DatasetView,
        phase: CapturePhase,
        crop: RectCrop,
    ) -> DatasetCapturedView:
        """Lossless PNG を書き、metadata 用の view 記述を返す.

        Raises:
            ValueError: sample_index / phase / crop が不正、または同一 capture の重複
            RuntimeError: session が確定済み
        """
        self._ensure_open()
        if type(sample_index) is not int or sample_index < 1:
            raise ValueError(f"sample_indexは1以上の整数が必要です: {sample_index!r}")
        if phase not in ("pre", "post"):
            raise ValueError(f"未知のcapture phaseです: {phase!r}")
        key = (sample_index, view.number, phase)
        if key in self._captures:
            raise ValueError(f"captureが重複しています: {key}")
        _validate_crop(crop, self._crop_size_px)

        filename = f"{sample_index:06d}.{view.number:02d}.png"
        _write_png(self._working_path / phase / filename, crop.image)
        self._captures.add(key)
        return DatasetCapturedView(
            number=view.number,
            offset_x_mm=view.offset_x_mm,
            offset_y_mm=view.offset_y_mm,
            pixel_rect=crop.pixel_rect,
            pre=(Path("pre") / filename).as_posix(),
            post=(Path("post") / filename).as_posix(),
        )

    def write_pending(self, pending: PasteDatasetPending) -> Path:
        """作業中 session へ ``pending.json`` を書き、その path を返す.

        計量質量のプロンプトへ応答できないまま落ちても、撮影済み画像から
        metadata を組み直せるようにするための保険。

        Raises:
            RuntimeError: session が確定済み
        """
        self._ensure_open()
        path = self._working_path / PENDING_FILENAME
        _write_json(path, pending.to_dict())
        return path

    def finalize(self, metadata: PasteDatasetMetadata) -> Path:
        """Metadata を書き、完成 session 名へ atomic rename する.

        Raises:
            ValueError: metadata が参照する capture が不足・重複・命名規則不一致
            RuntimeError: session が確定済み
        """
        self._ensure_open()
        expected: set[tuple[int, int, CapturePhase]] = set()
        sample_indices: set[int] = set()
        captured: list[tuple[int, tuple[DatasetCapturedView, ...]]] = [
            *((sample.index, sample.views) for sample in metadata.samples),
            *((blank.index, blank.views) for blank in metadata.blanks),
        ]
        for index, views in captured:
            if index in sample_indices:
                raise ValueError(f"metadataのsample indexが重複しています: {index}")
            sample_indices.add(index)
            view_numbers: set[int] = set()
            for view in views:
                if view.number in view_numbers:
                    raise ValueError(
                        f"metadataのview numberが重複しています: "
                        f"sample={index}, view={view.number}"
                    )
                view_numbers.add(view.number)
                expected.add((index, view.number, "pre"))
                expected.add((index, view.number, "post"))
                filename = f"{index:06d}.{view.number:02d}.png"
                if view.pre != f"pre/{filename}" or view.post != f"post/{filename}":
                    raise ValueError(
                        f"metadataのcapture pathが命名規則と一致しません: {filename}"
                    )
        missing = expected - self._captures
        if missing:
            raise ValueError(
                f"metadataが参照するcaptureが不足しています: {sorted(missing)}"
            )
        _write_json(self._working_path / METADATA_FILENAME, metadata.to_dict())
        # 確定済み session に救出用の doc を残すと、どちらが正かが曖昧になる
        (self._working_path / PENDING_FILENAME).unlink(missing_ok=True)
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


def rescuable_sessions(root: Path) -> tuple[Path, ...]:
    """``pending.json`` を持つ未確定 session を名前順に返す（救出の選択肢）.

    対象は ``<stem>.incomplete``（ジョブが例外・中止時に確定させたもの）と
    ``.<stem>.tmp``（プロセスごと落ちて確定できなかったもの）の両方。

    どちらも収集ジョブが装置ロックを手放したあとにしか残らないので、実行中の
    session を対象にすることはない。
    """
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            path
            for path in root.iterdir()
            if path.is_dir()
            and _completed_stem(path.name) is not None
            and (path / PENDING_FILENAME).is_file()
        )
    )


def finalize_incomplete(
    session: Path, metadata: PasteDatasetMetadata
) -> tuple[Path | None, str | None]:
    """未確定 session へ後から ``metadata.json`` を書き、完成名へ rename する.

    撮影済み画像はそのまま使う（コピーも再エンコードもしない）。

    metadata が参照する capture が欠けていれば書かずに理由を返す（session は未確定のまま）。
    """
    if not session.is_dir():
        return None, f"未確定sessionが見つかりません: {session}"
    base = _completed_stem(session.name)
    if base is None:
        return None, (
            f"未確定session（*.incomplete / .*.tmp）ではありません: {session.name}"
        )
    missing = [
        path
        for target in (*metadata.samples, *metadata.blanks)
        for view in target.views
        for path in (view.pre, view.post)
        if not (session / path).is_file()
    ]
    if missing:
        return None, f"metadataが参照するcaptureがありません: {', '.join(missing)}"
    _write_json(session / METADATA_FILENAME, metadata.to_dict())
    (session / PENDING_FILENAME).unlink(missing_ok=True)
    destination = session.parent / _first_free_name(
        base, lambda name: (session.parent / name).exists()
    )
    os.replace(session, destination)
    return destination, None


def _completed_stem(name: str) -> str | None:
    """未確定 session の directory 名から完成 session の stem を求める.

    ``<stem>.incomplete`` と ``.<stem>.tmp`` のどちらでもなければ ``None``。
    """
    if name.endswith(".incomplete"):
        return name.removesuffix(".incomplete") or None
    if name.startswith(".") and name.endswith(".tmp"):
        return name[1:].removesuffix(".tmp") or None
    return None


def _write_json(path: Path, document: dict[str, object]) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _available_stem(root: Path, base: str) -> str:
    """作業中・未確定・完成のどれとも衝突しない session stem."""
    return _first_free_name(
        base,
        lambda stem: any(
            (root / candidate).exists()
            for candidate in (stem, f"{stem}.incomplete", f".{stem}.tmp")
        ),
    )


def _first_free_name(base: str, taken: Callable[[str], bool]) -> str:
    """``base`` から始めて連番を足し、最初に未使用となる名前を返す."""
    suffix = 0
    while True:
        name = base if suffix == 0 else f"{base}-{suffix}"
        if not taken(name):
            return name
        suffix += 1


def _validate_crop(crop: RectCrop, crop_size_px: int) -> None:
    expected = (crop_size_px, crop_size_px, 3)
    if crop.image.shape != expected:
        raise ValueError(f"capture画像は{expected}が必要です: {crop.image.shape}")
    if crop.image.dtype != np.uint8:
        raise ValueError(f"capture画像はuint8が必要です: {crop.image.dtype}")


def _write_png(path: Path, data: ImageArray) -> None:
    if not cv2.imwrite(str(path), data, [cv2.IMWRITE_PNG_COMPRESSION, 3]):
        raise OSError(f"PNGを書き込めません: {path}")
