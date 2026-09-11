"""完成 session の列挙と読み出し（writer の対）の公開契約.

on-disk の出典は ``data/testing/schemas/paste_dataset_metadata_v3.json``。
writer が作る 3 状態（``.tmp`` / ``.incomplete`` / 完成）のうち、完成だけを拾うこと、
版違いを黙って読み飛ばさず理由付きで拒否することを固める。
"""

import json
from pathlib import Path

import pytest

from pcbasm.pasting.dataset.reader import DatasetSession, completed_sessions
from tests.helpers import TESTING_DATA_DIR

METADATA_V3 = TESTING_DATA_DIR / "schemas" / "paste_dataset_metadata_v3.json"
STEM = "plate-40x40-20260908T143052.123+0000"


def _write_session(root: Path, name: str, *, document: dict | None = None) -> Path:
    session = root / name
    session.mkdir(parents=True)
    payload = (
        json.loads(METADATA_V3.read_text(encoding="utf-8"))
        if document is None
        else document
    )
    (session / "metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return session


class TestCompletedSessions:
    """完成 session だけを名前順で拾う."""

    def test_returns_nothing_when_the_root_is_missing(self, tmp_path: Path):
        assert completed_sessions(tmp_path / "absent") == ()

    def test_lists_only_directories_that_carry_metadata(self, tmp_path: Path):
        done = _write_session(tmp_path, STEM)
        (tmp_path / f"{STEM}.incomplete").mkdir()
        (tmp_path / f".{STEM}.tmp").mkdir()
        (tmp_path / "stray.json").write_text("{}", encoding="utf-8")

        assert completed_sessions(tmp_path) == (done,)

    def test_excludes_an_incomplete_session_that_already_holds_metadata(
        self, tmp_path: Path
    ):
        """``finalize_incomplete`` は rename 前に metadata を書くので名前で外す."""
        done = _write_session(tmp_path, STEM)
        _write_session(tmp_path, f"{STEM}.incomplete")
        _write_session(tmp_path, f".{STEM}.tmp")

        assert completed_sessions(tmp_path) == (done,)

    def test_orders_sessions_by_name(self, tmp_path: Path):
        second = _write_session(tmp_path, "plate-b")
        first = _write_session(tmp_path, "plate-a")

        assert completed_sessions(tmp_path) == (first, second)


class TestDatasetSessionLoad:
    """Metadata の復元と、読めないものの理由返却."""

    def test_loads_the_on_disk_document(self, tmp_path: Path):
        root = _write_session(tmp_path, STEM)

        session, error = DatasetSession.load(root)

        assert error is None, error
        assert session is not None
        assert session.label == STEM
        assert session.metadata.schema_version == 3
        assert session.pixel_per_mm == session.metadata.camera.pixel_per_mm

    def test_reports_a_directory_without_metadata(self, tmp_path: Path):
        (tmp_path / STEM).mkdir()

        session, error = DatasetSession.load(tmp_path / STEM)

        assert session is None
        assert error is not None
        assert "metadata.json" in error

    def test_reports_broken_json(self, tmp_path: Path):
        root = tmp_path / STEM
        root.mkdir()
        (root / "metadata.json").write_text("{", encoding="utf-8")

        session, error = DatasetSession.load(root)

        assert session is None
        assert error is not None

    @pytest.mark.parametrize("version", [1, 2, 4])
    def test_rejects_other_schema_versions_without_migrating(
        self, tmp_path: Path, version: int
    ):
        payload = json.loads(METADATA_V3.read_text(encoding="utf-8"))
        payload["schema_version"] = version
        root = _write_session(tmp_path, STEM, document=payload)

        session, error = DatasetSession.load(root)

        assert session is None
        assert error is not None
        assert "schema_version" in error
        # どの session が拒否されたのか分かるよう名前を入れる
        assert STEM in error


class TestDatasetSessionImagePath:
    """画像 path の解決は session の外へ出さない."""

    @pytest.fixture
    def session(self, tmp_path: Path) -> DatasetSession:
        root = _write_session(tmp_path, STEM)
        (root / "pre").mkdir()
        (root / "pre" / "000001.00.png").write_bytes(b"x")
        loaded, error = DatasetSession.load(root)
        assert loaded is not None, error
        return loaded

    def test_resolves_a_relative_path_inside_the_session(self, session: DatasetSession):
        path, error = session.image_path("pre/000001.00.png")

        assert error is None
        assert path == (session.root / "pre" / "000001.00.png").resolve()

    def test_rejects_an_absolute_path(self, session: DatasetSession):
        path, error = session.image_path("/etc/passwd")

        assert path is None
        assert error is not None

    def test_rejects_an_escape_out_of_the_session(self, session: DatasetSession):
        path, error = session.image_path("../../etc/passwd")

        assert path is None
        assert error is not None

    def test_reports_a_missing_image(self, session: DatasetSession):
        path, error = session.image_path("pre/absent.png")

        assert path is None
        assert error is not None


class TestDatasetSessionCells:
    """塗布 sample と blank を同じ採番列で並べる."""

    def test_merges_samples_and_blanks_in_index_order(self, tmp_path: Path):
        root = _write_session(tmp_path, STEM)
        session, error = DatasetSession.load(root)
        assert session is not None, error

        cells = session.cells()

        indices = [cell.index for cell in cells]
        assert indices == sorted(indices)
        assert len(cells) == len(session.metadata.samples) + len(
            session.metadata.blanks
        )
