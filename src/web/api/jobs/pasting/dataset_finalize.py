"""未完了ペースト塗布 dataset の確定ジョブ（``pending.json`` + 計量質量 → metadata）.

収集ジョブは計量質量のプロンプトの前に ``pending.json`` を残す。そこで応答できずに
中断したセッションを、装置を動かさずに完成 dataset へ確定させる。配分規則・schema・
確定手順は :mod:`pcbasm.pasting.dataset` 側にあり、ここは選択 UI と成果物への変換だけ
を担う。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from pcbasm.pasting.dataset.pending import (
    PENDING_FILENAME,
    PasteDatasetPending,
    finalize_pending,
    parse_pending,
)
from pcbasm.pasting.dataset.writer import finalize_incomplete, rescuable_sessions
from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.context import JobContext, JobResult, PromptSpec


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="paste_dataset_finalize",
            label="未完了ペーストdatasetの確定",
            tab="pasting",
            run=_run_paste_dataset_finalize,
            params=(
                ParamSpec(
                    "measured_mass",
                    "計量した増加質量",
                    "float",
                    unit="mg",
                    help=(
                        "TAREした電子天秤で塗布済み銅板を計量した増加質量です。"
                        "収集ジョブの最後に入力するはずだった値を入れます。"
                    ),
                ),
            ),
            requires_pcb=False,
            uses_machine=False,
        )
    )


def _run_paste_dataset_finalize(ctx: JobContext) -> JobResult:
    """撮影済みの未完了 session へ metadata.json を書き、完成名へ確定する.

    画像は書き直さない（未確定 directory を rename するだけ）ので、収集本経路が書いたものがそのまま残る。
    """
    measured_mass_mg = float(ctx.params["measured_mass"])
    if measured_mass_mg <= 0:
        raise ValueError(f"計量質量は正の値が必要です: {measured_mass_mg!r}")
    sessions = rescuable_sessions(ctx.paste_dataset_dir)
    if not sessions:
        raise ValueError(
            f"確定できる未完了datasetがありません（{PENDING_FILENAME} を持つ "
            f"*.incomplete / .*.tmp が対象です）: {ctx.paste_dataset_dir}"
        )

    answer = ctx.prompt(
        PromptSpec(
            kind="choice",
            message="確定する未完了datasetを選んでください。",
            default=sessions[-1].name,
            choices=tuple(session.name for session in sessions),
        )
    )
    incomplete = ctx.paste_dataset_dir / str(answer)
    pending = _load_pending(incomplete)
    ctx.log(
        f"塗布 {len(pending.samples)} 点 + blank {len(pending.blanks)} 点 / "
        f"配置シード {pending.config.shuffle_seed} / "
        f"収集開始 {pending.created_at}"
    )

    metadata, error = finalize_pending(pending, measured_mass_mg=measured_mass_mg)
    if metadata is None:
        raise ValueError(error)
    session_path, error = finalize_incomplete(incomplete, metadata)
    if session_path is None:
        raise ValueError(error)

    archive_name = f"paste-dataset-{session_path.name}.zip"
    shutil.make_archive(
        str((ctx.artifacts_dir / archive_name).with_suffix("")),
        "zip",
        root_dir=session_path,
    )
    ctx.log(f"datasetを確定しました: {session_path}")
    return JobResult(
        summary=(
            f"dataset確定完了: 塗布 {len(metadata.samples)} 点 + blank "
            f"{len(metadata.blanks)} 点 / {metadata.total.measured_mass_mg:.3f} mg / "
            f"{metadata.total.measured_volume_ul:.6f} uL"
        ),
        artifacts=(ctx.artifact("ペースト塗布dataset", archive_name, "file"),),
    )


def _load_pending(session: Path) -> PasteDatasetPending:
    """未完了 session の ``pending.json`` を読む（不正は ValueError）."""
    path = session / PENDING_FILENAME
    if not path.is_file():
        raise ValueError(f"{PENDING_FILENAME} がありません: {session}")
    pending, error = parse_pending(json.loads(path.read_text(encoding="utf-8")))
    if pending is None:
        raise ValueError(f"{path}: {error}")
    return pending
