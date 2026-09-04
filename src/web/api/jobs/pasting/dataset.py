"""ペースト塗布データセット収集ジョブ."""

from __future__ import annotations

import shutil
from datetime import datetime

from pcbasm.pasting.dataset.capture import DatasetCapturer
from pcbasm.pasting.dataset.metadata import DatasetView
from pcbasm.pasting.dataset.recorder import (
    DatasetRunInfo,
    PasteDatasetRecorder,
    validate_dataset_run,
)
from pcbasm.pasting.dataset.writer import PasteDatasetWriter
from pcbasm.pasting.workflow import DatasetTargets, plan_dataset_targets
from pcbasm.pcb import Pad, PadHierarchy, PcbFile
from pcbasm.vision.crop import PolygonCrop
from web.api.jobs.board_ops import setup_board
from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.context import (
    JobAborted,
    JobContext,
    JobResult,
    PromptSpec,
)
from web.api.jobs.pasting.common import (
    prepare_paste_workflow,
    prompt_positive_number,
    resolve_paste_model,
)


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="paste_dataset_collection",
            label="ペースト塗布データセット収集",
            tab="pasting",
            run=_run_paste_dataset_collection,
            params=(
                ParamSpec("tolerance", "位置合わせ許容誤差", "float", 0.1, unit="mm"),
                ParamSpec(
                    "crop_margin_mm",
                    "画像余白",
                    "float",
                    1.0,
                    unit="mm",
                    minimum=0.0,
                ),
                ParamSpec(
                    "mask_margin_mm",
                    "マスク余白",
                    "float",
                    0.1,
                    unit="mm",
                    minimum=0.0,
                    help="パッド外周からマスクを外側へ広げる距離です。",
                ),
                ParamSpec(
                    "paste_id",
                    "ペースト製品ID",
                    "str",
                    help="メーカー名・製品名または管理用の品番を入力します。",
                ),
                ParamSpec(
                    "paste_lot",
                    "製造ロット（任意）",
                    "str",
                    help="任意。ペースト容器に記載された製造ロット番号を入力します。",
                    optional=True,
                ),
            ),
            requires_pcb=True,
            uses_machine=True,
            notify_on_completion=True,
            accepts_commands=True,
            provides_preview=True,
        )
    )


def _dataset_targets(ctx: JobContext, pcb: PcbFile) -> DatasetTargets:
    """任意PCBからpurgeを除く有効top pad routeを装置非依存で解決する（不正は ValueError）."""
    hierarchy = PadHierarchy.build(pcb.components, pcb.pads)
    model = resolve_paste_model(ctx, hierarchy)
    targets, error = plan_dataset_targets(
        pcb,
        hierarchy,
        model,
        initial_purge_ul=ctx.machine.paste_dispenser.initial_purge_ul,
    )
    if targets is None:
        raise ValueError(error)
    return targets


def _confirm_dataset_collection(ctx: JobContext) -> None:
    calibrated = ctx.prompt(
        PromptSpec(
            kind="confirm",
            message="吐出量キャリブレーションが完了していることを確認してください。",
            default=True,
            true_label="確認済み",
            false_label="中止",
        )
    )
    if not calibrated:
        raise JobAborted()
    tared = ctx.prompt(
        PromptSpec(
            kind="confirm",
            message=(
                "未塗布の基板を電子天秤に載せてTAREし、その基板を装置へ設置してください。"
            ),
            default=True,
            true_label="TARE・設置完了",
            false_label="中止",
        )
    )
    if not tared:
        raise JobAborted()


def _capture(capturer: DatasetCapturer, pad: Pad, view: DatasetView) -> PolygonCrop:
    crop, error = capturer.capture(pad, view)
    if crop is None:
        raise ValueError(f"{pad.designator}.{pad.pad_number} の撮影に失敗: {error}")
    return crop


