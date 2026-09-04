"""ペースト塗布データセット収集ジョブ."""

from __future__ import annotations

import shutil
from datetime import datetime

import attrs

from pcbasm import gcode
from pcbasm.geometry import (
    Point2d,
)
from pcbasm.pasting.applicator import (
    PasteApplicationResult,
)
from pcbasm.pasting.params import PasteParams
from pcbasm.pasting.paste_dataset import (
    DatasetCapturedView,
    DatasetExecution,
    DatasetPolygon,
    DatasetResolvedPaste,
    DatasetView,
    PadImageCrop,
    PasteDatasetBoard,
    PasteDatasetCamera,
    PasteDatasetConfig,
    PasteDatasetMachine,
    PasteDatasetMetadata,
    PasteDatasetNozzle,
    PasteDatasetPad,
    PasteDatasetPaste,
    PasteDatasetPurge,
    PasteDatasetTotal,
    PasteDatasetWriter,
    allocate_volume_by_rotations,
    crop_pad_image,
    validate_dataset_image_margins,
)
from pcbasm.pasting.workflow import DatasetTargets, plan_dataset_targets
from pcbasm.pcb import (
    Pad,
    PcbFile,
    build_pad_hierarchy,
)
from pcbasm.posctrl import (
    BoardCalibrationResult,
)
from web.api.jobs.board_ops import setup_board
from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.context import (
    JobAborted,
    JobContext,
    JobResult,
    PromptSpec,
)
from web.api.jobs.pasting.common import (
    PreparedPasteWorkflow,
    prepare_paste_workflow,
    prompt_positive_number,
    resolve_paste_model,
)

_DATASET_CAPTURE_SETTLE_TIME = 0.5


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
    hierarchy = build_pad_hierarchy(pcb.components, pcb.pads)
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


def _capture_dataset_pad(
    ctx: JobContext,
    result: BoardCalibrationResult,
    prepared: PreparedPasteWorkflow,
    pad: Pad,
    view: DatasetView,
    *,
    margin_mm: float,
    mask_margin_mm: float,
) -> PadImageCrop:
    """補正済みpad位置へcameraを動かしてcrop/maskを1組取得する."""
    correction = prepared.correction.alignment.correction_for(
        pad.center, designator=pad.designator
    )
    target = prepared.session.camera_target(
        pad, prepared.correction, offset=Point2d(view.offset_x_mm, view.offset_y_mm)
    )
    result.klipper.send_gcode(
        result.stage.move(
            x=target.x,
            y=target.y,
            z=result.calibration.z_position,
        )
        + gcode.wait(_DATASET_CAPTURE_SETTLE_TIME)
        + gcode.wait_for_done()
    )
    image = result.camera.capture()
    ctx.frame(image)
    projector = prepared.alignment_session.projector.with_correction(correction)
    matrix, shift = projector.board_to_pixel_affine(target)
    return crop_pad_image(
        image,
        pad.polygon,
        matrix,
        shift,
        margin_mm=margin_mm,
        mask_margin_mm=mask_margin_mm,
    )


def _dataset_execution(result: PasteApplicationResult) -> DatasetExecution:
    return DatasetExecution(**attrs.asdict(result.summary))


def _dataset_resolved(params: PasteParams) -> DatasetResolvedPaste:
    return DatasetResolvedPaste(**params.to_dict())


