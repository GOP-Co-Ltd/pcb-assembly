"""ペースト塗布データセット収集ジョブ（銅板のセル格子へ点塗布して撮影）.

セル配置・吐出量スイープ・view 生成・事前検証・crop・metadata は
:mod:`pcbasm.pasting.dataset` に置き、ここは prompt / progress / abort / artifact への
変換だけを担う。
"""

from __future__ import annotations

import random
import shutil
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

import attrs

from pcbasm.geometry import HeightPlane, Point2d
from pcbasm.pasting.dataset.capture import DatasetCapturer
from pcbasm.pasting.dataset.metadata import DatasetView
from pcbasm.pasting.dataset.plan import (
    DEFAULT_PASTE_HEIGHT_MM,
    DEFAULT_VIEW_COUNT,
    DEFAULT_VIEW_OFFSET_MM,
    DotGridPlan,
    DotGridSpec,
    DotTarget,
    plan_dot_grid,
    plan_views,
    validate_capture_reach,
    validate_crop_in_frame,
    validate_dispense_reach,
    validate_min_rotations,
)
from pcbasm.pasting.dataset.recorder import (
    DatasetRunInfo,
    PasteDatasetRecorder,
    validate_dataset_run,
)
from pcbasm.pasting.dataset.writer import PasteDatasetWriter
from pcbasm.pasting.params import PasteParams
from pcbasm.pasting.session import PasteSession
from pcbasm.pcb import Copper, Layer
from pcbasm.vision import CalibrationResult
from pcbasm.vision.crop import RectCrop, crop_pixel_size
from web.api.jobs.board_ops import setup_board
from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.context import (
    JobAborted,
    JobContext,
    JobResult,
    ParamValue,
    PromptSpec,
)
from web.api.jobs.pasting.common import prompt_positive_number

