"""Dataset session writer（採番・lossless PNG・atomic 確定）の公開契約.

点塗布データセットは塗布後のはんだ円径が事前に分からないため mask を保存しない。
writer が作るのは ``pre/`` と ``post/`` だけで、``mask/`` は生成しない。

Metadata は ``data/testing/schemas/paste_dataset_metadata_v3.json`` を
:func:`parse_metadata` で読み戻して使う（on-disk 契約を唯一の出典にする）。
"""

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import pytest

from pcbasm.pasting.dataset.metadata import (
    DatasetView,
    PasteDatasetMetadata,
    parse_metadata,
)
from pcbasm.pasting.dataset.pending import (
    PENDING_FILENAME,
    PasteDatasetPending,
    parse_pending,
)
from pcbasm.pasting.dataset.writer import (
    PasteDatasetWriter,
    finalize_incomplete,
    rescuable_sessions,
)
from pcbasm.vision.crop import RectCrop
from tests.helpers import TESTING_DATA_DIR

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


def _crop(offset: int = 0, size: int = CROP_SIZE_PX) -> RectCrop:
    """全画像同一寸法の契約に合う正方 uint8 RGB crop."""
    yy, xx = np.indices((size, size), dtype=np.uint8)
    image = np.dstack((xx + offset, yy, xx ^ yy))
    return RectCrop(
        image=image, pixel_rect=(10 + offset, 20, 10 + offset + size, 20 + size)
    )


def _open(
    tmp_path: Path,
    *,
    started_at: datetime | None = STARTED_AT,
    crop_size_px: int = CROP_SIZE_PX,
):
    return PasteDatasetWriter.open(
        tmp_path,
        plate_name="plate-40x40",
        crop_size_px=crop_size_px,
        started_at=started_at,
    )


def _write_all(writer: PasteDatasetWriter) -> None:
    """Fixture が参照する sample index 1（量点）と 2（blank）の全 capture を書く."""
    for index in (1, 2):
        for view in VIEWS:
            writer.write_capture(index, view, "pre", _crop())
            writer.write_capture(index, view, "post", _crop(offset=1))


class TestPasteDatasetWriterOpen:
    """Open() が一時 session directory を作り、同名衝突を採番する."""

    def test_creates_only_pre_and_post_directories(self, tmp_path: Path):
        root = tmp_path / "datasets"

        writer = PasteDatasetWriter.open(
            root,
            plate_name="plate-40x40",
            crop_size_px=CROP_SIZE_PX,
            started_at=STARTED_AT,
        )

        assert writer.root == root
        assert writer.working_path == root / f".{STEM}.tmp"
        assert {path.name for path in writer.working_path.iterdir()} == {"pre", "post"}

    def test_same_plate_and_millisecond_gets_numeric_suffix(self, tmp_path: Path):
        first = _open(tmp_path)
        second = _open(tmp_path)

        assert first.working_path.name == f".{STEM}.tmp"
        assert second.working_path.name == f".{STEM}-1.tmp"

    def test_rejects_naive_started_at_and_empty_plate_name(self, tmp_path: Path):
        with pytest.raises(ValueError):
            _open(tmp_path, started_at=datetime(2026, 9, 8))
        with pytest.raises(ValueError):
            PasteDatasetWriter.open(
                tmp_path,
                plate_name="",
                crop_size_px=CROP_SIZE_PX,
                started_at=STARTED_AT,
            )

    @pytest.mark.parametrize("crop_size_px", [0, -1])
    def test_rejects_non_positive_crop_size(self, tmp_path: Path, crop_size_px: int):
        with pytest.raises(ValueError, match="crop_size_px"):
            _open(tmp_path, crop_size_px=crop_size_px)


