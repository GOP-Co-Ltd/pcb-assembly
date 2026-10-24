"""塗布量校正の生成ジョブ（銅板のセル格子へ点塗布して撮影し、校正まで作る）.

運転者の目的は校正を作ることなので、1 回の実行で収集からフィットまで通す。

3 パス（全点の塗布前撮影 → ローディング → 全点塗布 → 全点の塗布後撮影）で
dataset を作り、計量質量を受け取って確定させ、そのまま直径 → 体積の校正を
フィットして保存する。

収集した dataset は副産物として残り、`paste_volume_refit` でハイパラを変えて
作り直せる（装置も収集も要らない）。

セル配置・吐出量スイープ・view 生成・事前検証・crop・metadata は
:mod:`pcbasm.pasting.paste_volume.dataset`、計測とフィットは
:mod:`pcbasm.pasting.paste_volume` に置き、ここは prompt / progress / abort /
artifact への変換だけを担う。
"""

from __future__ import annotations

import json
import logging
import random
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import partial
from pathlib import Path

import attrs

from pcbasm.pasting.applicator import PasteApplicator
from pcbasm.pasting.dot_capture import DotCaptureRun, DotSpot
from pcbasm.pasting.paste_volume.calibration import calibration_path
from pcbasm.pasting.paste_volume.dataset.metadata import (
    DatasetView,
    PasteDatasetLoading,
)
from pcbasm.pasting.paste_volume.dataset.plan import (
    DEFAULT_PASTE_HEIGHT_MM,
    DEFAULT_VIEW_COUNT,
    DEFAULT_VIEW_OFFSET_MM,
    DotCell,
    DotGridPlan,
    DotGridSpec,
    grid_spec_from_params,
    plan_dot_grid,
    plan_views,
    validate_capture_reach,
    validate_crop_in_frame,
    validate_dispense_reach,
    validate_min_rotations,
)
from pcbasm.pasting.paste_volume.dataset.reader import DatasetSession
from pcbasm.pasting.paste_volume.dataset.recorder import (
    DatasetRunInfo,
    PasteDatasetRecorder,
    validate_dataset_run,
)
from pcbasm.pasting.paste_volume.dataset.store import PasteDatasetStore
from pcbasm.pasting.paste_volume.evaluate import (
    PasteVolumeEvaluation,
    evaluate_collected_session,
    evaluation_document,
    evaluation_lines,
)
from pcbasm.pasting.paste_volume.plot import (
    render_evaluation_scatter,
)
from pcbasm.pasting.plate import CopperPlate, write_rect_board
from pcbasm.pasting.session import PasteSession
from pcbasm.vision import CameraModel
from pcbasm.vision.crop import crop_pixel_size
from web.api.jobs.pasting import PasteStores
from web.api.jobs.runtime.catalog import (
    JobCatalog,
    JobDefinition,
    ParamGroup,
    ParamSpec,
)
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import Artifact, JobResult
from web.api.jobs.steps import (
    DETECTION_PARAM_NAMES,
    DETECTION_PARAMS,
    LOADING_DEFAULT_AMOUNT,
    LOADING_DEFAULT_RETRACT_ROTATIONS,
    LOADING_DEFAULT_ROTATION_ACCEL,
    LOADING_DEFAULT_ROTATION_RATE,
    LOADING_DEFAULT_ROTATIONS,
    REQUIRE_BLANK_ZERO_PARAM,
    LoadingPanel,
    LoadingRotation,
    LoadingTotals,
    build_calibration,
    loading_loop,
    report_calibration,
    setup_paste_session,
    validate_detection_params,
)

# ParamSpec 既定値の唯一の出典（pcbasm 側）
_DEFAULTS = DotGridSpec()
# shuffle_seed 未指定（0）時に生成する乱数シードの上限
_SEED_MAX = 2**31

# ローディング待ちでノズルを銅板から離す距離 [mm]（板の手前側へ、板端から測る）。
# 押し出したペーストが板へ落ちると計量した増加質量に混ざり、回転数比の体積配分を
# 通じて全 sample へ系統的なバイアスが乗る。
LOADING_CLEARANCE_MM = 15.0


