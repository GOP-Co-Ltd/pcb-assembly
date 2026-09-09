"""1 収集 session の読み込みと構造検証の公開契約."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pcbasm.pasting.paste_volume.session import PasteVolumeSession
from tests.pcbasm.pasting.paste_volume.helpers import (
    CROP_SIZE_PX,
    VIEW_COUNT,
    SyntheticCell,
    corrupt_metadata,
    write_session,
)

CELLS = (
    SyntheticCell(index=1, commanded_volume_ul=0.10, x_mm=0.5),
    SyntheticCell(index=2, commanded_volume_ul=0.30, x_mm=2.7),
    SyntheticCell(index=3, commanded_volume_ul=None, x_mm=4.9),
)


def _session(root: Path, **overrides: Any) -> PasteVolumeSession:
    session, reason = PasteVolumeSession.load(
        write_session(root, cells=CELLS, **overrides)
    )
    assert session is not None, reason
    return session


def _rejected(root: Path) -> str:
    session, reason = PasteVolumeSession.load(root)
    assert session is None
    assert reason is not None
    return reason


class TestPasteVolumeSessionLoad:
    """正常な session の読み込み結果."""

    def test_unifies_samples_and_blanks_into_cells(self, tmp_path: Path):
        """収集 schema の samples と blanks が 1 本の cells になる.

        学習側は「1 cell = 1 sample」で扱うので、収集 schema の 2 本立てをここで畳む。
        """

        session = _session(tmp_path / "session")

        assert [cell.index for cell in session.cells] == [1, 2, 3]
        assert [cell.is_blank for cell in session.cells] == [False, False, True]

    def test_blank_carries_no_dispense_command(self, tmp_path: Path):
        blank = _session(tmp_path / "session").cells[2]

        assert blank.commanded_volume_ul is None
        assert blank.order is None
        assert blank.measured_volume_ul == 0.0

    def test_every_cell_keeps_the_centre_view_and_the_peripheral_views(
        self, tmp_path: Path
    ):
        """各 cell の view は中心 1 点 + 周辺 n 方向で、番号 0 が中心.

        収集設定の ``view_count`` は周辺 view の数なので、実際の view 数は 1 多い。
        """

        session = _session(tmp_path / "session")

        assert session.metadata.config.view_count == VIEW_COUNT - 1
        for cell in session.cells:
            assert [view.number for view in cell.views] == list(range(VIEW_COUNT))
            assert (cell.views[0].offset_x_mm, cell.views[0].offset_y_mm) == (0.0, 0.0)

    def test_exposes_the_collection_resolution(self, tmp_path: Path):
        session = _session(tmp_path / "session")

        assert session.pixel_per_mm == session.metadata.camera.pixel_per_mm
        assert session.metadata.config.crop_size_px == CROP_SIZE_PX

    def test_resolves_image_paths_under_the_session_root(self, tmp_path: Path):
        session = _session(tmp_path / "session")

        path = session.image_path(session.cells[0].views[0].pre)

        assert path.is_file()
        assert path.parent.parent == session.root


class TestSessionFingerprint:
    """内容だけで session fingerprint が決まること."""

    def test_is_prefixed_with_the_hash_algorithm(self, tmp_path: Path):
        assert _session(tmp_path / "session").session_fingerprint.startswith("sha256:")

    def test_does_not_change_with_the_directory_name_or_location(self, tmp_path: Path):
        """展開先と directory 名を変えても同じ値になる.

        sample_id と split manifest がこの値に載るので、mount を変えただけで学習を
        やり直す羽目にならないことを固定する。
        """

        first = _session(tmp_path / "one" / "plate-a")
        second = _session(tmp_path / "another" / "plate-b")

        assert first.session_fingerprint == second.session_fingerprint

    def test_changes_when_a_single_image_byte_changes(self, tmp_path: Path):
        """画像を 1 byte 変えると値が変わる.

        metadata だけを hash すると、切り出し直した PNG を黙って受け入れて resume を 通してしまう。
        """

        baseline = _session(tmp_path / "one")
        target = _session(tmp_path / "another")
        path = target.image_path(target.cells[0].views[0].post)
        content = bytearray(path.read_bytes())
        content[-1] ^= 0x01
        path.write_bytes(bytes(content))

        changed, reason = PasteVolumeSession.load(target.root)

        assert changed is not None, reason
        assert changed.session_fingerprint != baseline.session_fingerprint

    def test_changes_when_a_label_changes(self, tmp_path: Path):
        baseline = _session(tmp_path / "one")
        root = corrupt_metadata(
            write_session(tmp_path / "another", cells=CELLS),
            lambda document: document["samples"][0].update(measured_volume_ul=0.999),
        )

        changed, reason = PasteVolumeSession.load(root)

        assert changed is not None, reason
        assert changed.session_fingerprint != baseline.session_fingerprint


class TestSessionRejection:
    """壊れた session を理由つきで拒否すること."""

    def test_reports_a_missing_metadata_file(self, tmp_path: Path):
        root = tmp_path / "empty"
        root.mkdir()

        assert "metadata.json が見つかりません" in _rejected(root)

    def test_reports_metadata_that_is_not_json(self, tmp_path: Path):
        root = write_session(tmp_path / "session", cells=CELLS)
        (root / "metadata.json").write_text("{ not json", encoding="utf-8")

        assert "metadata.json を読めません" in _rejected(root)

    def test_reports_metadata_whose_top_level_is_not_an_object(self, tmp_path: Path):
        root = write_session(tmp_path / "session", cells=CELLS)
        (root / "metadata.json").write_text("[]", encoding="utf-8")

        assert "最上位は object が必要です" in _rejected(root)

    def test_reports_an_unsupported_schema_version(self, tmp_path: Path):
        """収集 schema の v2 以外を拒否する.

        判定そのものは ``parse_metadata`` の責務で、ここではその理由が呼び出し側まで
        素通しで返ることを見る。
        """

        root = corrupt_metadata(
            write_session(tmp_path / "session", cells=CELLS),
            lambda document: document.update(schema_version=1),
        )

        assert "schema_version" in _rejected(root)

    def test_reports_an_image_path_that_escapes_the_session(self, tmp_path: Path):
        """``..`` で session の外を指す path を拒否する."""

        root = corrupt_metadata(
            write_session(tmp_path / "session", cells=CELLS),
            lambda document: document["samples"][0]["views"][0].update(
                pre="../../etc/passwd"
            ),
        )

        assert "session の外を指しています" in _rejected(root)

    def test_reports_an_absolute_image_path(self, tmp_path: Path):
        root = corrupt_metadata(
            write_session(tmp_path / "session", cells=CELLS),
            lambda document: document["samples"][0]["views"][0].update(
                pre="/etc/passwd"
            ),
        )

        assert "相対 path が必要です" in _rejected(root)

    def test_reports_a_missing_image(self, tmp_path: Path):
        root = write_session(tmp_path / "session", cells=CELLS)
        (root / "post" / f"{CELLS[1].index:06d}.02.png").unlink()

        assert "画像が見つかりません" in _rejected(root)

    def test_reports_a_duplicated_cell_index(self, tmp_path: Path):
        """塗布 sample と blank をまたいだ index 重複も見る.

        sample_id が index から作られるので、重複すると別の cell が同じ ID を持つ。
        """

        root = corrupt_metadata(
            write_session(tmp_path / "session", cells=CELLS),
            lambda document: document["blanks"][0].update(
                index=document["samples"][0]["index"]
            ),
        )

        assert "index が重複しています" in _rejected(root)

    def test_reports_a_blank_whose_label_is_not_zero(self, tmp_path: Path):
        root = corrupt_metadata(
            write_session(tmp_path / "session", cells=CELLS),
            lambda document: document["blanks"][0].update(measured_volume_ul=0.05),
        )

        assert "measured_volume_ul は 0.0 が必要です" in _rejected(root)

    def test_reports_a_duplicated_view_number(self, tmp_path: Path):
        root = corrupt_metadata(
            write_session(tmp_path / "session", cells=CELLS),
            lambda document: document["samples"][0]["views"][1].update(number=0),
        )

        assert "view number が重複しています" in _rejected(root)

    def test_reports_a_cell_without_any_view(self, tmp_path: Path):
        root = corrupt_metadata(
            write_session(tmp_path / "session", cells=CELLS),
            lambda document: document["samples"][0].update(views=[]),
        )

        assert "view がありません" in _rejected(root)

    def test_reports_a_session_without_any_cell(self, tmp_path: Path):
        root = corrupt_metadata(
            write_session(tmp_path / "session", cells=CELLS),
            lambda document: document.update(samples=[], blanks=[]),
        )

        assert "cell がありません" in _rejected(root)

    @pytest.mark.parametrize("pixel_per_mm", [0.0, -1.0])
    def test_reports_a_non_positive_resolution(
        self, tmp_path: Path, pixel_per_mm: float
    ):
        """解像度が正でない session を拒否する.

        conditioning が ``log(pixel_per_mm)`` なので、0 以下だと定義域を外れる。
        """

        root = corrupt_metadata(
            write_session(tmp_path / "session", cells=CELLS),
            lambda document: document["camera"].update(pixel_per_mm=pixel_per_mm),
        )

        assert "pixel_per_mm は正の値が必要です" in _rejected(root)
