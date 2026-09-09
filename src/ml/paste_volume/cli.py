"""Experiment config を要らない運用 CLI.

学習 entrypoint と subcommand parser に同じ argv を処理させない（仕様書 §7）。

``train`` / ``evaluate`` / ``search`` は ``group=option`` と ``key=value`` だけを
受け取る独立 module で、ここはその中継をしない。

扱うのは収集 dataset の検証と要約だけ。

``export`` / ``optimize`` / ``benchmark`` / ``infer`` は Phase 4 の成果物を
入力にするので、その MR で足す。

この module は mlflow も optuna も import しない。

``ml-runtime`` だけを install した Raspberry Pi 5 で dataset を確かめられる
ようにするためで、この契約は ``tests/ml/test_architecture.py`` が機械検証する。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Sequence
from pathlib import Path

import attrs

from ml.data.image import ImageConstraints
from ml.paste_volume.index import PasteVolumeSampleIndex
from ml.serialization import make_strict_converter

_PROGRAM_NAME = "python -m ml.paste_volume.cli"


@attrs.frozen
class SessionSummary:
    """1 collection session の要約."""

    label: str
    session_fingerprint: str
    machine_id: str
    sample_count: int
    blank_count: int
    pixel_per_mm: float
    measured_volume_mean_ul: float


@attrs.frozen
class DatasetSummary:
    """複数 root にまたがる収集 dataset の要約.

    ``sample_count`` は blank を含まない学習 sample の数。

    真値が 0 の blank は相対誤差の集計に一切現れないので、母数を分けて数える。

    ``measured_volume_mean_ul`` は ``PasteVolumeModelConfig.mean_bias_initial``
    の妥当性を測る観測点でもある。
    """

    session_count: int
    sample_count: int
    blank_count: int
    rejection_count: int
    dataset_fingerprint: str
    smallest_source_size: int
    cell_group_count: int
    measured_volume_minimum_ul: float
    measured_volume_maximum_ul: float
    measured_volume_mean_ul: float
    sessions: tuple[SessionSummary, ...]


def summarize_dataset(
    roots: Sequence[Path], *, constraints: ImageConstraints
) -> tuple[DatasetSummary | None, str | None]:
    """単一 root でも複数 root でも同じ形で dataset を要約する.

    ``validate`` と ``summarize`` はこの 1 本の公開 API を共有する。

    走査・重複除去・fingerprint の作り方を 2 箇所へ書くと、片方だけが将来の
    schema 変更に追随して結果が食い違う。
    """

    index, error = PasteVolumeSampleIndex.from_roots(roots, constraints=constraints)
    if index is None:
        return None, error
    measured = [
        entry.measured_volume_ul for entry in index.entries if not entry.is_blank
    ]
    if not measured:
        return None, "blank しかない dataset です"
    sessions = tuple(
        _session_summary(index, fingerprint=fingerprint)
        for fingerprint in index.session_values()
    )
    return (
        DatasetSummary(
            session_count=len(sessions),
            sample_count=len(measured),
            blank_count=sum(1 for entry in index.entries if entry.is_blank),
            rejection_count=len(index.rejections),
            dataset_fingerprint=index.dataset_fingerprint,
            smallest_source_size=index.smallest_source_size,
            cell_group_count=len(set(index.sample_groups(dimension="cell").values())),
            measured_volume_minimum_ul=min(measured),
            measured_volume_maximum_ul=max(measured),
            measured_volume_mean_ul=statistics.fmean(measured),
            sessions=sessions,
        ),
        None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """運用 CLI。0 は成功、1 は理由を stderr へ出して失敗."""

    parser = _parser()
    arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    roots = [Path(root) for root in arguments.roots]
    summary, error = summarize_dataset(roots, constraints=ImageConstraints())
    if summary is None:
        print(error, file=sys.stderr)
        return 1
    if arguments.command == "validate":
        print(_validation_lines(roots, summary))
        return 0
    if arguments.json:
        print(
            json.dumps(
                make_strict_converter().unstructure(summary),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(_summary_lines(summary))
    return 0


def _parser() -> argparse.ArgumentParser:
    """``dataset`` group だけを持つ parser を組む."""

    parser = argparse.ArgumentParser(prog=_PROGRAM_NAME)
    groups = parser.add_subparsers(dest="group", required=True)
    dataset = groups.add_parser("dataset", help="収集 dataset の検証と要約")
    commands = dataset.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="読み込みと構造検証だけを行う")
    validate.add_argument("roots", nargs="+", help="session directory かその親")
    summarize = commands.add_parser("summarize", help="件数と体積の分布を出す")
    summarize.add_argument("roots", nargs="+", help="session directory かその親")
    summarize.add_argument("--json", action="store_true", help="JSON で出力する")
    return parser


def _session_summary(
    index: PasteVolumeSampleIndex, *, fingerprint: str
) -> SessionSummary:
    entries = [
        entry for entry in index.entries if entry.session_fingerprint == fingerprint
    ]
    measured = [entry.measured_volume_ul for entry in entries if not entry.is_blank]
    first = entries[0]
    return SessionSummary(
        label=first.session_label,
        session_fingerprint=fingerprint,
        machine_id=first.machine_id,
        sample_count=len(measured),
        blank_count=sum(1 for entry in entries if entry.is_blank),
        pixel_per_mm=first.pixel_per_mm,
        measured_volume_mean_ul=statistics.fmean(measured) if measured else 0.0,
    )


def _validation_lines(roots: Sequence[Path], summary: DatasetSummary) -> str:
    """検証結果を人が読む形へ.

    隔離した cell は失敗にしない。

    使えない cell を除いて index を組むのは設計どおりで、件数と理由を見せる。
    """

    lines = [
        f"roots: {', '.join(str(root) for root in roots)}",
        f"sessions: {summary.session_count}",
        f"samples: {summary.sample_count}（blank {summary.blank_count}）",
        f"rejections: {summary.rejection_count}",
        f"dataset_fingerprint: {summary.dataset_fingerprint}",
    ]
    return "\n".join(lines)


def _summary_lines(summary: DatasetSummary) -> str:
    """要約を人が読む形へ."""

    lines = [
        f"sessions: {summary.session_count}",
        f"samples: {summary.sample_count}（blank {summary.blank_count}）",
        f"rejections: {summary.rejection_count}",
        f"cell groups: {summary.cell_group_count}",
        f"smallest source size: {summary.smallest_source_size} px",
        (
            "measured [uL]: "
            f"min {summary.measured_volume_minimum_ul:.4f} / "
            f"max {summary.measured_volume_maximum_ul:.4f} / "
            f"mean {summary.measured_volume_mean_ul:.4f}"
        ),
        f"dataset_fingerprint: {summary.dataset_fingerprint}",
        "",
    ]
    for session in summary.sessions:
        lines.append(
            f"  {session.label}: n {session.sample_count}"
            f"（blank {session.blank_count}）"
            f" mean {session.measured_volume_mean_ul:.4f} uL"
            f" pixel_per_mm {session.pixel_per_mm:.6f}"
            f" fingerprint {session.session_fingerprint}"
        )
    return "\n".join(lines)


__all__ = [
    "DatasetSummary",
    "SessionSummary",
    "main",
    "summarize_dataset",
]


if __name__ == "__main__":
    raise SystemExit(main())