# ParamSpec 既定値の唯一の出典（pcbasm 側）
_DEFAULTS = DotGridSpec()
# shuffle_seed 未指定（0）時に生成する乱数シードの上限
_SEED_MAX = 2**31


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="paste_dataset_collection",
            label="ペースト塗布データセット収集",
            tab="pasting",
            run=_run_paste_dataset_collection,
            params=(
                ParamSpec(
                    "plate_width",
                    "銅板の幅",
                    "float",
                    _DEFAULTS.plate_width_mm,
                    unit="mm",
                ),
                ParamSpec(
                    "plate_height",
                    "銅板の高さ",
                    "float",
                    _DEFAULTS.plate_height_mm,
                    unit="mm",
                ),
                ParamSpec("tolerance", "位置合わせ許容誤差", "float", 0.1, unit="mm"),
                ParamSpec(
                    "edge_margin",
                    "外周余白",
                    "float",
                    _DEFAULTS.edge_margin_mm,
                    unit="mm",
                    minimum=0.0,
                ),
                ParamSpec(
                    "cell_size",
                    "セル寸法",
                    "float",
                    _DEFAULTS.cell_size_mm,
                    unit="mm",
                    help="1 サンプルが占有する領域の一辺です。",
                ),
                ParamSpec(
                    "cell_gap",
                    "セル間隔",
                    "float",
                    _DEFAULTS.cell_gap_mm,
                    unit="mm",
                    minimum=0.0,
                ),
                ParamSpec(
                    "crop_size",
                    "撮影 crop 寸法",
                    "float",
                    _DEFAULTS.crop_size_mm,
                    unit="mm",
                    help=(
                        "保存画像の一辺です。隣セルの写り込みを防ぐため"
                        "セル寸法 + セル間隔以下にします。"
                    ),
                ),
                ParamSpec(
                    "purge_cell_size",
                    "パージ領域寸法",
                    "float",
                    _DEFAULTS.purge_cell_size_mm,
                    unit="mm",
                ),
                ParamSpec(
                    "paste_height",
                    "塗布高さ",
                    "float",
                    DEFAULT_PASTE_HEIGHT_MM,
                    unit="mm",
                    help="点塗布のノズル高さです。膜厚追従（auto）は使いません。",
                    minimum=0.0,
                ),
                ParamSpec(
                    "volume_min",
                    "吐出量の下限",
                    "float",
                    _DEFAULTS.volume_min_ul,
                    unit="uL",
                ),
                ParamSpec(
                    "volume_max",
                    "吐出量の上限",
                    "float",
                    _DEFAULTS.volume_max_ul,
                    unit="uL",
                ),
                ParamSpec(
                    "volume_divisions",
                    "吐出量の分割数",
                    "int",
                    _DEFAULTS.volume_divisions,
                ),
                ParamSpec(
                    "samples_per_volume",
                    "1 量あたりのサンプル数",
                    "int",
                    _DEFAULTS.samples_per_volume,
                ),
                ParamSpec(
                    "blank_count",
                    "blank セル数",
                    "int",
                    _DEFAULTS.blank_count,
                    help="塗布せず撮影だけ行う真値 0 のサンプル数です。",
                    minimum=0,
                ),
                ParamSpec(
                    "view_count",
                    "周辺 view 数",
                    "int",
                    DEFAULT_VIEW_COUNT,
                    help="中心 view に加えて撮影する周辺 view の数です（0 で中心のみ）。",
                    minimum=0,
                ),
                ParamSpec(
                    "view_offset",
                    "view の移動距離",
                    "float",
                    DEFAULT_VIEW_OFFSET_MM,
                    unit="mm",
                    minimum=0.0,
                ),
                ParamSpec(
                    "shuffle_seed",
                    "配置シード",
                    "int",
                    0,
                    help="使用セルの選択と吐出量割り当てのシードです。0 で毎回生成します。",
                    minimum=0,
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
            requires_pcb=False,
            uses_machine=True,
            notify_on_completion=True,
            accepts_commands=True,
            provides_preview=True,
            persisted_params=(
                "plate_width",
                "plate_height",
                "tolerance",
                "edge_margin",
                "cell_size",
                "cell_gap",
                "crop_size",
                "purge_cell_size",
                "paste_height",
                "volume_min",
                "volume_max",
                "volume_divisions",
                "samples_per_volume",
                "blank_count",
                "view_count",
                "view_offset",
                "shuffle_seed",
                "paste_id",
                "paste_lot",
            ),
        )
    )


def resolve_shuffle_seed(value: int) -> int:
    """配置シードを解決する（``0`` は実行ごとに生成、それ以外はそのまま）.

    ``0`` を固定シードとして扱うと全セッションで同じ配置になり、板の特定位置の欠陥と
    特定の吐出量の相関が固定化する。
    """
    return value if value != 0 else random.randrange(1, _SEED_MAX)


def grid_spec_from_params(params: Mapping[str, ParamValue]) -> DotGridSpec:
    """ジョブパラメータからセル格子設定を組む.

    ``shuffle_seed`` はそのまま写す（``0`` の解決は :func:`resolve_shuffle_seed`）。
    レイアウト preview API は同じ写しを使い、``0`` を決定論的な配置として描く。
    """
    return DotGridSpec(
        plate_width_mm=float(params["plate_width"]),
        plate_height_mm=float(params["plate_height"]),
        edge_margin_mm=float(params["edge_margin"]),
        cell_size_mm=float(params["cell_size"]),
        cell_gap_mm=float(params["cell_gap"]),
        crop_size_mm=float(params["crop_size"]),
        purge_cell_size_mm=float(params["purge_cell_size"]),
        volume_min_ul=float(params["volume_min"]),
        volume_max_ul=float(params["volume_max"]),
        volume_divisions=int(params["volume_divisions"]),
        samples_per_volume=int(params["samples_per_volume"]),
        blank_count=int(params["blank_count"]),
        shuffle_seed=int(params["shuffle_seed"]),
    )


