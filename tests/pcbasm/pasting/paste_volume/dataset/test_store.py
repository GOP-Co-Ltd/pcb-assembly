"""Dataset session store（採番・lossless PNG・atomic 確定）の公開契約.

点塗布データセットは塗布後のはんだ円径が事前に分からないため mask を保存しない。
store が作るのは ``pre/`` と ``post/`` だけで、``mask/`` は生成しない。

Metadata は ``data/testing/schemas/paste_dataset_metadata_v3.json`` を
:func:`parse_metadata` で読み戻して使う（on-disk 契約を唯一の出典にする）。
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import pytest

from pcbasm.pasting.paste_volume.dataset.metadata import (
    DatasetView,
    PasteDatasetMetadata,
    parse_metadata,
)
from pcbasm.pasting.paste_volume.dataset.pending import (
    PENDING_FILENAME,
    PasteDatasetPending,
    parse_pending,
)
from pcbasm.pasting.paste_volume.dataset.store import (
    PasteDatasetStore,
    finalize_incomplete,
    rescuable_sessions,
)
from pcbasm.vision.crop import RectCrop
from tests.support.paths import TESTING_DATA_DIR

METADATA_V3 = TESTING_DATA_DIR / "schemas" / "paste_dataset_metadata_v3.json"
PENDING_V1 = TESTING_DATA_DIR / "schemas" / "paste_dataset_pending_v1.json"
STARTED_AT = datetime(2026, 9, 8, 14, 30, 52, 123456, tzinfo=UTC)
STEM = "plate-40x40-20260908T143052.123+0000"
CROP_SIZE_PX = 9
VIEWS = (DatasetView(number=0), DatasetView(number=1, offset_x_mm=1.0))


def _payload() -> dict[str, object]:
    return json.loads(METADATA_V3.read_text(encoding="utf-8"))


def _parsed(payload: dict[str, object]) -> PasteDatasetMetadata:
    metadata, error = parse_metadata(payload)

    assert error is None, error
    assert metadata is not None
    return metadata


def _metadata() -> PasteDatasetMetadata:
    """Fixture の v3 doc（sample index 1、view 0 / 1）をそのまま使う."""
    return _parsed(_payload())


def _pending_payload() -> dict[str, object]:
    return json.loads(PENDING_V1.read_text(encoding="utf-8"))


def _pending() -> PasteDatasetPending:
    """Fixture の v1 doc（``_metadata()`` と同じ収集の、質量未確定版）."""
    pending, error = parse_pending(_pending_payload())

    assert error is None, error
    assert pending is not None
    return pending


def _crop(offset: int = 0) -> RectCrop:
    """全画像同一寸法の正方 uint8 RGB crop."""
    size = CROP_SIZE_PX
    yy, xx = np.indices((size, size), dtype=np.uint8)
    image = np.dstack((xx + offset, yy, xx ^ yy))
    return RectCrop(
        image=image, pixel_rect=(10 + offset, 20, 10 + offset + size, 20 + size)
    )


def _open(tmp_path: Path, *, started_at: datetime | None = STARTED_AT):
    return PasteDatasetStore.open(
        tmp_path, plate_name="plate-40x40", started_at=started_at
    )


def _write_all(store: PasteDatasetStore) -> None:
    """Fixture が参照する sample index 1（量点）と 2（blank）の全 capture を書く."""
    for index in (1, 2):
        for view in VIEWS:
            store.write_capture(index, view, "pre", _crop())
            store.write_capture(index, view, "post", _crop(offset=1))


class TestPasteDatasetStoreOpen:
    """Open() が一時 session directory を作り、同名衝突を採番する."""

    def test_creates_only_pre_and_post_directories(self, tmp_path: Path):
        root = tmp_path / "datasets"

        store = PasteDatasetStore.open(
            root,
            plate_name="plate-40x40",
            started_at=STARTED_AT,
        )

        assert store.root == root
        assert store.working_path == root / f".{STEM}.tmp"
        assert {path.name for path in store.working_path.iterdir()} == {"pre", "post"}

    def test_same_plate_and_millisecond_gets_numeric_suffix(self, tmp_path: Path):
        first = _open(tmp_path)
        second = _open(tmp_path)

        assert first.working_path.name == f".{STEM}.tmp"
        assert second.working_path.name == f".{STEM}-1.tmp"

    def test_rejects_naive_started_at_and_empty_plate_name(self, tmp_path: Path):
        with pytest.raises(ValueError):
            _open(tmp_path, started_at=datetime(2026, 9, 8))
        with pytest.raises(ValueError):
            PasteDatasetStore.open(
                tmp_path,
                plate_name="",
                started_at=STARTED_AT,
            )


class TestPasteDatasetStoreCaptures:
    """Lossless PNG の命名、mask を書かないこと、中断 session の保持."""

    def test_writes_pre_and_post_png_without_any_mask_file(self, tmp_path: Path):
        store = _open(tmp_path)

        _write_all(store)
        session = store.mark_incomplete()

        assert session.name == f"{STEM}.incomplete"
        assert {path.name for path in session.iterdir()} == {"pre", "post"}
        assert not list(session.rglob("mask*"))
        assert {path.name for path in (session / "pre").iterdir()} == {
            "000001.00.png",
            "000001.01.png",
            "000002.00.png",
            "000002.01.png",
        }

    def test_written_png_is_lossless(self, tmp_path: Path):
        store = _open(tmp_path)
        pre = _crop()
        post = _crop(offset=1)
        store.write_capture(1, VIEWS[0], "pre", pre)
        store.write_capture(1, VIEWS[0], "post", post)

        session = store.mark_incomplete()

        name = "000001.00.png"
        stored_pre = cv2.imread(str(session / "pre" / name), cv2.IMREAD_UNCHANGED)
        stored_post = cv2.imread(str(session / "post" / name), cv2.IMREAD_UNCHANGED)
        assert stored_pre is not None and stored_post is not None
        assert np.array_equal(stored_pre, pre.image)
        assert np.array_equal(stored_post, post.image)

    def test_captured_view_reports_crop_rect_and_relative_paths(self, tmp_path: Path):
        store = _open(tmp_path)

        captured = store.write_capture(1, VIEWS[1], "pre", _crop())

        assert captured.number == 1
        assert captured.offset_x_mm == 1.0
        assert captured.pixel_rect == (10, 20, 10 + CROP_SIZE_PX, 20 + CROP_SIZE_PX)
        assert captured.pre == "pre/000001.01.png"
        assert captured.post == "post/000001.01.png"

    def test_rejects_capture_after_the_session_is_finalized(self, tmp_path: Path):
        store = _open(tmp_path)
        _write_all(store)
        store.finalize(_metadata())

        with pytest.raises(RuntimeError):
            store.write_capture(3, VIEWS[0], "pre", _crop())


class TestPasteDatasetStoreFinalize:
    """Metadata 書き出しと完成 session への atomic rename."""

    def test_writes_schema_v3_and_atomically_publishes_session(self, tmp_path: Path):
        store = _open(tmp_path)
        _write_all(store)

        session = store.finalize(_metadata())

        assert session.name == STEM
        assert {path.name for path in tmp_path.iterdir()} == {STEM}
        payload = json.loads((session / "metadata.json").read_text(encoding="utf-8"))
        assert payload == _payload()
        assert payload["schema_version"] == 3

    def test_rejects_missing_post_capture(self, tmp_path: Path):
        store = _open(tmp_path)
        for index in (1, 2):
            for view in VIEWS:
                store.write_capture(index, view, "pre", _crop())

        with pytest.raises(ValueError):
            store.finalize(_metadata())

    def test_rejects_metadata_index_that_was_never_captured(self, tmp_path: Path):
        store = _open(tmp_path)
        _write_all(store)
        payload = _payload()
        samples = payload["samples"]
        assert isinstance(samples, list)
        samples[0]["index"] = 9
        for view in samples[0]["views"]:
            view["pre"] = view["pre"].replace("000001", "000009")
            view["post"] = view["post"].replace("000001", "000009")

        with pytest.raises(ValueError):
            store.finalize(_parsed(payload))

    def test_requires_the_blank_captures_as_well(self, tmp_path: Path):
        store = _open(tmp_path)
        for view in VIEWS:
            store.write_capture(1, view, "pre", _crop())
            store.write_capture(1, view, "post", _crop(offset=1))

        with pytest.raises(ValueError):
            store.finalize(_metadata())


class TestPasteDatasetStoreContextManager:
    """Finalize せずに抜けた session は incomplete として保持される."""

    def test_unfinalized_session_is_marked_incomplete_on_exit(self, tmp_path: Path):
        with _open(tmp_path) as store:
            store.write_capture(1, VIEWS[0], "pre", _crop())

        assert {path.name for path in tmp_path.iterdir()} == {f"{STEM}.incomplete"}

    def test_exception_inside_block_keeps_incomplete_session(self, tmp_path: Path):
        with pytest.raises(RuntimeError):
            with _open(tmp_path) as store:
                store.write_capture(1, VIEWS[0], "pre", _crop())
                raise RuntimeError("abort")

        incomplete = tmp_path / f"{STEM}.incomplete"
        assert (incomplete / "pre" / "000001.00.png").is_file()

    def test_finalized_session_is_not_marked_incomplete(self, tmp_path: Path):
        with _open(tmp_path) as store:
            _write_all(store)
            session = store.finalize(_metadata())

        assert {path.name for path in tmp_path.iterdir()} == {session.name}


class TestPasteDatasetStorePending:
    """計量質量の入力前に pending.json を残し、確定時に取り除く."""

    def test_write_pending_puts_the_document_in_the_working_session(
        self, tmp_path: Path
    ):
        store = _open(tmp_path)

        path = store.write_pending(_pending())

        assert path == store.working_path / PENDING_FILENAME
        assert json.loads(path.read_text(encoding="utf-8")) == _pending_payload()

    def test_incomplete_session_keeps_the_pending_document(self, tmp_path: Path):
        with _open(tmp_path) as store:
            _write_all(store)
            store.write_pending(_pending())

        incomplete = tmp_path / f"{STEM}.incomplete"
        assert (incomplete / PENDING_FILENAME).is_file()

    def test_finalize_removes_the_pending_document(self, tmp_path: Path):
        store = _open(tmp_path)
        _write_all(store)
        store.write_pending(_pending())

        session = store.finalize(_metadata())

        assert (session / "metadata.json").is_file()
        assert not (session / PENDING_FILENAME).exists()

    def test_rejects_pending_after_the_session_is_finalized(self, tmp_path: Path):
        store = _open(tmp_path)
        _write_all(store)
        store.finalize(_metadata())

        with pytest.raises(RuntimeError, match="確定済み"):
            store.write_pending(_pending())


class TestRescuableSessions:
    """pending.json を持つ未確定 session の列挙（救出ジョブの選択肢）."""

    def test_lists_only_sessions_that_carry_a_pending_document(self, tmp_path: Path):
        with _open(tmp_path) as store:
            _write_all(store)
            store.write_pending(_pending())
        (tmp_path / "plate-40x40-20260101T000000.000+0000.incomplete").mkdir()
        (tmp_path / "plate-40x40-20260102T000000.000+0000").mkdir()

        assert rescuable_sessions(tmp_path) == (tmp_path / f"{STEM}.incomplete",)

    def test_lists_a_working_session_left_by_a_killed_process(self, tmp_path: Path):
        """プロセスごと落ちると ``.tmp`` のまま残るので、それも救出対象にする."""
        store = _open(tmp_path)
        _write_all(store)
        store.write_pending(_pending())

        assert rescuable_sessions(tmp_path) == (tmp_path / f".{STEM}.tmp",)

    def test_returns_nothing_for_a_missing_root(self, tmp_path: Path):
        assert rescuable_sessions(tmp_path / "absent") == ()


class TestFinalizeIncomplete:
    """未完了 session へ後から metadata を書いて完成名へ確定する."""

    def _incomplete(self, tmp_path: Path) -> Path:
        with _open(tmp_path) as store:
            _write_all(store)
            store.write_pending(_pending())
        return tmp_path / f"{STEM}.incomplete"

    def test_writes_metadata_and_renames_to_the_completed_stem(self, tmp_path: Path):
        incomplete = self._incomplete(tmp_path)

        session, error = finalize_incomplete(incomplete, _metadata())

        assert error is None, error
        assert session is not None
        assert session == tmp_path / STEM
        assert (
            json.loads((session / "metadata.json").read_text(encoding="utf-8"))
            == _payload()
        )
        assert not (session / PENDING_FILENAME).exists()
        assert (session / "pre" / "000001.00.png").is_file()

    def test_suffixes_the_stem_when_a_completed_session_already_exists(
        self, tmp_path: Path
    ):
        incomplete = self._incomplete(tmp_path)
        (tmp_path / STEM).mkdir()

        session, error = finalize_incomplete(incomplete, _metadata())

        assert error is None, error
        assert session is not None
        assert session == tmp_path / f"{STEM}-1"

    def test_finalizes_a_working_session_left_by_a_killed_process(self, tmp_path: Path):
        store = _open(tmp_path)
        _write_all(store)
        store.write_pending(_pending())

        session, error = finalize_incomplete(tmp_path / f".{STEM}.tmp", _metadata())

        assert error is None, error
        assert session is not None
        assert session == tmp_path / STEM
        assert (session / "metadata.json").is_file()

    def test_rejects_a_directory_that_is_not_an_unfinished_session(
        self, tmp_path: Path
    ):
        session = tmp_path / STEM
        session.mkdir()

        result, error = finalize_incomplete(session, _metadata())

        assert result is None
        assert error is not None
        assert "incomplete" in error

    def test_rejects_a_missing_directory(self, tmp_path: Path):
        result, error = finalize_incomplete(
            tmp_path / f"{STEM}.incomplete", _metadata()
        )

        assert result is None
        assert error is not None

    def test_rejects_metadata_whose_capture_files_are_missing(self, tmp_path: Path):
        incomplete = self._incomplete(tmp_path)
        (incomplete / "post" / "000001.01.png").unlink()

        result, error = finalize_incomplete(incomplete, _metadata())

        assert result is None
        assert error is not None
        assert "post/000001.01.png" in error
        assert incomplete.is_dir()  # 失敗しても未完了のまま残す
