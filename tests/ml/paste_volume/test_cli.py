"""運用 CLI（dataset validate / summarize）の公開契約.

``validate`` と ``summarize`` が同じ公開 API を通ることを、単一 root と複数 root の
両方で確かめる。

走査・重複除去・fingerprint を 2 度書くと、片方だけが schema 変更へ追随する。

「単一と複数で同じ」型の検査は、違う入力なら違う結果になることを対で置く
（``memory/negative-assertion-needs-self-check.md``）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ml.data.image import ImageConstraints
from ml.paste_volume.cli import DatasetSummary, main, summarize_dataset
from tests.ml.paste_volume.helpers import (
    SyntheticCell,
    corrupt_metadata,
    session_measured_ratio,
    write_session,
    write_synthetic_sessions,
)

SESSION_COUNT = 3
CELL_COUNT = 6
BLANK_COUNT = 1


@pytest.fixture(scope="module")
def dataset_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("paste-volume-cli")
    write_synthetic_sessions(
        root,
        session_count=SESSION_COUNT,
        cell_count=CELL_COUNT,
        blank_count=BLANK_COUNT,
    )
    return root


def _summary(*roots: Path) -> DatasetSummary:
    summary, error = summarize_dataset(list(roots), constraints=ImageConstraints())

    assert error is None, error
    assert summary is not None
    return summary


class TestSummarizeDataset:
    """収集 dataset の要約."""

    def test_it_counts_samples_and_blanks_apart(self, dataset_root: Path):
        """真値 0 の blank を学習 sample と同じ母数に入れないこと.

        blank は相対誤差の集計に一切現れないので、混ぜると分布の要約が実際の学習 sample を表さなくなる。
        """

        summary = _summary(dataset_root)

        assert summary.session_count == SESSION_COUNT
        assert summary.sample_count == SESSION_COUNT * (CELL_COUNT - BLANK_COUNT)
        assert summary.blank_count == SESSION_COUNT * BLANK_COUNT
        assert summary.rejection_count == 0

    def test_it_reports_the_measured_volume_range(self, dataset_root: Path):
        """``mean_bias_initial`` の妥当性を測る観測点になること."""

        summary = _summary(dataset_root)

        # k は session ごとに違うので、最小の真値は最小 k の session から出る。
        assert summary.measured_volume_minimum_ul == pytest.approx(
            0.05 * session_measured_ratio(SESSION_COUNT - 1)
        )
        assert (
            summary.measured_volume_minimum_ul
            < summary.measured_volume_mean_ul
            < summary.measured_volume_maximum_ul
        )

    def test_the_cell_group_is_orthogonal_to_the_session(self, dataset_root: Path):
        """Cell group が session をまたいで同一値になること.

        ``_cell_key`` は座標由来なので、同じ配置で撮った session どうしでは
        cell group が重なる。cell 単位 split がどの split にも全 session を
        入れてしまう根拠がここに出る。
        """

        summary = _summary(dataset_root)

        assert summary.cell_group_count == CELL_COUNT

    def test_a_single_root_and_the_session_directories_agree(self, dataset_root: Path):
        """親 root 1 個と、その下の session を並べた指定が同じ要約になること."""

        sessions = sorted(child for child in dataset_root.iterdir())

        assert _summary(dataset_root) == _summary(*sessions)

    def test_a_smaller_root_set_gives_a_different_summary(self, dataset_root: Path):
        """入力が違えば要約も違うこと.

        上の一致が「どんな入力でも同じ値を返す」形に退化していないことを見る。
        """

        sessions = sorted(child for child in dataset_root.iterdir())

        assert _summary(*sessions[:2]) != _summary(*sessions)

    def test_it_deduplicates_a_session_given_twice(self, dataset_root: Path):
        """同じ session を 2 度渡しても 1 回ぶんとして数えること."""

        sessions = sorted(child for child in dataset_root.iterdir())

        assert _summary(*sessions, sessions[0]) == _summary(*sessions)

    def test_it_reports_roots_without_a_session(self, tmp_path: Path):
        summary, error = summarize_dataset(
            [tmp_path / "empty"], constraints=ImageConstraints()
        )

        assert summary is None
        assert error is not None
        assert "session" in error


class TestDatasetCommands:
    """``dataset validate`` と ``dataset summarize``."""

    def test_validate_reports_the_dataset_fingerprint(
        self, dataset_root: Path, capsys: pytest.CaptureFixture[str]
    ):
        code = main(["dataset", "validate", str(dataset_root)])

        assert code == 0
        assert _summary(dataset_root).dataset_fingerprint in capsys.readouterr().out

    def test_validate_fails_on_a_broken_session(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        root = write_session(tmp_path / "broken", cells=(SyntheticCell(index=0),))
        corrupt_metadata(root, lambda document: document.pop("samples"))

        code = main(["dataset", "validate", str(root)])

        assert code == 1
        assert capsys.readouterr().err.strip() != ""

    def test_summarize_prints_one_line_per_session(
        self, dataset_root: Path, capsys: pytest.CaptureFixture[str]
    ):
        code = main(["dataset", "summarize", str(dataset_root)])

        printed = capsys.readouterr().out
        assert code == 0
        for session in _summary(dataset_root).sessions:
            assert session.label in printed

    def test_summarize_emits_the_same_summary_as_json(
        self, dataset_root: Path, capsys: pytest.CaptureFixture[str]
    ):
        code = main(["dataset", "summarize", "--json", str(dataset_root)])

        printed = json.loads(capsys.readouterr().out)
        assert code == 0
        assert printed["dataset_fingerprint"] == (
            _summary(dataset_root).dataset_fingerprint
        )
        assert len(printed["sessions"]) == SESSION_COUNT

    def test_summarize_takes_several_roots(
        self, dataset_root: Path, capsys: pytest.CaptureFixture[str]
    ):
        sessions = sorted(child for child in dataset_root.iterdir())

        code = main(
            ["dataset", "summarize", "--json", *(str(root) for root in sessions)]
        )

        printed = json.loads(capsys.readouterr().out)
        assert code == 0
        assert printed["dataset_fingerprint"] == (
            _summary(dataset_root).dataset_fingerprint
        )

    def test_it_requires_a_command(self, dataset_root: Path):
        """Subcommand を省いた呼び出しを parser が拒否すること."""

        with pytest.raises(SystemExit):
            main(["dataset"])