def _plan_collection(
    ctx: JobContext,
) -> tuple[DotGridPlan, tuple[DatasetView, ...], int]:
    """装置を開く前にセル配置・view・crop 寸法を確定する（不正は ValueError）.

    crop のピクセル寸法はここで 1 回だけ決め、capturer / writer / metadata の 3 箇所へ
    同じ値を渡す。
    """
    dispenser = ctx.machine.paste_dispenser
    run_error = validate_dataset_run(
        initial_purge_ul=dispenser.initial_purge_ul,
        paste_id=str(ctx.params["paste_id"]).strip(),
        paste_height_mm=float(ctx.params["paste_height"]),
    )
    if run_error is not None:
        raise ValueError(run_error)
    spec = grid_spec_from_params(ctx.params)
    plan, plan_error = plan_dot_grid(
        attrs.evolve(spec, shuffle_seed=resolve_shuffle_seed(spec.shuffle_seed))
    )
    if plan is None:
        raise ValueError(plan_error)
    views, view_error = plan_views(
        int(ctx.params["view_count"]), float(ctx.params["view_offset"])
    )
    if views is None:
        raise ValueError(view_error)
    rotation_error = validate_min_rotations(
        plan.spec, rotations_per_ul=dispenser.rotations_per_ul
    )
    if rotation_error is not None:
        raise ValueError(rotation_error)

    # crop がカメラ視野に収まるかは calibration ファイルだけで判定できるので、
    # 装置を開く前に確定させる（setup_board も同じファイルを読む）。
    calibration = CalibrationResult.load(ctx.machine.camera.calibration_file)
    crop_size_px, crop_error = crop_pixel_size(
        plan.spec.crop_size_mm, calibration.pixel_per_mm
    )
    if crop_size_px is None:
        raise ValueError(crop_error)
    frame_error = validate_crop_in_frame(
        views,
        crop_size_px=crop_size_px,
        pixel_per_mm=calibration.pixel_per_mm,
        resolution=calibration.resolution,
    )
    if frame_error is not None:
        raise ValueError(frame_error)

    targets = len(plan.targets)
    ctx.log(
        f"収集計画: 塗布 {len(plan.cells)} 点 + blank {len(plan.blanks)} 点 / "
        f"撮影 {targets * len(views) * 2} 枚（view {len(views)} / セル "
        f"{plan.capacity} 個中 {targets} 個使用）"
    )
    ctx.log(
        "吐出量 [uL]: "
        + ", ".join(f"{volume:.4f}" for volume in plan.spec.volumes_ul)
        + f" / 配置シード: {plan.spec.shuffle_seed}"
        + f" / crop: {crop_size_px} px"
    )
    return plan, views, crop_size_px


def _confirm_dataset_collection(ctx: JobContext) -> None:
    calibrated = ctx.prompt(
        PromptSpec(
            kind="confirm",
            message=(
                "吐出量キャリブレーションが完了していること、その後に手動プライム・"
                "手動ローディングを行っていないことを確認してください。"
                "収集は先頭でリトラクションを行わないため、手動プライムが残っていると"
                "パージがリトラクション量ぶん過剰に吐出し、回転数比の体積配分を通じて"
                "全sampleへ系統的なバイアスが乗ります。"
            ),
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
                "未塗布の銅板を電子天秤に載せてTAREし、その銅板を装置へ設置してください。"
            ),
            default=True,
            true_label="TARE・設置完了",
            false_label="中止",
        )
    )
    if not tared:
        raise JobAborted()


def _generate_plate(ctx: JobContext, width: float, height: float) -> Path:
    """収集用の外形だけ矩形銅板を artifacts へ生成し、そのパスを返す."""
    # pcbnew 依存はジョブ実行時のみ（KiCAD 未導入でも webui は起動可）
    from pcbasm.pcb.generate import generate_rect_pcb, save_board

    filename = f"paste_dataset_plate_{width:g}x{height:g}.kicad_pcb"
    path = ctx.artifacts_dir / filename
    save_board(generate_rect_pcb(width, height), path)
    ctx.log(f"収集用銅板を生成: {filename}（{width:g}x{height:g} mm）")
    return path