def _run_paste_dataset_collection(ctx: JobContext) -> JobResult:
    """任意PCBで塗布前後画像と計量教師値を収集・永続化する."""
    assert ctx.pcb_path is not None
    dispenser_config = ctx.machine.paste_dispenser
    paste_id = str(ctx.params["paste_id"]).strip()
    paste_lot = str(ctx.params.get("paste_lot", "")).strip() or None
    crop_margin_mm = float(ctx.params["crop_margin_mm"])
    mask_margin_mm = float(ctx.params["mask_margin_mm"])
    run_error = validate_dataset_run(
        initial_purge_ul=dispenser_config.initial_purge_ul,
        paste_id=paste_id,
        crop_margin_mm=crop_margin_mm,
        mask_margin_mm=mask_margin_mm,
    )
    if run_error is not None:
        raise ValueError(run_error)

    # purge・収集対象の不正はpromptや装置動作より前に検出する。
    _dataset_targets(ctx, PcbFile(ctx.pcb_path))
    _confirm_dataset_collection(ctx)

    started_at = datetime.now().astimezone()
    with ctx.open_camera() as camera:
        result = setup_board(ctx, camera)
        plan = _dataset_targets(ctx, result.pcb)
        prepared = prepare_paste_workflow(
            ctx, result, alignment_pads=plan.alignment_pads
        )
        session = prepared.session
        correction = prepared.correction
        capturer = DatasetCapturer(
            session,
            prepared.alignment_session,
            correction,
            crop_margin_mm=crop_margin_mm,
            mask_margin_mm=mask_margin_mm,
            frame_sink=ctx.frame,
        )
        writer = PasteDatasetWriter.open(
            ctx.paste_dataset_dir,
            board_name=ctx.pcb_path.stem,
            started_at=started_at,
        )
        views = (DatasetView(number=0),)
        recorder = PasteDatasetRecorder(writer, plan, views)

        try:
            for index, pad in enumerate(plan.sample_pads, start=1):
                ctx.progress("塗布前撮影", 100.0 * (index - 1) / len(plan.sample_pads))
                ctx.checkpoint()
                for view in views:
                    recorder.record_pre(index, pad, view, _capture(capturer, pad, view))

            with session.make_applicator() as applicator:
                ctx.progress("リトラクション")
                applicator.retract()
                ctx.progress("パージ")
                ctx.checkpoint()
                recorder.record_execution(
                    plan.purge_pad_id,
                    applicator.deposit_at(
                        plan.purge_pad.center,
                        amount_ul=dispenser_config.initial_purge_ul,
                        transform=session.pad_transform(plan.purge_pad, correction),
                    ),
                )

                for index, pad in enumerate(plan.sample_pads):
                    ctx.progress("塗布", 100.0 * index / len(plan.sample_pads))
                    ctx.checkpoint()
                    recorder.record_execution(
                        plan.hierarchy.pad_id_for_pad(pad),
                        applicator.apply(
                            pad.polygon,
                            params=plan.params_for(pad),
                            transform=session.pad_transform(pad, correction),
                            line_reference=session.component_positions.get(
                                pad.designator
                            ),
                        ),
                    )

            for index, pad in enumerate(plan.sample_pads, start=1):
                ctx.progress("塗布後撮影", 100.0 * (index - 1) / len(plan.sample_pads))
                ctx.checkpoint()
                for view in views:
                    post_error = recorder.record_post(
                        index, pad, view, _capture(capturer, pad, view)
                    )
                    if post_error is not None:
                        raise ValueError(post_error)

            measured_mass_mg = prompt_positive_number(
                ctx,
                "TAREした電子天秤で塗布済み基板を計量し、増加質量 [mg] を入力してください。",
            )
            assert measured_mass_mg is not None
            session_path = recorder.finalize(
                measured_mass_mg=measured_mass_mg,
                run=DatasetRunInfo(
                    machine_id=ctx.machine_id,
                    machine_name=ctx.machine.machine_name,
                    pcb_filename=ctx.pcb_path.name,
                    source_pcb=ctx.source_pcb or ctx.pcb_path.as_posix(),
                    paste_id=paste_id,
                    paste_lot=paste_lot,
                    crop_margin_mm=crop_margin_mm,
                    mask_margin_mm=mask_margin_mm,
                    started_at=started_at,
                    dispenser=dispenser_config,
                    calibration=result.calibration,
                ),
            )
        except Exception:
            incomplete = recorder.mark_incomplete()
            ctx.log(f"未完了datasetを保持しました: {incomplete}")
            raise

    metadata = recorder.metadata
    assert metadata is not None
    archive_name = f"paste-dataset-{session_path.name}.zip"
    archive_path = ctx.artifacts_dir / archive_name
    shutil.make_archive(str(archive_path.with_suffix("")), "zip", root_dir=session_path)
    ctx.log(f"datasetを保存しました: {session_path}")
    return JobResult(
        summary=(
            f"dataset収集完了: {len(plan.sample_pads)} pads / "
            f"{metadata.total.measured_mass_mg:.3f} mg / "
            f"{metadata.total.measured_volume_ul:.6f} uL"
        ),
        artifacts=(ctx.artifact("ペースト塗布dataset", archive_name, "file"),),
    )