# 1 時間かけた実行の主成果物なので「保存しない」は選ばせず、名前の上書きだけを任意にする
# （空ならペースト・ノズル径・塗布高さから自動命名する）
_SAVE_NAME_PARAM = ParamSpec(
    "save_name",
    "校正の保存名（空なら自動）",
    "str",
    default="",
    optional=True,
    help=(
        "空ならペースト・ノズル径・塗布高さと時刻から自動で名前を付けて保存します。"
        "収集した校正は必ず保存されます。"
    ),
)

# フォーム内のまとまり
_PLATE = ParamGroup("銅板")
_GRID = ParamGroup("セル格子")
_SWEEP = ParamGroup("吐出量スイープ")
_LOADING = ParamGroup("ローディング")
_CAPTURE = ParamGroup("塗布と撮影")
_PASTE = ParamGroup("ペースト")
_DETECTION = ParamGroup("円検出のハイパラ", advanced=True)
_OUTPUT = ParamGroup("校正の保存と検証")

logger = logging.getLogger(__name__)


def register(catalog: JobCatalog, stores: PasteStores) -> None:
    catalog.register(
        JobDefinition(
            name="paste_volume_calibration",
            label="塗布量校正の生成",
            run=partial(_run_paste_volume_calibration, stores=stores),
            params=(
                ParamSpec(
                    "plate_width",
                    "銅板の幅",
                    "float",
                    _DEFAULTS.plate_width_mm,
                    unit="mm",
                    group=_PLATE,
                ),
                ParamSpec(
                    "plate_height",
                    "銅板の高さ",
                    "float",
                    _DEFAULTS.plate_height_mm,
                    unit="mm",
                    group=_PLATE,
                ),
                ParamSpec(
                    "tolerance",
                    "位置合わせ許容誤差",
                    "float",
                    0.1,
                    unit="mm",
                    group=_PLATE,
                ),
                ParamSpec(
                    "edge_margin",
                    "外周余白",
                    "float",
                    _DEFAULTS.edge_margin_mm,
                    unit="mm",
                    minimum=0.0,
                    group=_PLATE,
                ),
                ParamSpec(
                    "cell_size",
                    "セル寸法",
                    "float",
                    _DEFAULTS.cell_size_mm,
                    unit="mm",
                    help="1 サンプルが占有する領域の一辺です。",
                    group=_GRID,
                ),
                ParamSpec(
                    "cell_gap",
                    "セル間隔",
                    "float",
                    _DEFAULTS.cell_gap_mm,
                    unit="mm",
                    minimum=0.0,
                    group=_GRID,
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
                    group=_GRID,
                ),
                ParamSpec(
                    "loading_amount",
                    "体積ローディング量",
                    "float",
                    LOADING_DEFAULT_AMOUNT,
                    unit="uL",
                    help=(
                        "塗布パスの先頭で回すローディングの 1 回あたり押出量です。"
                        "押し出したペーストは銅板の外へ廃棄します。"
                    ),
                    group=_LOADING,
                ),
                ParamSpec(
                    "loading_rotations",
                    "回転ローディング回転数",
                    "float",
                    LOADING_DEFAULT_ROTATIONS,
                    unit="rev",
                    group=_LOADING,
                ),
                ParamSpec(
                    "loading_rate",
                    "回転ローディング角速度",
                    "float",
                    LOADING_DEFAULT_ROTATION_RATE,
                    unit="rev/s",
                    group=_LOADING,
                ),
                ParamSpec(
                    "loading_accel",
                    "回転ローディング角加速度",
                    "float",
                    LOADING_DEFAULT_ROTATION_ACCEL,
                    unit="rev/s^2",
                    group=_LOADING,
                ),
                ParamSpec(
                    "loading_retract_rotations",
                    "回転ローディング引き戻し回転数",
                    "float",
                    LOADING_DEFAULT_RETRACT_ROTATIONS,
                    unit="rev",
                    group=_LOADING,
                ),
                ParamSpec(
                    "paste_height",
                    "塗布高さ",
                    "float",
                    DEFAULT_PASTE_HEIGHT_MM,
                    unit="mm",
                    help="点塗布のノズル高さです。膜厚追従（auto）は使いません。",
                    minimum=0.0,
                    group=_CAPTURE,
                ),
                ParamSpec(
                    "volume_min",
                    "吐出量の下限",
                    "float",
                    _DEFAULTS.volume_min_ul,
                    unit="uL",
                    group=_SWEEP,
                ),
                ParamSpec(
                    "volume_max",
                    "吐出量の上限",
                    "float",
                    _DEFAULTS.volume_max_ul,
                    unit="uL",
                    group=_SWEEP,
                ),
                ParamSpec(
                    "volume_divisions",
                    "吐出量の分割数",
                    "int",
                    _DEFAULTS.volume_divisions,
                    group=_SWEEP,
                ),
                ParamSpec(
                    "samples_per_volume",
                    "1 量あたりのサンプル数",
                    "int",
                    _DEFAULTS.samples_per_volume,
                    group=_SWEEP,
                ),
                ParamSpec(
                    "blank_count",
                    "blank セル数",
                    "int",
                    _DEFAULTS.blank_count,
                    help="塗布せず撮影だけ行う真値 0 のサンプル数です。",
                    minimum=0,
                    group=_SWEEP,
                ),
                ParamSpec(
                    "view_count",
                    "周辺 view 数",
                    "int",
                    DEFAULT_VIEW_COUNT,
                    help=(
                        "中心 view に加えて撮影する周辺 view の数です（0 で中心のみ）。"
                        "実測では 0 で十分で、増やすと撮影時間が比例して伸びます。"
                        "検出失敗が出るときだけ増やします。"
                    ),
                    minimum=0,
                    group=_CAPTURE,
                ),
                ParamSpec(
                    "view_offset",
                    "view の移動距離",
                    "float",
                    DEFAULT_VIEW_OFFSET_MM,
                    unit="mm",
                    minimum=0.0,
                    group=_CAPTURE,
                ),
                ParamSpec(
                    "shuffle_seed",
                    "配置シード",
                    "int",
                    0,
                    help="使用セルの選択と吐出量割り当てのシードです。0 で毎回生成します。",
                    minimum=0,
                    group=_SWEEP,
                ),
                ParamSpec(
                    "paste_id",
                    "ペースト製品ID",
                    "str",
                    help="メーカー名・製品名または管理用の品番を入力します。",
                    group=_PASTE,
                ),
                ParamSpec(
                    "paste_lot",
                    "製造ロット（任意）",
                    "str",
                    help="任意。ペースト容器に記載された製造ロット番号を入力します。",
                    optional=True,
                    group=_PASTE,
                ),
                *(
                    attrs.evolve(spec, group=_DETECTION)
                    for spec in (*DETECTION_PARAMS, REQUIRE_BLANK_ZERO_PARAM)
                ),
                attrs.evolve(_SAVE_NAME_PARAM, group=_OUTPUT),
                ParamSpec(
                    "volume_calibration",
                    "検証に使う塗布量校正（任意）",
                    "str",
                    default="",
                    optional=True,
                    help=(
                        "指定すると収集完了後に、この校正での推定体積と実測体積の"
                        "誤差を出します。検証が失敗しても収集結果は失いません。"
                    ),
                    group=_OUTPUT,
                ),
            ),
            requires_pcb=False,
            uses_machine=True,
            notify_on_completion=True,
            accepts_commands=True,
            provides_preview=True,
            loading=LoadingPanel(
                amount="loading_amount",
                rotation=LoadingRotation(
                    rotations="loading_rotations",
                    rate="loading_rate",
                    accel="loading_accel",
                    retract_rotations="loading_retract_rotations",
                ),
            ),
            persisted_params=(
                "plate_width",
                "plate_height",
                "tolerance",
                "edge_margin",
                "cell_size",
                "cell_gap",
                "crop_size",
                "loading_amount",
                "loading_rotations",
                "loading_rate",
                "loading_accel",
                "loading_retract_rotations",
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
                *DETECTION_PARAM_NAMES,
                "require_blank_zero",
                "save_name",
                "volume_calibration",
            ),
        )
    )


