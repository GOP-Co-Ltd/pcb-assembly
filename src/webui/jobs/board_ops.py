"""ボード計測セットアップと銅箔照合ループの共有処理."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import attrs

from pcbasm.geometry import Transform
from pcbasm.hal import Camera
from pcbasm.pasting import (
    PasteSettingsModel,
    base_override_from_config,
)
from pcbasm.pcb import Layer, PadHierarchy
from pcbasm.posctrl import (
    BoardCalibrationResult,
    ComponentPads,
    PadAlignmentCandidates,
    PadAlignmentResult,
    PadAlignments,
    PadAlignmentSession,
    PadAlignmentTarget,
    setup_board_calibration,
)
from webui.jobs.context import JobContext


def setup_board(
    ctx: JobContext,
    camera: Camera,
    *,
    tolerance: float | None = None,
    pcb_path: Path | None = None,
) -> BoardCalibrationResult:
    """Progress("セットアップ") → ボード計測セットアップの定型.

    Args:
        ctx: 実行中ジョブのコンテキスト
        camera: 撮像に使うカメラ
        tolerance: 位置合わせ許容誤差。省略時は ``ctx.params["tolerance"]``
        pcb_path: 計測対象の PCB。省略時は選択 PCB（``ctx.pcb_path``）
    """
    if tolerance is None:
        tolerance = float(ctx.params["tolerance"])
    if pcb_path is None:
        assert ctx.pcb_path is not None  # requires_pcb=True
        pcb_path = ctx.pcb_path
    ctx.progress("セットアップ")
    return setup_board_calibration(
        machine=ctx.machine,
        pcb_file_path=pcb_path,
        tolerance=tolerance,
        camera=camera,
        frame_sink=ctx.frame,
    )


def load_board_paste_settings(
    ctx: JobContext, hierarchy: PadHierarchy
) -> PasteSettingsModel:
    """基板保存設定を読み込み、ストア未配線時はmachine既定モデルを返す."""
    if ctx.board_store is not None and ctx.source_pcb is not None:
        return ctx.board_store.load_or_init(
            ctx.machine_name,
            ctx.source_pcb,
            ctx.machine.paste_dispenser,
            board_signature=hierarchy.signature(),
        )
    return PasteSettingsModel(
        base=base_override_from_config(ctx.machine.paste_dispenser),
        base_enabled=True,
    )


def top_pad_alignment_targets(
    hierarchy: PadHierarchy,
) -> tuple[PadAlignmentTarget, ...]:
    """階層の全TOP padを一意なpad id付き位置合わせ対象へ変換する."""
    return tuple(
        PadAlignmentTarget(
            identifier=hierarchy.pad_id_for_pad(pad),
            pad=pad,
        )
        for pad in hierarchy.iter_pads()
        if pad.layer == Layer.TOP
    )


@attrs.frozen
class PadAlignmentExecution:
    """目標成功数型pad照合executorの結果とログ用集計."""

    alignments: PadAlignments
    failed_identifiers: tuple[str, ...]
    candidate_count: int
    attempt_count: int
    configured_target_count: int
    target_count: int
    preferred_component_count: int
    rejected_count: int

    @property
    def success_count(self) -> int:
        """照合成功数."""
        return len(self.alignments.results)

    @property
    def failure_count(self) -> int:
        """照合失敗数."""
        return len(self.failed_identifiers)


PadFailureCallback = Callable[[PadAlignmentTarget, int, int, int, int], None]


def align_pad_targets(
    ctx: JobContext,
    session: PadAlignmentSession,
    candidates: PadAlignmentCandidates,
    *,
    board_transform: Transform,
    sample_count: int,
    max_failures: int,
    on_failure: PadFailureCallback | None = None,
) -> PadAlignmentExecution:
    """安全候補を順に照合し、目標成功数まで失敗を次候補で補充する."""
    candidate_count = len(candidates.targets)
    target_count = min(sample_count, candidate_count)
    ctx.log(
        f"安全候補: Component分散 {candidates.preferred_component_count} / "
        f"全 {candidate_count} / 曖昧除外 {candidates.rejected_count}"
    )
    if candidate_count == 0:
        raise ValueError("安全に銅箔照合できるTOP padがありません")
    ctx.log(f"設定成功数: {sample_count} / 実行時目標: {target_count}")
    if target_count < sample_count:
        ctx.log(
            f"警告: 目標成功数を {sample_count} から安全候補数 "
            f"{target_count} へ下げます"
        )

    aligned: list[tuple[PadAlignmentTarget, PadAlignmentResult]] = []
    failed: list[str] = []
    attempts = 0
    for index, target in enumerate(candidates.targets):
        if len(aligned) >= target_count:
            break
        ctx.progress("銅箔照合", 100.0 * len(aligned) / target_count)
        ctx.checkpoint()
        attempts += 1
        alignment = session.align_pad(target)
        if alignment is None:
            failed.append(target.identifier)
            ctx.log(
                f"警告: {target.identifier} の照合に失敗、次候補へ"
                f"（成功 {len(aligned)}/{target_count}・"
                f"失敗 {len(failed)}/{max_failures}）"
            )
            if on_failure is not None:
                on_failure(
                    target,
                    index,
                    len(aligned),
                    len(failed),
                    target_count,
                )
            if len(failed) > max_failures:
                raise ValueError(
                    "銅箔照合の失敗pad数が許容数を超えました"
                    f"（失敗 {len(failed)} / 許容 {max_failures}）: "
                    f"{', '.join(failed)}。基板の向き・種類を確認してください"
                )
            continue

        translation = alignment.translation
        ctx.log(
            f"{target.identifier}: dx={translation.x:+.4f} "
            f"dy={translation.y:+.4f} mm, "
            f"theta={alignment.rotation.degrees:+.3f} deg, "
            f"mean_distance={alignment.match.mean_distance_px:.2f} px"
        )
        aligned.append((target, alignment))

    if len(aligned) < target_count:
        raise ValueError(
            "安全候補を使い切りましたが銅箔照合の目標成功数へ届きません"
            f"（成功 {len(aligned)} / 目標 {target_count}・"
            f"試行 {attempts} / 候補 {candidate_count}）"
        )

    alignments = PadAlignments(
        board_transform=board_transform,
        results=tuple(aligned),
    )
    mean = alignments.average_translation()
    ctx.log(
        f"平均補正: 成功 {len(aligned)} / 試行 {attempts}, "
        f"補充 {len(failed)}, "
        f"dx={mean.x:+.4f} dy={mean.y:+.4f} mm"
    )
    return PadAlignmentExecution(
        alignments=alignments,
        failed_identifiers=tuple(failed),
        candidate_count=candidate_count,
        attempt_count=attempts,
        configured_target_count=sample_count,
        target_count=target_count,
        preferred_component_count=candidates.preferred_component_count,
        rejected_count=candidates.rejected_count,
    )


def pad_align_abort_message(
    failed_designators: Sequence[str], max_failures: int | None
) -> str | None:
    """照合失敗数が許容数を超えたときの中止メッセージを返す.

    Args:
        failed_designators: 照合に失敗した部品の designator 一覧
        max_failures: 許容する失敗部品数。None は無制限

    Returns:
        許容内（失敗数 <= 許容数）または無制限なら None。
        超過なら失敗数・許容数・全 designator を含むメッセージ文字列。
    """
    if max_failures is None or len(failed_designators) <= max_failures:
        return None
    return (
        f"銅箔照合の失敗部品数が許容数を超えました"
        f"（失敗 {len(failed_designators)} / 許容 {max_failures}）: "
        f"{', '.join(failed_designators)}。基板の向き・種類を確認してください"
    )


def align_component_groups(
    ctx: JobContext,
    session: PadAlignmentSession,
    groups: Sequence[ComponentPads],
    *,
    on_failure: Callable[[ComponentPads, int], None] | None = None,
    max_failures: int | None = None,
) -> list[tuple[ComponentPads, PadAlignmentResult]]:
    """部品単位の銅箔照合ループの共通骨格.

    部品ごとに progress("銅箔照合") → checkpoint → ``session.align`` →
    dx/dy/theta/mean_distance の log を行い、成功した (group, alignment) を
    集めて返す。失敗は警告 log の後 ``on_failure``（あれば）を呼んで続行する
    （board_tour が失敗 overlay の配信に使う）。

    Args:
        ctx: 実行中ジョブのコンテキスト
        session: 銅箔照合セッション
        groups: 照合対象の部品グループ
        on_failure: 照合失敗時に呼ぶコールバック（group, index）
        max_failures: 失敗部品数の許容数。超過した時点で ValueError を送出し
            即中止。None は無制限（board_tour が使用）

    Raises:
        ValueError: 失敗部品数が ``max_failures`` を超えた場合
    """
    aligned: list[tuple[ComponentPads, PadAlignmentResult]] = []
    failed: list[str] = []
    for index, group in enumerate(groups):
        ctx.progress("銅箔照合", 100.0 * index / len(groups))
        ctx.checkpoint()
        designator = group.component.designator
        alignment = session.align(group)
        if alignment is None:
            ctx.log(f"警告: {designator} の照合に失敗")
            if on_failure is not None:
                on_failure(group, index)
            failed.append(designator)
            message = pad_align_abort_message(failed, max_failures)
            if message is not None:
                raise ValueError(message)
            continue
        translation = alignment.translation
        ctx.log(
            f"{designator}: dx={translation.x:+.4f} dy={translation.y:+.4f} mm, "
            f"theta={alignment.rotation.degrees:+.3f} deg, "
            f"mean_distance={alignment.match.mean_distance_px:.2f} px"
        )
        aligned.append((group, alignment))
    return aligned