class TestPasteDatasetWriterCaptures:
    """Lossless PNG の命名、mask を書かないこと、中断 session の保持."""

    def test_writes_pre_and_post_png_without_any_mask_file(self, tmp_path: Path):
        writer = _open(tmp_path)

        _write_all(writer)
        session = writer.mark_incomplete()

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
        writer = _open(tmp_path)
        pre = _crop()
        post = _crop(offset=1)
        writer.write_capture(1, VIEWS[0], "pre", pre)
        writer.write_capture(1, VIEWS[0], "post", post)

        session = writer.mark_incomplete()

        name = "000001.00.png"
        stored_pre = cv2.imread(str(session / "pre" / name), cv2.IMREAD_UNCHANGED)
        stored_post = cv2.imread(str(session / "post" / name), cv2.IMREAD_UNCHANGED)
        assert stored_pre is not None and stored_post is not None
        assert np.array_equal(stored_pre, pre.image)
        assert np.array_equal(stored_post, post.image)

    def test_captured_view_reports_crop_rect_and_relative_paths(self, tmp_path: Path):
        writer = _open(tmp_path)

        captured = writer.write_capture(1, VIEWS[1], "pre", _crop())

        assert captured.number == 1
        assert captured.offset_x_mm == 1.0
        assert captured.pixel_rect == (10, 20, 10 + CROP_SIZE_PX, 20 + CROP_SIZE_PX)
        assert captured.pre == "pre/000001.01.png"
        assert captured.post == "post/000001.01.png"

    def test_rejects_duplicate_capture(self, tmp_path: Path):
        writer = _open(tmp_path)
        writer.write_capture(1, VIEWS[0], "pre", _crop())

        with pytest.raises(ValueError):
            writer.write_capture(1, VIEWS[0], "pre", _crop())

    @pytest.mark.parametrize("index", [0, -1])
    def test_rejects_non_positive_index(self, tmp_path: Path, index: int):
        writer = _open(tmp_path)

        with pytest.raises(ValueError):
            writer.write_capture(index, VIEWS[0], "pre", _crop())

    def test_rejects_unknown_phase(self, tmp_path: Path):
        writer = _open(tmp_path)

        with pytest.raises(ValueError):
            writer.write_capture(1, VIEWS[0], "mask", _crop())  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "image",
        [
            np.zeros((9, 9), dtype=np.uint8),
            np.zeros((9, 9, 3), dtype=np.uint16),
            np.zeros((9, 9, 4), dtype=np.uint8),
        ],
    )
    def test_rejects_crop_that_is_not_uint8_rgb(
        self, tmp_path: Path, image: np.ndarray
    ):
        writer = _open(tmp_path)

        with pytest.raises(ValueError):
            writer.write_capture(
                1, VIEWS[0], "pre", RectCrop(image=image, pixel_rect=(0, 0, 9, 9))
            )

    @pytest.mark.parametrize("shape", [(9, 8, 3), (8, 9, 3)])
    def test_rejects_non_square_crop(self, tmp_path: Path, shape: tuple[int, int, int]):
        writer = _open(tmp_path)
        image = np.zeros(shape, dtype=np.uint8)

        with pytest.raises(ValueError):
            writer.write_capture(
                1,
                VIEWS[0],
                "pre",
                RectCrop(image=image, pixel_rect=(0, 0, shape[1], shape[0])),
            )

    @pytest.mark.parametrize("delta", [-2, 2])
    def test_rejects_a_crop_whose_size_differs_from_the_session_size(
        self, tmp_path: Path, delta: int
    ):
        # 「全画像同一ピクセル寸法」の永続化境界は writer にある。
        writer = _open(tmp_path)
        writer.write_capture(1, VIEWS[0], "pre", _crop())

        with pytest.raises(ValueError):
            writer.write_capture(1, VIEWS[1], "pre", _crop(size=CROP_SIZE_PX + delta))

    def test_accepts_every_capture_at_the_session_crop_size(self, tmp_path: Path):
        writer = _open(tmp_path)

        _write_all(writer)
        session = writer.mark_incomplete()

        for path in session.rglob("*.png"):
            stored = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            assert stored is not None
            assert stored.shape == (CROP_SIZE_PX, CROP_SIZE_PX, 3)

    def test_rejects_capture_after_the_session_is_finalized(self, tmp_path: Path):
        writer = _open(tmp_path)
        _write_all(writer)
        writer.finalize(_metadata())

        with pytest.raises(RuntimeError):
            writer.write_capture(3, VIEWS[0], "pre", _crop())


class TestPasteDatasetWriterFinalize:
    """Metadata 書き出しと完成 session への atomic rename."""

    def test_writes_schema_v3_and_atomically_publishes_session(self, tmp_path: Path):
        writer = _open(tmp_path)
        _write_all(writer)

        session = writer.finalize(_metadata())

        assert session.name == STEM
        assert {path.name for path in tmp_path.iterdir()} == {STEM}
        payload = json.loads((session / "metadata.json").read_text(encoding="utf-8"))
        assert payload == _payload()
        assert payload["schema_version"] == 3

    def test_rejects_missing_post_capture(self, tmp_path: Path):
        writer = _open(tmp_path)
        for index in (1, 2):
            for view in VIEWS:
                writer.write_capture(index, view, "pre", _crop())

        with pytest.raises(ValueError):
            writer.finalize(_metadata())

    def test_rejects_metadata_index_that_was_never_captured(self, tmp_path: Path):
        writer = _open(tmp_path)
        _write_all(writer)
        payload = _payload()
        samples = payload["samples"]
        assert isinstance(samples, list)
        samples[0]["index"] = 9
        for view in samples[0]["views"]:
            view["pre"] = view["pre"].replace("000001", "000009")
            view["post"] = view["post"].replace("000001", "000009")

        with pytest.raises(ValueError):
            writer.finalize(_parsed(payload))

    def test_rejects_duplicated_sample_index(self, tmp_path: Path):
        writer = _open(tmp_path)
        _write_all(writer)
        payload = _payload()
        samples = payload["samples"]
        assert isinstance(samples, list)
        samples.append(deepcopy(samples[0]))

        with pytest.raises(ValueError, match="index"):
            writer.finalize(_parsed(payload))

    def test_requires_the_blank_captures_as_well(self, tmp_path: Path):
        writer = _open(tmp_path)
        for view in VIEWS:
            writer.write_capture(1, view, "pre", _crop())
            writer.write_capture(1, view, "post", _crop(offset=1))

        with pytest.raises(ValueError):
            writer.finalize(_metadata())

    def test_rejects_a_blank_index_colliding_with_a_sample_index(self, tmp_path: Path):
        writer = _open(tmp_path)
        _write_all(writer)
        payload = _payload()
        blanks = payload["blanks"]
        samples = payload["samples"]
        assert isinstance(blanks, list) and isinstance(samples, list)
        blanks[0]["index"] = samples[0]["index"]
        for view in blanks[0]["views"]:
            view["pre"] = view["pre"].replace("000002", "000001")
            view["post"] = view["post"].replace("000002", "000001")

        with pytest.raises(ValueError, match="index"):
            writer.finalize(_parsed(payload))

    def test_rejects_capture_path_that_breaks_the_naming_rule(self, tmp_path: Path):
        writer = _open(tmp_path)
        _write_all(writer)
        payload = _payload()
        samples = payload["samples"]
        assert isinstance(samples, list)
        samples[0]["views"][0]["pre"] = "pre/1.0.png"

        with pytest.raises(ValueError):
            writer.finalize(_parsed(payload))