def resolve_shuffle_seed(value: int) -> int:
    """配置シードを解決する（``0`` は実行ごとに生成、それ以外はそのまま）.

    ``0`` を固定シードとして扱うと全セッションで同じ配置になり、板の特定位置の欠陥と
    特定の吐出量の相関が固定化する。
    """
    return value if value != 0 else random.randrange(1, _SEED_MAX)


def _plan_collection(
    ctx: JobContext, calibration_dir: Path
) -> tuple[DotGridPlan, tuple[DatasetView, ...], int]:
    """装置を開く前にセル配置・view・crop 寸法を確定する（不正は ValueError）.

    crop のピクセル寸法はここで 1 回だけ決め、capturer / store / metadata の 3 箇所へ同じ値を渡す。
    """
    dispenser = ctx.machine.paste_dispenser
    run_error = validate_dataset_run(
        paste_id=str(ctx.params["paste_id"]).strip(),
        paste_height_mm=float(ctx.params["paste_height"]),
    )
    if run_error is not None:
        raise ValueError(run_error)
    # 1 時間の収集を終えてからハイパラの不正を知らされても遅い
    detection_error = validate_detection_params(ctx.params)
    if detection_error is not None:
        raise ValueError(detection_error)
    self_check_error = _self_verification_error(ctx, calibration_dir)
    if self_check_error is not None:
        raise ValueError(self_check_error)
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
    camera_model = CameraModel.load(ctx.machine.camera.calibration_file)
    crop_size_px, crop_error = crop_pixel_size(
        plan.spec.crop_size_mm, camera_model.pixel_per_mm
    )
    if crop_size_px is None:
        raise ValueError(crop_error)
    frame_error = validate_crop_in_frame(
        views,
        crop_size_px=crop_size_px,
        pixel_per_mm=camera_model.pixel_per_mm,
        resolution=camera_model.image_size,
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
    ctx.require(
        "吐出量キャリブレーションが完了していることを確認してください。"
        "塗布パスの先頭でローディングを行うので、事前の手動プライムは不要です。"
        "ローディングで押し出したペーストは必ず銅板の外へ廃棄してください。"
        "銅板へ落ちると計量した増加質量に混ざり、回転数比の体積配分を通じて"
        "全sampleへ系統的なバイアスが乗ります。",
        true_label="確認済み",
    )
    ctx.require(
        "未塗布の銅板を電子天秤に載せてTAREし、その銅板を装置へ設置してください。",
        true_label="TARE・設置完了",
    )


def _check_reach(
    session: PasteSession, plan: DotGridPlan, views: tuple[DatasetView, ...]
) -> None:
    """撮影位置と塗布位置がステージ可動域に入るかを検証する.

    塗布は toolhead offset ぶん撮影位置からずれるので、両方を検査する。
    """
    limits = session.stage.limits
    for error in (
        validate_capture_reach(
            plan,
            views,
            board_to_stage=session.board_frame.board_transform,
            limits=limits,
        ),
        validate_dispense_reach(
            plan, board_to_machine=session.board_to_machine, limits=limits
        ),
    ):
        if error is not None:
            raise ValueError(error)


def _run_paste_volume_calibration(ctx: JobContext, *, stores: PasteStores) -> JobResult:
    """銅板のセル格子へ点塗布し、塗布前後画像と計量教師値を収集・永続化する.

    撮影は 3 パスにまとめる。全点 pre 撮影 → ローディング → 全点塗布 → 全点 post 撮影の
    順で、撮影と塗布の切り替えをまとめて時間を詰める。ローディングは塗布パスの先頭に置く
    （pre 撮影の前に行うと、数十分の撮影のあいだにプライム状態が抜ける）。押し出した
    ペーストは銅板の外へ廃棄するので、計量した増加質量にも教師体積の配分にも入らない。

    applicator を開くのは塗布パスだけとする。AirPump を入れたまま撮影パスを回すと、
    加圧されたノズルからペーストが垂れて pre 画像と blank セルが汚れる。

    ジョブ先頭で ``applicator.retract()`` はしない。各 ``FillSequence`` が
    ``retract_amount`` を prime してから同量 retract する自己完結型なので、先頭で
    retract すると plunger が baseline より引き込まれた状態で全点が走る。プライム状態は
    塗布直前のローディングで運転者が整える。

    ローディング分を銅板の外へ廃棄したかは装置の外から観測できないので、開始時の
    confirm プロンプトで運転者に確認させる。板へ落ちると計量した増加質量に混ざり、
    回転数比の体積配分を通じて全 sample へ系統的なバイアスが乗る。

    計量質量のプロンプトの前に ``pending.json`` を書く。収集は 1 時間規模で、最後の
    入力だけが装置の外から来るため、そこで WebUI が停止すると撮影済み画像の教師値が残らない。
    残した doc は ``paste_dataset_finalize`` ジョブが計量値と突き合わせて確定する。
    """
    dispenser_config = ctx.machine.paste_dispenser
    paste_id = str(ctx.params["paste_id"]).strip()
    paste_lot = str(ctx.params.get("paste_lot", "")).strip() or None
    paste_height_mm = float(ctx.params["paste_height"])
    view_count = int(ctx.params["view_count"])
    view_offset_mm = float(ctx.params["view_offset"])
    started_at = datetime.now().astimezone()

    # 設定不正・収まらない配置・撮影窓の破綻は prompt や装置動作より前に検出する。
    calibration_dir = stores.paste_volume_calibration_dir
    plan, views, crop_size_px = _plan_collection(ctx, calibration_dir)
    spec = plan.spec
    _confirm_dataset_collection(ctx)

    with ctx.open_camera() as camera:
        width, height = spec.plate_width_mm, spec.plate_height_mm
        filename = f"paste_dataset_plate_{width:g}x{height:g}.kicad_pcb"
        board = write_rect_board(ctx.artifacts_dir / filename, width, height)
        ctx.log(f"収集用銅板を生成: {filename}（{width:g}x{height:g} mm）")
        session = setup_paste_session(ctx, camera, board=board)
        _check_reach(session, plan, views)
        ctx.progress("高さ計測")
        plate = CopperPlate.measure(session)

        store = PasteDatasetStore.open(
            stores.paste_dataset_dir,
            plate_name=f"plate-{width:g}x{height:g}",
            started_at=started_at,
        )
        recorder = PasteDatasetRecorder(store, plan, views)
        capture = DotCaptureRun(
            session,
            ctx,
            crop_size_px=crop_size_px,
            params=attrs.evolve(
                dispenser_config.defaults, paste_height=paste_height_mm
            ),
            views=views,
        )
        loading_totals = LoadingTotals()

        @contextmanager
        def loaded_applicator() -> Iterator[PasteApplicator]:
            """撮影パスが板の上に残したヘッドを逃がし、AirPump を入れてローディングさせる.

            撮影パスは applicator を開かない。

            AirPump を入れたまま撮ると、ノズルから垂れたペーストが pre 画像と blank セルを汚す。
            """
            nonlocal loading_totals
            if (error := plate.move_clear(LOADING_CLEARANCE_MM)) is not None:
                ctx.log(f"{error}。押し出す前に、ノズルを銅板の外へ jog してください")
            with session.make_applicator() as applicator:
                # stage と進捗は loading_loop が設定する。押し出す量は運転者が
                # 決めるので percent は持たせない。
                ctx.log(
                    "ノズルを銅板の外へ逃がしました。ローディングしてください。"
                    "押し出したペーストは銅板へ落とさないでください"
                )
                loading_totals = loading_loop(
                    ctx, session.klipper, session.stage, applicator
                )
                yield applicator

        try:
            error = capture.run(
                [
                    DotSpot(
                        index=target.index,
                        point=target.center,
                        transform=plate.transform,
                        amount_ul=(
                            target.commanded_volume_ul
                            if isinstance(target, DotCell)
                            else None
                        ),
                    )
                    for target in plan.targets
                ],
                recorder,
                before_deposit=loaded_applicator,
            )
            if error is not None:
                raise ValueError(error)

            run = DatasetRunInfo(
                machine_id=ctx.machine_id,
                paste_id=paste_id,
                paste_lot=paste_lot,
                params=capture.params,
                height_plane_z_mm=plate.center_z(),
                view_count=view_count,
                view_offset_mm=view_offset_mm,
                crop_size_px=crop_size_px,
                loading=PasteDatasetLoading(
                    total_ul=loading_totals.amount_ul,
                    total_rotations=loading_totals.rotations,
                ),
                started_at=started_at,
                dispenser=dispenser_config,
                camera_model=session.board_frame.camera_model,
            )
            # 計量質量は装置の外から来る唯一の値なので、それ以外を先に永続化する。
            # プロンプトへ応答できないまま停止しても、撮影済み画像の教師値が残る。
            recorder.write_pending(run)
            ctx.log(
                "計量待ちの状態を保存しました。ここで応答できなくても"
                "「未完了ペーストdatasetの確定」ジョブで復元できます"
            )
            measured_mass_mg = ctx.ask_positive(
                "TAREした電子天秤で塗布済み銅板を計量し、増加質量 [mg] を入力してください。"
            )
            session_path = recorder.finalize(measured_mass_mg=measured_mass_mg, run=run)
        except Exception:
            incomplete = recorder.mark_incomplete()
            ctx.log(f"未完了datasetを保持しました: {incomplete}")
            raise

    metadata = recorder.metadata
    assert metadata is not None
    # 校正生成より前に zip を作る。校正側で何が起きても収集の成果は取り出せる
    archive_name = f"paste-dataset-{session_path.name}.zip"
    archive_path = ctx.artifacts_dir / archive_name
    shutil.make_archive(str(archive_path.with_suffix("")), "zip", root_dir=session_path)
    ctx.log(f"datasetを保存しました: {session_path}")

    calibration_summary, calibration_artifacts = fit_calibration(
        ctx, session_path, calibration_dir
    )
    evaluation_summary, evaluation_artifacts = verify_with_calibration(
        ctx, session_path, calibration_dir
    )
    return JobResult(
        summary=(
            f"校正生成: 塗布 {len(plan.cells)} 点 + blank {len(plan.blanks)} 点 / "
            f"{metadata.total.measured_mass_mg:.3f} mg / "
            f"{metadata.total.measured_volume_ul:.6f} uL"
            + calibration_summary
            + evaluation_summary
        ),
        artifacts=(
            ctx.artifact("ペースト塗布dataset", archive_name, "file"),
            *calibration_artifacts,
            *evaluation_artifacts,
        ),
    )


def _self_verification_error(ctx: JobContext, calibration_dir: Path) -> str | None:
    """今から作る校正で同じ session を検証しようとしていないか確かめる.

    保存名と検証先が同じファイルを指すと、自分で作った校正を自分へ当ててしまう。

    フィットの性質上そこは総体積誤差ほぼ 0 になり、汎化を測ったように見えてしまう。
    """
    verify = str(ctx.params.get("volume_calibration", "")).strip()
    save = str(ctx.params.get("save_name", "")).strip()
    if not verify or not save:
        return None
    if calibration_path(calibration_dir, save).name != verify:
        return None
    return (
        f"検証に使う校正 {verify} を、この収集で上書きしようとしています"
        "（同じsessionで作った校正を同じsessionへ当てると誤差はほぼ0になり、"
        "汎化を測ったことになりません）。保存名か検証先を変えてください"
    )


def fit_calibration(
    ctx: JobContext, session_path: Path, calibration_dir: Path
) -> tuple[str, tuple[Artifact, ...]]:
    """収集した session から校正を作り、まとめの追記と artifact を返す.

    **この関数は例外を投げない。**

    校正づくりがこのジョブの目的だが、この時点で 1 時間の収集と計量が終わっており
    session は永続 dir に残っている。

    ここで FAILED にすると dataset の zip すら作られないので、失敗は警告に留める。

    ハイパラを変えて作り直すのは `paste_volume_refit` の役目で、装置も収集も要らない。
    """
    ctx.progress("塗布量校正の生成", None)
    try:
        session, error = DatasetSession.load(session_path)
        if session is None:
            return _calibration_failed(ctx, error)
        fit, error = build_calibration(ctx, session)
        if fit is None:
            return _calibration_failed(ctx, error)
        summary, artifacts = report_calibration(
            ctx, session, fit, calibration_dir=calibration_dir, always_save=True
        )
    except Exception:
        # 送出しうる例外を 1 つずつ列挙して「投げない」を保証するのは無理なので、まとめて捕捉する
        logger.warning("塗布量校正の生成が例外で終わりました", exc_info=True)
        return _calibration_failed(ctx, "想定外のエラー（詳細はサーバーログ）")
    return f" / {summary}", artifacts


def _calibration_failed(
    ctx: JobContext, error: str | None
) -> tuple[str, tuple[Artifact, ...]]:
    """校正を作れなかったことを警告として報告する（収集結果は残っている）."""
    logger.warning("塗布量校正を作れませんでした: %s", error)
    ctx.log(
        f"塗布量校正を作れませんでした（収集結果は保存済みです）: {error}"
        "。ハイパラを変えて「塗布量校正の再フィット」から作り直せます"
    )
    return " / 校正生成失敗", ()


def verify_with_calibration(
    ctx: JobContext, session_path: Path, calibration_dir: Path
) -> tuple[str, tuple[Artifact, ...]]:
    """収集した session を指定の校正で検証し、まとめの追記と artifact を返す.

    この時点で 1 時間の収集と計量が終わっている。後付けの検証で FAILED にして収集
    結果を失うのは割に合わないので、**この関数は例外を投げない**。

    校正が読めない・session が測れない・図や JSON が書けないといった失敗はすべて
    警告ログに記録し、まとめには誤差か「検証失敗」だけを載せる。

    ``volume_calibration`` が空なら何もせず ``("", ())`` を返す。
    """
    name = str(ctx.params.get("volume_calibration", "")).strip()
    if not name:
        return "", ()

    ctx.progress("塗布量の検証", None)
    evaluation, error = evaluate_collected_session(session_path, calibration_dir / name)
    if evaluation is None:
        logger.warning("塗布量の検証に失敗しました: %s", error)
        ctx.log(f"塗布量の検証に失敗しました（収集結果は保存済みです）: {error}")
        return " / 検証失敗", ()

    for line in evaluation_lines(evaluation):
        ctx.log(line)
    summary = f" / 総体積誤差 {evaluation.total_relative_error * 100:+.2f}%"
    try:
        artifacts = _evaluation_artifacts(ctx, evaluation)
    except Exception:
        # 誤差はログに出ている。図や JSON が書けなくても収集を失敗させない
        logger.warning("塗布量の検証成果物を作れませんでした", exc_info=True)
        ctx.log("塗布量の検証成果物を作れませんでした（誤差はログに出しています）")
        return summary, ()
    return summary, artifacts


def _evaluation_artifacts(
    ctx: JobContext, evaluation: PasteVolumeEvaluation
) -> tuple[Artifact, ...]:
    """検証レポートの JSON と散布図を artifacts_dir へ書く."""
    report = "paste_volume_evaluation.json"
    (ctx.artifacts_dir / report).write_text(
        json.dumps(evaluation_document(evaluation), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    scatter = "paste_volume_evaluation.png"
    render_evaluation_scatter(
        evaluation,
        f"{evaluation.session} / measured vs estimated",
        ctx.artifacts_dir / scatter,
    )
    return (
        ctx.artifact("塗布量の検証", report, "file"),
        ctx.artifact("実測と推定の散布図", scatter, "image"),
    )