def _run_paste_dataset_collection(ctx: JobContext) -> JobResult:
    """任意PCBで塗布前後画像と計量教師値を収集・永続化する."""
    assert ctx.pcb_path is not None
    dispenser_config = ctx.machine.paste_dispenser
    if dispenser_config.initial_purge_ul <= 0:
        raise ValueError("dataset収集にはinitial_purge_ulを正の値で設定してください")
    paste_id = str(ctx.params["paste_id"]).strip()
    paste_lot = str(ctx.params.get("paste_lot", "")).strip() or None
    if not paste_id:
        raise ValueError("paste_idは空にできません")
    crop_margin_mm = float(ctx.params["crop_margin_mm"])
    mask_margin_mm = float(ctx.params["mask_margin_mm"])
    margin_error = validate_dataset_image_margins(crop_margin_mm, mask_margin_mm)
    if margin_error is not None:
        raise ValueError(margin_error)

    # purge・収集対象の不正はpromptや装置動作より前に検出する。
    _dataset_targets(ctx, PcbFile(ctx.pcb_path))
    _confirm_dataset_collection(ctx)

    started_at = datetime.now().astimezone()
    writer: PasteDatasetWriter | None = None
    with ctx.open_camera() as camera:
        result = setup_board(ctx, camera)
        plan = _dataset_targets(ctx, result.pcb)
        prepared = prepare_paste_workflow(
            ctx, result, alignment_pads=plan.alignment_pads
        )
        session = prepared.session
        correction = prepared.correction
        writer = PasteDatasetWriter(
            ctx.paste_dataset_dir,
            board_name=ctx.pcb_path.stem,
            started_at=started_at,
        )
        central_view = DatasetView(number=0)
        views = (central_view,)
        captured_views: dict[tuple[str, int], DatasetCapturedView] = {}
        executions: dict[str, PasteApplicationResult] = {}

        try:
            for index, pad in enumerate(plan.sample_pads, start=1):
                pad_id = plan.hierarchy.pad_id_for_pad(pad)
                ctx.progress("塗布前撮影", 100.0 * (index - 1) / len(plan.sample_pads))
                ctx.checkpoint()
                for view in views:
                    crop = _capture_dataset_pad(
                        ctx,
                        result,
                        prepared,
                        pad,
                        view,
                        margin_mm=crop_margin_mm,
                        mask_margin_mm=mask_margin_mm,
                    )
                    captured_views[(pad_id, view.number)] = writer.write_capture(
                        index, view, "pre", crop
                    )

            with session.make_applicator() as applicator:
                ctx.progress("リトラクション")
                applicator.retract()
                ctx.progress("パージ")
                ctx.checkpoint()
                purge_result = applicator.deposit_at(
                    plan.purge_pad.center,
                    amount_ul=dispenser_config.initial_purge_ul,
                    transform=session.pad_transform(plan.purge_pad, correction),
                )
                executions[plan.purge_pad_id] = purge_result

                for index, pad in enumerate(plan.sample_pads):
                    ctx.progress("塗布", 100.0 * index / len(plan.sample_pads))
                    ctx.checkpoint()
                    pad_id = plan.hierarchy.pad_id_for_pad(pad)
                    executions[pad_id] = applicator.apply(
                        pad.polygon,
                        params=plan.params_for(pad),
                        transform=session.pad_transform(pad, correction),
                        line_reference=session.component_positions.get(pad.designator),
                    )

            for index, pad in enumerate(plan.sample_pads, start=1):
                pad_id = plan.hierarchy.pad_id_for_pad(pad)
                ctx.progress("塗布後撮影", 100.0 * (index - 1) / len(plan.sample_pads))
                ctx.checkpoint()
                for view in views:
                    crop = _capture_dataset_pad(
                        ctx,
                        result,
                        prepared,
                        pad,
                        view,
                        margin_mm=crop_margin_mm,
                        mask_margin_mm=mask_margin_mm,
                    )
                    post_view = writer.write_capture(index, view, "post", crop)
                    if (
                        post_view.pixel_rect
                        != captured_views[(pad_id, view.number)].pixel_rect
                    ):
                        raise ValueError(f"{pad_id} のpre/post crop位置が一致しません")

            measured_mass_mg = prompt_positive_number(
                ctx,
                "TAREした電子天秤で塗布済み基板を計量し、増加質量 [mg] を入力してください。",
            )
            assert measured_mass_mg is not None
            measured_volume_ul = (
                measured_mass_mg / dispenser_config.solder_paste_density
            )
            rotations = {
                key: execution.summary.rotations
                for key, execution in executions.items()
            }
            allocated = allocate_volume_by_rotations(measured_volume_ul, rotations)
            purge_execution = executions[plan.purge_pad_id]
            pads_metadata = tuple(
                PasteDatasetPad(
                    index=index,
                    pad_id=(pad_id := plan.hierarchy.pad_id_for_pad(pad)),
                    source_pad_id=f"{pad.designator}.{pad.pad_number}",
                    polygon=DatasetPolygon.from_polygon(pad.polygon),
                    resolved=_dataset_resolved(plan.params_for(pad)),
                    execution=_dataset_execution(executions[pad_id]),
                    measured_volume_ul=allocated[pad_id],
                    views=tuple(
                        captured_views[(pad_id, view.number)] for view in views
                    ),
                )
                for index, pad in enumerate(plan.sample_pads, start=1)
            )
            calibration = result.calibration
            metadata = PasteDatasetMetadata(
                kind="pcbasm-paste-volume-dataset",
                schema_version=1,
                created_at=started_at.isoformat(),
                machine=PasteDatasetMachine(
                    machine_id=ctx.machine_id,
                    name=ctx.machine.machine_name,
                ),
                board=PasteDatasetBoard(
                    filename=ctx.pcb_path.name,
                    source_pcb=ctx.source_pcb or ctx.pcb_path.as_posix(),
                    signature=plan.hierarchy.signature(),
                ),
                paste=PasteDatasetPaste(
                    paste_id=paste_id,
                    lot=paste_lot,
                    density_mg_per_ul=dispenser_config.solder_paste_density,
                ),
                camera=PasteDatasetCamera(
                    pixel_per_mm=calibration.pixel_per_mm,
                    resolution=calibration.resolution,
                    calibrated_at=calibration.calibrated_at.isoformat(),
                    z_position_mm=calibration.z_position,
                ),
                nozzle=PasteDatasetNozzle(diameter_mm=dispenser_config.nozzle_diameter),
                config=PasteDatasetConfig(
                    rotations_per_ul=dispenser_config.rotations_per_ul,
                    max_fill_speed_mm_s=dispenser_config.max_fill_speed,
                    max_dispense_rate_ul_s=dispenser_config.max_dispense_rate,
                    dispense_accel_ul_s2=dispenser_config.dispense_accel,
                    retract_amount_ul=dispenser_config.retract_amount,
                    retract_rate_ul_s=dispenser_config.effective_retract_rate,
                    initial_purge_ul=dispenser_config.initial_purge_ul,
                    crop_margin_mm=crop_margin_mm,
                    mask_margin_mm=mask_margin_mm,
                ),
                total=PasteDatasetTotal(
                    measured_mass_mg=measured_mass_mg,
                    measured_volume_ul=measured_volume_ul,
                    rotations=sum(rotations.values()),
                ),
                purge=PasteDatasetPurge(
                    pad_id=plan.purge_pad_id,
                    source_pad_id=(
                        f"{plan.purge_pad.designator}.{plan.purge_pad.pad_number}"
                    ),
                    execution=_dataset_execution(purge_execution),
                    measured_volume_ul=allocated[plan.purge_pad_id],
                ),
                pads=pads_metadata,
            )
            session_path = writer.finalize(metadata)
        except Exception:
            incomplete = writer.mark_incomplete()
            ctx.log(f"未完了datasetを保持しました: {incomplete}")
            raise

    archive_name = f"paste-dataset-{session_path.name}.zip"
    archive_path = ctx.artifacts_dir / archive_name
    shutil.make_archive(str(archive_path.with_suffix("")), "zip", root_dir=session_path)
    ctx.log(f"datasetを保存しました: {session_path}")
    return JobResult(
        summary=(
            f"dataset収集完了: {len(plan.sample_pads)} pads / "
            f"{measured_mass_mg:.3f} mg / {measured_volume_ul:.6f} uL"
        ),
        artifacts=(ctx.artifact("ペースト塗布dataset", archive_name, "file"),),
    )