def _measure_plate_height(session: PasteSession) -> HeightPlane:
    """銅板外形を銅箔とみなして高さ面を計測する（生成板は TOP 銅箔を持たない）."""
    return session.measure_height_plane(
        [Copper(layer=Layer.TOP, polygon=session.pcb.outline.polygon)]
    )


def _plate_center_z(
    session: PasteSession, spec: DotGridSpec, height_plane: HeightPlane
) -> float:
    """計測した高さ面の、銅板中心における Z [mm]."""
    center = Point2d(spec.plate_width_mm / 2.0, spec.plate_height_mm / 2.0)
    machine_center = session.board_to_machine.apply(center)
    return height_plane.apply(machine_center.to3d(0.0)).z


def _check_reach(
    session: PasteSession, plan: DotGridPlan, views: tuple[DatasetView, ...]
) -> None:
    """撮影位置と塗布位置がステージ可動域に入るかを検証する.

    塗布は toolhead offset ぶん撮影位置からずれるので、両方を検査する。
    """
    limits = session.stage.limits
    x_limits = (limits.x.min, limits.x.max)
    y_limits = (limits.y.min, limits.y.max)
    for error in (
        validate_capture_reach(
            plan,
            views,
            board_to_stage=session.board_transform,
            x_limits=x_limits,
            y_limits=y_limits,
        ),
        validate_dispense_reach(
            plan,
            board_to_machine=session.board_to_machine,
            x_limits=x_limits,
            y_limits=y_limits,
        ),
    ):
        if error is not None:
            raise ValueError(error)


def _pass_percent(position: int, total: int, start: float, end: float) -> float:
    """1 パス内の進捗を、全体の進捗 ``[start, end]`` へ写す."""
    return start + (end - start) * position / total


def _capture(
    capturer: DatasetCapturer, target: DotTarget, view: DatasetView
) -> RectCrop:
    crop, error = capturer.capture(target, view)
    if crop is None:
        raise ValueError(
            f"sample {target.index} view {view.number} の撮影に失敗: {error}"
        )
    return crop