class TestPasteDatasetWriterContextManager:
    """Finalize せずに抜けた session は incomplete として保持される."""

    def test_unfinalized_session_is_marked_incomplete_on_exit(self, tmp_path: Path):
        with _open(tmp_path) as writer:
            writer.write_capture(1, VIEWS[0], "pre", _crop())

        assert {path.name for path in tmp_path.iterdir()} == {f"{STEM}.incomplete"}

    def test_exception_inside_block_keeps_incomplete_session(self, tmp_path: Path):
        with pytest.raises(RuntimeError):
            with _open(tmp_path) as writer:
                writer.write_capture(1, VIEWS[0], "pre", _crop())
                raise RuntimeError("abort")

        incomplete = tmp_path / f"{STEM}.incomplete"
        assert (incomplete / "pre" / "000001.00.png").is_file()

    def test_finalized_session_is_not_marked_incomplete(self, tmp_path: Path):
        with _open(tmp_path) as writer:
            _write_all(writer)
            session = writer.finalize(_metadata())

        assert {path.name for path in tmp_path.iterdir()} == {session.name}


class TestPasteDatasetWriterPending:
    """計量質量の入力前に pending.json を残し、確定時に取り除く."""

    def test_write_pending_puts_the_document_in_the_working_session(
        self, tmp_path: Path
    ):
        writer = _open(tmp_path)

        path = writer.write_pending(_pending())

        assert path == writer.working_path / PENDING_FILENAME
        assert json.loads(path.read_text(encoding="utf-8")) == _pending_payload()

    def test_incomplete_session_keeps_the_pending_document(self, tmp_path: Path):
        with _open(tmp_path) as writer:
            _write_all(writer)
            writer.write_pending(_pending())

        incomplete = tmp_path / f"{STEM}.incomplete"
        assert (incomplete / PENDING_FILENAME).is_file()

    def test_finalize_removes_the_pending_document(self, tmp_path: Path):
        writer = _open(tmp_path)
        _write_all(writer)
        writer.write_pending(_pending())

        session = writer.finalize(_metadata())

        assert (session / "metadata.json").is_file()
        assert not (session / PENDING_FILENAME).exists()

    def test_rejects_pending_after_the_session_is_finalized(self, tmp_path: Path):
        writer = _open(tmp_path)
        _write_all(writer)
        writer.finalize(_metadata())

        with pytest.raises(RuntimeError, match="確定済み"):
            writer.write_pending(_pending())


class TestRescuableSessions:
    """pending.json を持つ未確定 session の列挙（救出ジョブの選択肢）."""

    def test_lists_only_sessions_that_carry_a_pending_document(self, tmp_path: Path):
        with _open(tmp_path) as writer:
            _write_all(writer)
            writer.write_pending(_pending())
        (tmp_path / "plate-40x40-20260101T000000.000+0000.incomplete").mkdir()
        (tmp_path / "plate-40x40-20260102T000000.000+0000").mkdir()

        assert rescuable_sessions(tmp_path) == (tmp_path / f"{STEM}.incomplete",)

    def test_lists_a_working_session_left_by_a_killed_process(self, tmp_path: Path):
        """プロセスごと落ちると ``.tmp`` のまま残るので、それも救出対象にする."""
        writer = _open(tmp_path)
        _write_all(writer)
        writer.write_pending(_pending())

        assert rescuable_sessions(tmp_path) == (tmp_path / f".{STEM}.tmp",)

    def test_returns_nothing_for_a_missing_root(self, tmp_path: Path):
        assert rescuable_sessions(tmp_path / "absent") == ()


class TestFinalizeIncomplete:
    """未完了 session へ後から metadata を書いて完成名へ確定する."""

    def _incomplete(self, tmp_path: Path) -> Path:
        with _open(tmp_path) as writer:
            _write_all(writer)
            writer.write_pending(_pending())
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
        writer = _open(tmp_path)
        _write_all(writer)
        writer.write_pending(_pending())

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