def _run_paste_dataset_collection(ctx: JobContext) -> JobResult:
    """銅板のセル格子へ点塗布し、塗布前後画像と計量教師値を収集・永続化する.

    撮影は 3 パスにまとめる。全点 pre 撮影 → パージ → 全点塗布 → 全点 post 撮影の順で、
    撮影と塗布の切り替えをまとめて時間を詰める。パージは塗布パスの先頭に置く
    （pre 撮影の前に打つと、撮影のあいだにプライム状態が抜ける）。

    applicator を開くのは塗布パスだけとする。AirPump を入れたまま撮影パスを回すと、
    加圧されたノズルからペーストが垂れて pre 画像と blank セルが汚れる。

    ジョブ先頭で ``applicator.retract()`` はしない。各 ``FillSequence`` が
    ``retract_amount`` を prime してから同量 retract する自己完結型で、dataset 収集は
    手動ローディングを挟まないため、先頭で retract すると plunger が baseline より
    引き込まれた状態で全点が走る。プライム状態のずれは最初のパージが吸収する。

    この前提（手動プライムをしていないこと）は装置の外から観測できないので、開始時の
    confirm プロンプトで運転者に確認させる。
    """
    dispenser_config = ctx.machine.paste_dispenser
    paste_id = str(ctx.params["paste_id"]).strip()
    paste_lot = str(ctx.params.get("paste_lot", "")).strip() or None
    paste_height_mm = float(ctx.params["paste_height"])
    view_count = int(ctx.params["view_count"])
    view_offset_mm = float(ctx.params["view_offset"])
    started_at = datetime.now().astimezone()

    # 設定不正・収まらない配置・撮影窓の破綻はpromptや装置動作より前に検出する。
    plan, views, crop_size_px = _plan_collection(ctx)
    spec = plan.spec
    _confirm_dataset_collection(ctx)

    with ctx.open_camera() as camera:
        plate_path = _generate_plate(ctx, spec.plate_width_mm, spec.plate_height_mm)
        result = setup_board(ctx, camera, pcb_path=plate_path)
        session = PasteSession.from_calibration(result)
        _check_reach(session, plan, views)
        ctx.progress("高さ計測")
        height_plane = _measure_plate_height(session)

        capturer = DatasetCapturer(
            session, crop_size_px=crop_size_px, frame_sink=ctx.frame
        )
        writer = PasteDatasetWriter.open(
            ctx.paste_dataset_dir,
            plate_name=f"plate-{spec.plate_width_mm:g}x{spec.plate_height_mm:g}",
            crop_size_px=crop_size_px,
            started_at=started_at,
        )
        recorder = PasteDatasetRecorder(writer, plan, views)
        # 領域照合の補正を挟まないので、変換は板上のどの点でも同じ 1 本で足りる。
        transform = session.plate_transform(height_plane=height_plane)
        # prime 後の追加遅延は量に依らず一定量を押し出すので、小さい量の点で
        # ラベルと blob の対応が歪む。収集では 0 固定にする。
        params = attrs.evolve(
            PasteParams.from_config(dispenser_config),
            paste_height=paste_height_mm,
            prime_extra_delay=0.0,
        )
        targets = plan.targets

        try:
            # 撮影パスは applicator を開かない。AirPump を入れたまま数十分ヘッドを
            # 動かすと、ノズルからペーストが垂れて pre 画像と blank セルが汚れる。
            for position, target in enumerate(targets):
                ctx.progress(
                    "塗布前撮影", _pass_percent(position, len(targets), 0.0, 45.0)
                )
                ctx.checkpoint()
                for view in views:
                    recorder.record_pre(target, view, _capture(capturer, target, view))

            with session.make_applicator() as applicator:
                ctx.progress("パージ", 45.0)
                ctx.checkpoint()
                recorder.record_purge_execution(
                    applicator.deposit_at(
                        plan.purge_center,
                        amount_ul=dispenser_config.initial_purge_ul,
                        transform=transform,
                        params=params,
                    )
                )

                for position, cell in enumerate(plan.cells):
                    ctx.progress(
                        "塗布", _pass_percent(position, len(plan.cells), 47.0, 55.0)
                    )
                    ctx.checkpoint()
                    recorder.record_execution(
                        cell,
                        applicator.deposit_at(
                            cell.center,
                            amount_ul=cell.commanded_volume_ul,
                            transform=transform,
                            params=params,
                        ),
                    )

            for position, target in enumerate(targets):
                ctx.progress(
                    "塗布後撮影", _pass_percent(position, len(targets), 55.0, 100.0)
                )
                ctx.checkpoint()
                for view in views:
                    post_error = recorder.record_post(
                        target, view, _capture(capturer, target, view)
                    )
                    if post_error is not None:
                        raise ValueError(post_error)

            measured_mass_mg = prompt_positive_number(
                ctx,
                "TAREした電子天秤で塗布済み銅板を計量し、増加質量 [mg] を入力してください。",
            )
            assert measured_mass_mg is not None
            session_path = recorder.finalize(
                measured_mass_mg=measured_mass_mg,
                run=DatasetRunInfo(
                    machine_id=ctx.machine_id,
                    machine_name=ctx.machine.machine_name,
                    paste_id=paste_id,
                    paste_lot=paste_lot,
                    paste_height_mm=paste_height_mm,
                    height_plane_z_mm=_plate_center_z(session, spec, height_plane),
                    view_count=view_count,
                    view_offset_mm=view_offset_mm,
                    crop_size_px=crop_size_px,
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
            f"dataset収集完了: 塗布 {len(plan.cells)} 点 + blank "
            f"{len(plan.blanks)} 点 / {metadata.total.measured_mass_mg:.3f} mg / "
            f"{metadata.total.measured_volume_ul:.6f} uL"
        ),
        artifacts=(ctx.artifact("ペースト塗布dataset", archive_name, "file"),),
    )
