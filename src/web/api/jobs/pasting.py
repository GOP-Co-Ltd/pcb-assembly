"""Pasting タブのジョブ定義（塗布 / 高さ計測 / ローディング / キャリブレーション）."""

from __future__ import annotations

import json
import math
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import attrs
import cv2

from pcbasm import gcode
from pcbasm.config import Machine, resolve_paste_height
from pcbasm.geometry import (
    Compose,
    Identity,
    Point2d,
    Transform,
    sample_points_in_polygons,
    sampling_diagnostics,
)
from pcbasm.hal import (
    Klipper,
    PasteDispenser,
    XYZStage,
)
from pcbasm.pasting import (
    MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT,
    DatasetCapturedView,
    DatasetExecution,
    DatasetPolygon,
    DatasetResolvedPaste,
    DatasetView,
    DispenseRateCalibration,
    FillSpeedSweep,
    FlowCalibrationSet,
    LineLayout,
    LineLayoutOverflowError,
    PadImageCrop,
    PasteApplicationResult,
    PasteApplicator,
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
    PasteSettingsModel,
    ProbeExecutor,
    RateMeasurement,
    ResolvedPaste,
    RotationsPerUlRound,
    ToolheadOffsetResult,
    ToolheadOffsetSample,
    allocate_volume_by_rotations,
    base_override_from_config,
    crop_pad_image,
    dispense_rate_schedule,
    fill_speed_schedule,
    pad_image_crop_to_rgb,
    plan_paste_route,
    plan_toolhead_offset_points,
    rate_sweep_amount,
    resolve_dataset_initial_purge,
    resolve_initial_purge,
    resolve_pad_settings,
    select_enabled_pads,
    slot_area,
    validate_dataset_image_margins,
)
from pcbasm.pcb import (
    Copper,
    Layer,
    Pad,
    PadHierarchy,
    PadRef,
    PcbFile,
    build_pad_hierarchy,
)
from pcbasm.posctrl import (
    BoardAlignment,
    BoardCalibrationResult,
    CircleDetectionError,
    OffsetObserver,
    RegionAlignmentSession,
    XYPositionAdjustor,
    is_pad_refinement_target,
)
from pcbasm.session import PasteSession
from pcbasm.vision import CircleDetector, Image
from pcbasm.visualization import (
    render_height_plane,
    render_planned_points,
)
from web.api.jobs.board_ops import align_regions, setup_board
from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.context import (
    ApplyPayload,
    JobAborted,
    JobContext,
    JobResult,
    PromptSpec,
)
from web.api.jobs.machine_commands import create_command_klipper, handle_machine_command

if TYPE_CHECKING:
    from pcbasm.pasting.paste_volume.calibration import (
        PasteVolumeCalibrationResult,
        PasteVolumeCalibrationSample,
    )
    from pcbasm.pasting.paste_volume.inference import PasteVolumeEstimator

# ローディングフェーズの progress stage 名
# （loading_controls.html の data 属性・テストでピンする契約値）
LOADING_STAGE = "ローディング"
LOADING_DEFAULT_AMOUNT = 0.1
LOADING_DEFAULT_ROTATIONS = 5.0
LOADING_DEFAULT_ROTATION_RATE = 0.5
LOADING_DEFAULT_ROTATION_ACCEL = 0.5
LOADING_DEFAULT_RETRACT_ROTATIONS = 0.0
_LOADING_POSITION_PARAMS = (
    ("position_x", "x"),
    ("position_y", "y"),
    ("position_z", "z"),
)
APPLY_DIGITS = 6
_TOOLHEAD_OFFSET_MIN_FRAME_DETECTIONS = 5
_TOOLHEAD_OFFSET_DETECTION_MAX_ATTEMPTS = 3
_TOOLHEAD_OFFSET_DETECTION_RETRY_DELAY = 0.5

# 吐出量キャリブレーション統合ジョブ（①rotations_per_ul / ②max_dispense_rate /
# ③max_fill_speed をメニュー駆動で順次/個別に回す）の progress stage 名と既定値。
# CALIBRATION_MENU_STAGE は calibration_menu.html の data-calib-stage / calibration_menu.js
# と一致させる契約値。
CALIBRATION_MENU_STAGE = "キャリブレーションメニュー"
DISPENSE_CALIBRATION_DEFAULT_BOARD_WIDTH = 40.0
DISPENSE_CALIBRATION_DEFAULT_BOARD_HEIGHT = 40.0
DISPENSE_CALIBRATION_DEFAULT_LINE_LENGTH = 10.0
DISPENSE_CALIBRATION_DEFAULT_LINE_COUNT = 10
DISPENSE_CALIBRATION_DEFAULT_LINE_AMOUNT = 0.5
DISPENSE_CALIBRATION_DEFAULT_ROW_PITCH = 3.0
DISPENSE_CALIBRATION_DEFAULT_RATE_MIN = 0.5
DISPENSE_CALIBRATION_DEFAULT_RATE_MAX = 5.0
DISPENSE_CALIBRATION_DEFAULT_RATE_DIVISIONS = 6
DISPENSE_CALIBRATION_DEFAULT_SPEED_MIN = 1.0
DISPENSE_CALIBRATION_DEFAULT_SPEED_MAX = 10.0
DISPENSE_CALIBRATION_DEFAULT_SPEED_DIVISIONS = 6
# 計量のため基板を取り出すときの退避 Z オフセット（z_max から引く量）。既定 0 = 全退避。
DISPENSE_CALIBRATION_DEFAULT_REMOVAL_Z_OFFSET = 0.0
# ① 収束判定の相対許容（採用→再計測ループの自動収束ヒント表示用）
DISPENSE_CALIBRATION_CONVERGENCE_REL_TOL = 0.02
# 段ずらしレイアウトの描画領域マージン（銅板端から全周）。折り返し位置と
# 収容可能本数（LineLayout.capacity）の算出に使う。
DISPENSE_CALIBRATION_LAYOUT_MARGIN = 5.0
_DATASET_CAPTURE_SETTLE_TIME = 0.5
_PASTE_VOLUME_CALIBRATION_DEFAULT_PAD_COUNT = 3
_PASTE_VOLUME_CROP_MARGIN_MM = 1.0
_PASTE_VOLUME_MASK_MARGIN_MM = 0.1


@attrs.frozen
class Extrude:
    """ローディング中の押出/吸引 1 回分.

    Attributes:
        amount: 符号付き押出量 [uL]（吸引は負）
    """

    amount: float


@attrs.frozen
class Rotate:
    """ローディング中の raw rotation 押出/吸引 1 回分.

    Attributes:
        rotations: 符号付き回転数 [rev]（吸引は負）
        rate: 角速度 [rev/sec]
        accel: 角加速度 [rev/sec²]
        retract_rotations: 押出直後に逆回転で引き戻す回転数 [rev]（0 で無効）。
            押出（rotations > 0）にのみ付随し、吸引には適用しない。
    """

    rotations: float
    rate: float
    accel: float
    retract_rotations: float = 0.0


@attrs.frozen
class Finish:
    """ローディング終了."""


@attrs.frozen
class InvalidLoadingCommand:
    """ローディング用 type だが値が不正なコマンド（理由をログに出す用）.

    Attributes:
        reason: ユーザー向けの不正理由（ジョブコンソールへそのままログする）
    """

    reason: str


@attrs.frozen
class LoadingTotals:
    """ローディングループ内の押出合計."""

    amount_ul: float = 0.0
    rotations: float = 0.0


type LoadingAction = Extrude | Rotate | Finish


def parse_loading_command(
    command: Mapping[str, Any],
) -> LoadingAction | InvalidLoadingCommand | None:
    """ローディング用 WS command を LoadingAction へ変換する.

    ``{type:"extrude", amount: 正数}`` → ``Extrude(+amount)``、
    ``{type:"suck", amount: 正数}`` → ``Extrude(-amount)``、
    ``{type:"finish"}`` → ``Finish()``。
    ローディング用 type だが値が欠落・非正・非数なら
    ``InvalidLoadingCommand(reason)``、未知 type は None（機械操作の後段判定へ）。
    """
    match command:
        case {"type": "extrude" | "suck" as kind}:
            value = _positive_amount(command.get("amount"))
            if value is None:
                return InvalidLoadingCommand(f"{kind} の量には正の数値が必要です")
            return Extrude(value if kind == "extrude" else -value)
        case {"type": "extrude_rotations" | "suck_rotations" as kind}:
            rotation_value = _positive_amount(command.get("rotations"))
            rate_value = _positive_amount(command.get("rate"))
            accel_value = _positive_amount(command.get("accel"))
            if rotation_value is None or rate_value is None or accel_value is None:
                return InvalidLoadingCommand(
                    f"{kind} の回転数・速度・加速度には正の数値が必要です"
                )
            if kind == "suck_rotations":
                return Rotate(-rotation_value, rate_value, accel_value)
            retract_value = _non_negative_amount(command.get("retract_rotations", 0.0))
            if retract_value is None:
                return InvalidLoadingCommand(
                    "extrude_rotations の引き戻し回転数には 0 以上の数値が必要です"
                )
            return Rotate(
                rotation_value, rate_value, accel_value, retract_rotations=retract_value
            )
        case {"type": "finish"}:
            return Finish()
    return None


def _positive_amount(value: object) -> float | None:
    """正の数値なら float、それ以外は None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _non_negative_amount(value: object) -> float | None:
    """非負（0 含む）の数値なら float、それ以外は None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


def register_pasting_jobs(catalog: JobCatalog) -> None:
    """Pasting タブのジョブを登録する."""
    catalog.register(
        JobDefinition(
            name="paste_solder",
            label="はんだ塗布",
            tab="pasting",
            run=_run_paste_solder,
            params=(
                ParamSpec("tolerance", "位置合わせ許容誤差", "float", 0.1, unit="mm"),
                ParamSpec(
                    "amount",
                    "ローディング既定量",
                    "float",
                    LOADING_DEFAULT_AMOUNT,
                    unit="uL",
                ),
                ParamSpec(
                    "interactive_loading", "対話的ローディング", "bool", default=False
                ),
                ParamSpec(
                    "calibration_pad_count",
                    "画像ベース吐出量補正pad数",
                    "int",
                    _PASTE_VOLUME_CALIBRATION_DEFAULT_PAD_COUNT,
                    minimum=1,
                ),
            ),
            requires_pcb=True,
            uses_machine=True,
            notify_on_completion=True,
            accepts_commands=True,
            provides_preview=True,
            loading_param="amount",
        )
    )
    catalog.register(
        JobDefinition(
            name="height_plane",
            label="高さ平面計測",
            tab="pasting",
            run=_run_height_plane,
            params=(
                ParamSpec("tolerance", "位置合わせ許容誤差", "float", 0.1, unit="mm"),
            ),
            requires_pcb=True,
            uses_machine=True,
            provides_preview=True,
        )
    )
    catalog.register(
        JobDefinition(
            name="loading",
            label="ペーストローディング",
            tab="pasting",
            run=_run_loading,
            params=(
                ParamSpec(
                    "amount",
                    "体積ローディング量",
                    "float",
                    LOADING_DEFAULT_AMOUNT,
                    unit="uL",
                ),
                ParamSpec(
                    "rotations",
                    "回転ローディング回転数",
                    "float",
                    LOADING_DEFAULT_ROTATIONS,
                    unit="rev",
                ),
                ParamSpec(
                    "rate",
                    "回転ローディング角速度",
                    "float",
                    LOADING_DEFAULT_ROTATION_RATE,
                    unit="rev/s",
                ),
                ParamSpec(
                    "accel",
                    "回転ローディング角加速度",
                    "float",
                    LOADING_DEFAULT_ROTATION_ACCEL,
                    unit="rev/s^2",
                ),
                ParamSpec(
                    "retract_rotations",
                    "回転ローディング引き戻し回転数",
                    "float",
                    LOADING_DEFAULT_RETRACT_ROTATIONS,
                    unit="rev",
                ),
                ParamSpec(
                    "position_x",
                    "X",
                    "float",
                    unit="mm",
                    optional=True,
                ),
                ParamSpec(
                    "position_y",
                    "Y",
                    "float",
                    unit="mm",
                    optional=True,
                ),
                ParamSpec(
                    "position_z",
                    "Z",
                    "float",
                    unit="mm",
                    optional=True,
                ),
            ),
            uses_machine=True,
            accepts_commands=True,
            persisted_params=(
                "amount",
                "rotations",
                "rate",
                "accel",
                "retract_rotations",
            ),
            loading_param="amount",
        )
    )
    catalog.register(
        JobDefinition(
            name="dispense_calibration",
            label="吐出量キャリブレーション",
            tab="pasting",
            run=_run_dispense_calibration,
            params=(
                # 共通土台（その場生成する銅板 + ボード計測）。銅板は開始時に 1 回生成する
                # ため board_width / board_height / tolerance はキャリブ後固定（実行中変更不可）。
                ParamSpec(
                    "board_width",
                    "銅板幅",
                    "float",
                    DISPENSE_CALIBRATION_DEFAULT_BOARD_WIDTH,
                    unit="mm",
                ),
                ParamSpec(
                    "board_height",
                    "銅板高さ",
                    "float",
                    DISPENSE_CALIBRATION_DEFAULT_BOARD_HEIGHT,
                    unit="mm",
                ),
                ParamSpec("tolerance", "位置合わせ許容誤差", "float", 0.1, unit="mm"),
                # 線の共通設定（実行中変更可）。line_count / line_amount は ① 専用
                # （②③ は分割数ぶんの線を配置。② はレート × 線長 / 速度で吐出量を
                # 導出、③ は ul_per_mm2 起点）。
                ParamSpec(
                    "line_length",
                    "線の長さ",
                    "float",
                    DISPENSE_CALIBRATION_DEFAULT_LINE_LENGTH,
                    unit="mm",
                    runtime_editable=True,
                ),
                ParamSpec(
                    "line_count",
                    "線の本数",
                    "int",
                    DISPENSE_CALIBRATION_DEFAULT_LINE_COUNT,
                    unit="本",
                    help="① 専用。②③ は分割数ぶんの線を配置",
                    runtime_editable=True,
                ),
                ParamSpec(
                    "line_amount",
                    "1 線あたりの塗布量",
                    "float",
                    DISPENSE_CALIBRATION_DEFAULT_LINE_AMOUNT,
                    unit="uL",
                    help="① 専用。② はレート × 線長 / 速度、③ は面積換算で導出",
                    runtime_editable=True,
                ),
                ParamSpec(
                    "row_pitch",
                    "線の段ずらし間隔",
                    "float",
                    DISPENSE_CALIBRATION_DEFAULT_ROW_PITCH,
                    unit="mm",
                    runtime_editable=True,
                ),
                # 計量退避（実行中変更可）。退避 Z = max(z_min, z_max - offset)。
                # 負 offset は退避 Z がはみ出るため minimum=0.0 で拒否する。
                ParamSpec(
                    "removal_z_offset",
                    "計量退避 Z オフセット",
                    "float",
                    DISPENSE_CALIBRATION_DEFAULT_REMOVAL_Z_OFFSET,
                    unit="mm",
                    runtime_editable=True,
                    minimum=0.0,
                ),
                # 比重は machine.toml の solder_paste_density を参照（フォーム入力なし）
                # ② max_dispense_rate（吐出効率の落ち検出・実行中変更可）
                ParamSpec(
                    "rate_min",
                    "吐出レート最小",
                    "float",
                    DISPENSE_CALIBRATION_DEFAULT_RATE_MIN,
                    unit="uL/s",
                    runtime_editable=True,
                ),
                ParamSpec(
                    "rate_max",
                    "吐出レート最大",
                    "float",
                    DISPENSE_CALIBRATION_DEFAULT_RATE_MAX,
                    unit="uL/s",
                    runtime_editable=True,
                ),
                ParamSpec(
                    "rate_divisions",
                    "吐出レート分割数",
                    "int",
                    DISPENSE_CALIBRATION_DEFAULT_RATE_DIVISIONS,
                    runtime_editable=True,
                ),
                # ③ max_fill_speed（連続最大速度・目視選択・実行中変更可）
                ParamSpec(
                    "speed_min",
                    "塗布速度最小",
                    "float",
                    DISPENSE_CALIBRATION_DEFAULT_SPEED_MIN,
                    unit="mm/s",
                    runtime_editable=True,
                ),
                ParamSpec(
                    "speed_max",
                    "塗布速度最大",
                    "float",
                    DISPENSE_CALIBRATION_DEFAULT_SPEED_MAX,
                    unit="mm/s",
                    runtime_editable=True,
                ),
                ParamSpec(
                    "speed_divisions",
                    "塗布速度分割数",
                    "int",
                    DISPENSE_CALIBRATION_DEFAULT_SPEED_DIVISIONS,
                    runtime_editable=True,
                ),
            ),
            requires_pcb=False,
            uses_machine=True,
            accepts_commands=True,
            persisted_params=(
                "board_width",
                "board_height",
                "tolerance",
                "line_length",
                "line_count",
                "line_amount",
                "row_pitch",
                "removal_z_offset",
                "rate_min",
                "rate_max",
                "rate_divisions",
                "speed_min",
                "speed_max",
                "speed_divisions",
            ),
            loading_param="line_amount",
            # メニュー段階のプライム（押出/吸引）と ① 専用ローディング段階の両方で
            # ボタンを有効化する
            loading_stages="キャリブレーションメニュー,ローディング",
        )
    )
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
    catalog.register(
        JobDefinition(
            name="generate_rect_pcb",
            label="キャリブレーション矩形 PCB 生成",
            tab="pasting",
            run=_run_generate_rect_pcb,
            params=(
                ParamSpec("width", "基板幅", "float", 40.0, unit="mm"),
                ParamSpec("height", "基板高さ", "float", 40.0, unit="mm"),
            ),
            uses_machine=False,
        )
    )
    catalog.register(
        JobDefinition(
            name="toolhead_offset",
            label="ツールヘッドオフセット計測",
            tab="pasting",
            run=_run_toolhead_offset,
            params=(
                ParamSpec("tolerance", "位置合わせ許容誤差", "float", 0.1, unit="mm"),
                ParamSpec(
                    "dispense_amount",
                    "吐出量",
                    "float",
                    LOADING_DEFAULT_AMOUNT,
                    unit="uL",
                ),
                ParamSpec(
                    "loading_amount",
                    "ローディング既定量",
                    "float",
                    LOADING_DEFAULT_AMOUNT,
                    unit="uL",
                ),
                ParamSpec("lift_height", "吐出後の上昇高さ", "float", 5.0, unit="mm"),
                ParamSpec(
                    "paste_diameter_min", "検出円の最小直径", "float", 0.0, unit="mm"
                ),
                ParamSpec(
                    "paste_diameter_max", "検出円の最大直径", "float", 2.0, unit="mm"
                ),
                ParamSpec(
                    "point_count",
                    "計測点数",
                    "int",
                    10,
                    minimum=MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT,
                    help="基板の安全領域を左上から走査して自動配置します",
                ),
                ParamSpec(
                    "point_spacing",
                    "点間隔",
                    "float",
                    5.0,
                    unit="mm",
                    help="自動配置する計測点同士の最小距離です",
                ),
                ParamSpec(
                    "edge_margin",
                    "基板外周margin",
                    "float",
                    5.0,
                    unit="mm",
                    minimum=0,
                    help="ペースト外縁から基板外周・穴まで確保する距離です",
                ),
            ),
            requires_pcb=True,
            uses_machine=True,
            accepts_commands=True,
            persisted_params=(
                "tolerance",
                "dispense_amount",
                "loading_amount",
                "lift_height",
                "paste_diameter_min",
                "paste_diameter_max",
                "point_count",
                "point_spacing",
                "edge_margin",
            ),
            provides_preview=True,
            loading_param="loading_amount",
        )
    )


# --- 共有ヘルパ ---


def _drain_commands(ctx: JobContext) -> int:
    """滞留コマンドを破棄し、破棄した件数を返す.

    段階開始前に押されたボタン/ジョグの遅延実行を防ぐ。
    """
    drained = 0
    while ctx.next_command(timeout=0) is not None:
        drained += 1
    return drained


def _run_loading_loop(
    ctx: JobContext,
    klipper: Klipper,
    stage: XYZStage,
    applicator: PasteApplicator,
    *,
    focus_z: float | None = None,
) -> LoadingTotals:
    """ローディング段階の command 駆動ループを実行し、押出合計を返す.

    extrude / suck は ``applicator.load`` へ、マシン操作コマンドは
    ``handle_machine_command`` へ委譲する。Finish で離脱する。

    Raises:
        JobAborted: 待機中に abort された場合
    """
    ctx.progress(LOADING_STAGE)

    # 滞留コマンドを drain（ローディング段階以前のボタン/ジョグの遅延実行を防ぐ）
    drained = _drain_commands(ctx)
    if drained:
        ctx.log(f"ローディング開始前のコマンド {drained} 件を破棄しました")

    ctx.log("押出 / 吸引ボタンでローディングし、終了ボタンで完了してください")
    total_ul = 0.0
    total_rotations = 0.0
    while True:
        command = ctx.next_command(timeout=None)
        assert command is not None  # timeout=None は取得（or abort）までブロック
        action = parse_loading_command(command)
        match action:
            case Extrude(amount=amount):
                applicator.load(amount)
                total_ul += amount
                ctx.log(
                    f"体積ローディング: {amount:+.3f} uL（累計 {total_ul:+.3f} uL）"
                )
            case Rotate(
                rotations=rotations,
                rate=rate,
                accel=accel,
                retract_rotations=retract_rotations,
            ):
                applicator.load_rotations(rotations, rate, accel)
                total_rotations += rotations
                ctx.log(
                    f"回転ローディング: {rotations:+.3f} rev "
                    f"@ {rate:.3f} rev/s, accel={accel:.3f} rev/s^2"
                    f"（累計 {total_rotations:+.3f} rev）"
                )
                if rotations > 0 and retract_rotations > 0:
                    applicator.load_rotations(-retract_rotations, rate, accel)
                    total_rotations -= retract_rotations
                    ctx.log(
                        f"引き戻し: {-retract_rotations:+.3f} rev "
                        f"@ {rate:.3f} rev/s, accel={accel:.3f} rev/s^2"
                        f"（累計 {total_rotations:+.3f} rev）"
                    )
            case Finish():
                ctx.log(
                    "ローディング終了"
                    f"（体積 {total_ul:+.3f} uL / 回転 {total_rotations:+.3f} rev）"
                )
                return LoadingTotals(amount_ul=total_ul, rotations=total_rotations)
            case InvalidLoadingCommand(reason=reason):
                ctx.log(reason)
            case None:
                if not handle_machine_command(
                    ctx, klipper, stage, command, focus_z=focus_z
                ):
                    ctx.log(f"未知のコマンドです: {command.get('type')!r}")


def _prompt_positive_number(
    ctx: JobContext,
    message: str,
    default: float | None = None,
    cancel_label: str | None = None,
) -> float | None:
    """正数が入力されるまで number プロンプトを繰り返す.

    ``cancel_label`` を渡すと入力欄に中止ボタンを表示し、押されたら ``None`` を返す
    （計測のスキップに使う）。渡さなければ中止ボタンは出ず、常に正数を返す。
    """
    while True:
        answer = ctx.prompt(
            PromptSpec(
                kind="number",
                message=message,
                default=default,
                false_label=cancel_label,
            )
        )
        if answer is False:  # 中止ボタン（cancel_label 指定時のみ届く）
            return None
        assert isinstance(answer, float)
        if answer > 0:
            return answer
        ctx.log(f"正の数値を入力してください（与えられた値: {answer}）")


class _CalibrationCancelled(Exception):
    """サブキャリブの中止要求。メニューループが捕捉してメニューへ戻す.

    ジョブ全体を終了する ``JobAborted`` とは別物（こちらはメニューへ戻るだけ）。
    多段ループ越しに None/False を手で伝播させる代わりに、既知ハンドラ（メニュー
    ループ）への制御フローとして例外を使う。
    """


def _prompt_confirm(
    ctx: JobContext,
    message: str,
    *,
    true_label: str = "続行",
    cancel_label: str = "中止",
    default: bool = True,
) -> None:
    """続行 / 中止の confirm を出す。中止なら ``_CalibrationCancelled`` を送出する."""
    ready = ctx.prompt(
        PromptSpec(
            kind="confirm",
            message=message,
            default=default,
            true_label=true_label,
            false_label=cancel_label,
        )
    )
    if not ready:
        raise _CalibrationCancelled


def _prompt_mass(
    ctx: JobContext, message: str, *, default: float | None = None
) -> float:
    """質量 (mg) を入力させる。中止なら ``_CalibrationCancelled`` を送出する."""
    mass = _prompt_positive_number(ctx, message, default=default, cancel_label="中止")
    if mass is None:
        raise _CalibrationCancelled
    return mass


def _apply_to_machine_toml(ctx: JobContext, values: Mapping[str, float]) -> None:
    """確定したキャリブ値を machine.toml へ即時反映し、内容を log する.

    採用のたびに書き込むことで、以降の中止・失敗でも計測結果を失わない。
    """
    rounded = {key: round(value, APPLY_DIGITS) for key, value in values.items()}
    ctx.apply_machine_settings(rounded)
    pairs = " / ".join(
        f"{key.rsplit('.', 1)[1]} = {value:.6f}" for key, value in rounded.items()
    )
    ctx.log(f"machine.toml へ反映しました: {pairs}")


# --- ジョブ実装 ---


def _resolve_paste_model(
    ctx: JobContext, hierarchy: PadHierarchy
) -> PasteSettingsModel:
    """基板設定ストア（あれば）から塗布設定モデルを取得する.

    ストア／PCB が未配線なら ``machine.toml`` の ``[paste_dispenser]`` を
    L0 デフォルトに据えた全 pad 有効のモデルを返す（= 現行等価のフォールバック）。
    """
    if ctx.board_store is not None and ctx.source_pcb is not None:
        return ctx.board_store.load_or_init(
            ctx.source_pcb,
            ctx.machine.paste_dispenser,
            board_signature=hierarchy.signature(),
        )
    return PasteSettingsModel(
        base=base_override_from_config(ctx.machine.paste_dispenser),
        base_enabled=True,
    )


@attrs.frozen
class _PreparedPasteWorkflow:
    """通常塗布とdataset収集が共有する計測・位置合わせ結果."""

    session: PasteSession
    hierarchy: PadHierarchy
    resolved: Mapping[PadRef, ResolvedPaste]
    routed_pads: tuple[Pad, ...]
    component_positions: Mapping[str, Point2d]
    alignment_session: RegionAlignmentSession
    alignment: BoardAlignment
    height_plane: Transform
    aligned_count: int
    region_count: int
    refinement_success_count: int
    refinement_target_count: int

    def pad_transform(self, pad: Pad) -> Transform:
        """計測済み補正からpad用の塗布座標変換を返す."""
        return self.session.pad_to_machine(
            pad,
            alignment=self.alignment,
            height_plane=self.height_plane,
        )


def _resolved_paste_for_pad(
    hierarchy: PadHierarchy,
    resolved: Mapping[PadRef, ResolvedPaste],
    pad: Pad,
) -> ResolvedPaste | None:
    """Component無しの後方互換padではNone、それ以外は解決済み設定を返す."""
    try:
        pad_ref = hierarchy.pad_ref_for_pad(pad)
    except KeyError:
        return None
    return resolved.get(pad_ref)


def _prepare_paste_workflow(
    ctx: JobContext,
    result: BoardCalibrationResult,
    *,
    routed_pads: Sequence[Pad],
    alignment_pads: Sequence[Pad],
    hierarchy: PadHierarchy,
    resolved: Mapping[PadRef, ResolvedPaste],
) -> _PreparedPasteWorkflow:
    """高さ計測、領域/pad照合、pad別変換の共通前処理を実行する."""
    session = PasteSession.from_calibration(result)
    top_coppers = [copper for copper in session.pcb.copper if copper.layer == Layer.TOP]
    component_positions = {
        component.designator: component.position for component in session.pcb.components
    }

    ctx.progress("高さ計測")
    height_plane = session.height_measurer.measure(
        coppers=top_coppers,
        board_to_machine=session.board_to_machine,
        outline=session.pcb.outline.polygon,
    )

    alignment_session = RegionAlignmentSession(result, frame_sink=ctx.frame)
    regions = alignment_session.plan_regions([pad.center for pad in alignment_pads])
    ctx.log(f"照合対象の領域数: {len(regions)}")
    aligned = align_regions(ctx, alignment_session, regions)
    alignment = BoardAlignment(results=tuple(aligned))
    for pad in alignment_pads:
        alignment.correction_for(pad.center, designator=pad.designator)
    ctx.log(f"位置合わせ成功: {len(aligned)}/{len(regions)} 領域")

    max_short_side = result.machine.paste_dispenser.pad_align.refine_max_short_side
    refinement_targets = [
        pad
        for pad in alignment_pads
        if is_pad_refinement_target(pad, max_short_side_mm=max_short_side)
    ]
    ctx.log(
        f"pad中心照合対象: {len(refinement_targets)}/{len(alignment_pads)} pads "
        f"(最大短辺 {max_short_side:g} mm)"
    )
    pad_alignments = []
    for index, pad in enumerate(refinement_targets):
        ctx.progress("pad照合", 100.0 * index / len(refinement_targets))
        ctx.checkpoint()
        initial_correction = alignment.correction_for(
            pad.center, designator=pad.designator
        )
        refined = alignment_session.refine(pad.center, initial_correction, pad.polygon)
        if refined is None:
            ctx.log(
                f"警告: {pad.designator}.{pad.pad_number} のpad中心照合が"
                "収束しないため領域補正を使用"
            )
            continue
        base = result.board_transform.apply(pad.center)
        initial_displacement = initial_correction.apply(base) - base
        residual = refined.displacement - initial_displacement
        ctx.log(
            f"{pad.designator}.{pad.pad_number}: "
            f"residual=({residual.x:+.4f}, {residual.y:+.4f}) mm, "
            f"passes={refined.passes}"
        )
        pad_alignments.append(refined)
    alignment = BoardAlignment(
        results=tuple(pad_alignments), fallback_results=tuple(aligned)
    )
    ctx.log(f"pad中心照合成功: {len(pad_alignments)}/{len(refinement_targets)} pads")
    for pad in alignment_pads:
        alignment.correction_for(pad.center, designator=pad.designator)

    return _PreparedPasteWorkflow(
        session=session,
        hierarchy=hierarchy,
        resolved=resolved,
        routed_pads=tuple(routed_pads),
        component_positions=component_positions,
        alignment_session=alignment_session,
        alignment=alignment,
        height_plane=height_plane,
        aligned_count=len(aligned),
        region_count=len(regions),
        refinement_success_count=len(pad_alignments),
        refinement_target_count=len(refinement_targets),
    )


type _PastePair = tuple[Pad, Transform, ResolvedPaste | None]


def _same_pad(left: Pad, right: Pad) -> bool:
    """同じ設計子・pad番号を持つpadかを返す."""
    return left.designator == right.designator and left.pad_number == right.pad_number


def _paste_volume_execution_split(
    pairs: Sequence[_PastePair],
    purge_pad: Pad | None,
    sample_count: int,
) -> tuple[tuple[_PastePair, ...], tuple[_PastePair, ...], tuple[_PastePair, ...]]:
    """route順を保ちつつ、補正確定前後と撮影対象へ分ける."""
    selected_indices = [
        index
        for index, (pad, _transform, _resolved) in enumerate(pairs)
        if purge_pad is None or not _same_pad(pad, purge_pad)
    ][:sample_count]
    if not selected_indices:
        return (), tuple(pairs), ()
    cutoff = selected_indices[-1] + 1
    selected = tuple(pairs[index] for index in selected_indices)
    return tuple(pairs[:cutoff]), tuple(pairs[cutoff:]), selected


def _apply_paste_pair(
    applicator: PasteApplicator,
    prepared: _PreparedPasteWorkflow,
    pair: _PastePair,
) -> PasteApplicationResult:
    """1 padへ解決済み設定を適用し、指令結果を返す."""
    pad, transform, resolved = pair
    line_reference = prepared.component_positions.get(pad.designator)
    if resolved is None:
        return applicator.apply(
            [pad.polygon],
            transform=transform,
            line_reference=line_reference,
        )
    return applicator.apply(
        [pad.polygon],
        transform=transform,
        line_reference=line_reference,
        paste_height=resolved.paste_height,
        ul_per_mm2=resolved.ul_per_mm2,
        dispense_mode=resolved.dispense_mode,
        line_direction=resolved.line_direction,
        prime_extra_delay=resolved.prime_extra_delay,
        bead_width_factor=resolved.bead_width_factor,
        overlap=resolved.overlap,
        boundary_margin=resolved.boundary_margin,
    )


def _load_paste_volume_estimator(ctx: JobContext) -> PasteVolumeEstimator | None:
    """Machine設定のpackageまたはactive pointerをjob開始時にloadする."""
    try:
        setting = ctx.machine.paste_volume
        if setting is None:
            ctx.log("画像ベース吐出量補正: model未設定のため無効")
            return None
        from pcbasm.pasting.paste_volume.inference import (
            load_configured_paste_volume_estimator,
        )

        estimator = load_configured_paste_volume_estimator(setting.model_package)
    except Exception as exc:
        ctx.log(f"画像ベース吐出量補正modelのloadに失敗しました: {exc}")
        proceed = ctx.prompt(
            PromptSpec(
                kind="confirm",
                message=(
                    "画像ベース吐出量補正を無効にし、machine.tomlの"
                    "rotations_per_ulで塗布を続行しますか？"
                ),
                default=False,
                true_label="補正なしで続行",
                false_label="中止",
            )
        )
        if not proceed:
            raise JobAborted() from exc
        return None

    info = estimator.model_info
    ctx.log(
        "画像ベース吐出量補正modelをloadしました: "
        f"id={info.model_id} / name={info.model_name} / "
        f"version={info.model_version} / checksum={info.model_checksum}"
    )
    return estimator


def _capture_paste_volume_pre_images(
    ctx: JobContext,
    result: BoardCalibrationResult,
    prepared: _PreparedPasteWorkflow,
    selected: Sequence[_PastePair],
) -> dict[tuple[str, str], PadImageCrop]:
    """補正対象全padを塗布前に撮影する."""
    captures: dict[tuple[str, str], PadImageCrop] = {}
    view = DatasetView(number=0)
    for index, (pad, _transform, _resolved) in enumerate(selected):
        ctx.progress("吐出量補正・塗布前撮影", 100.0 * index / max(len(selected), 1))
        ctx.checkpoint()
        captures[(pad.designator, pad.pad_number)] = _capture_dataset_pad(
            ctx,
            result,
            prepared,
            pad,
            view,
            margin_mm=_PASTE_VOLUME_CROP_MARGIN_MM,
            mask_margin_mm=_PASTE_VOLUME_MASK_MARGIN_MM,
        )
    return captures


def _log_paste_volume_calibration(
    ctx: JobContext,
    result: PasteVolumeCalibrationResult,
) -> None:
    """Coreの一括補正結果をjob logへ記録する."""
    gain = "-" if result.gain is None else f"{result.gain:.6f}"
    ctx.log(
        "画像ベース吐出量補正: "
        f"old={result.old_rotations_per_ul:.6f} rev/uL / "
        f"new={result.new_rotations_per_ul:.6f} rev/uL / "
        f"gain={gain} / used={result.used_count} / "
        f"rejected={result.rejected_count} / clamp={result.clamped} / "
        f"model={result.model_id or '-'} / reason={result.rejection_reason or '-'}"
    )


def _rejected_paste_volume_sample(
    *,
    commanded_volume_ul: float,
    model_id: str,
    reason: str,
) -> PasteVolumeCalibrationSample:
    """Pad単位の撮影・推論例外を棄却sampleへ変換する."""
    from pcbasm.pasting.paste_volume.calibration import PasteVolumeCalibrationSample
    from pcbasm.pasting.paste_volume.inference import PasteVolumePrediction

    return PasteVolumeCalibrationSample(
        commanded_volume_ul=commanded_volume_ul,
        prediction=PasteVolumePrediction(
            mean_volume_ul=0.0,
            std_volume_ul=0.0,
            relative_std=0.0,
            accepted=False,
            rejection_reason=reason,
            model_id=model_id,
        ),
    )


def _paste_volume_summary(
    estimator: PasteVolumeEstimator | None,
    result: PasteVolumeCalibrationResult | None,
    *,
    old_rotations_per_ul: float,
) -> str:
    """Job結果へ載せるmodel lineageと補正結果を組み立てる."""
    if estimator is None:
        model_id = model_name = model_version = model_checksum = "-"
    else:
        info = estimator.model_info
        model_id = info.model_id
        model_name = info.model_name
        model_version = info.model_version
        model_checksum = info.model_checksum
    new_value = old_rotations_per_ul if result is None else result.new_rotations_per_ul
    applied = result is not None and result.applied
    if result is not None:
        rejection_reason = result.rejection_reason or "-"
    elif estimator is None:
        rejection_reason = "modelが未設定またはloadされていません"
    else:
        rejection_reason = "補正対象padがありません"
    return (
        "画像補正 "
        f"old={old_rotations_per_ul:.6f} / new={new_value:.6f} rev/uL・"
        f"status={'applied' if applied else 'skip'}・"
        f"model={model_id} ({model_name})・version={model_version}・"
        f"checksum={model_checksum}・"
        f"used={0 if result is None else result.used_count}・"
        f"rejected={0 if result is None else result.rejected_count}・"
        f"clamp={False if result is None else result.clamped}・"
        f"rejection_reason={rejection_reason}"
    )


def _run_paste_solder(ctx: JobContext) -> JobResult:
    """ボード計測 → 高さ計測 → 銅箔照合 → 補正適用 → ペースト塗布を通しで実行する.

    塗布対象は基板ごとの pad 有効/無効 + 階層 override 設定で絞り込み、各 pad に
    解決済みの塗布設定を適用する。設定ファイル不在時は ``machine.toml`` デフォルトで
    全 pad 有効 = 現行等価で動く。
    """
    estimator = _load_paste_volume_estimator(ctx)
    calibration_result: PasteVolumeCalibrationResult | None = None
    old_rotations_per_ul = ctx.machine.paste_dispenser.rotations_per_ul
    with ctx.open_camera() as camera:
        result = setup_board(ctx, camera)
        top_pads = [pad for pad in result.pcb.pads if pad.layer == Layer.TOP]

        # pad 階層 + 基板ごとの塗布設定（装置不要・前段で解決）
        hierarchy = build_pad_hierarchy(result.pcb.components, result.pcb.pads)
        model = _resolve_paste_model(ctx, hierarchy)
        resolved = resolve_pad_settings(hierarchy, model)

        # 有効 top pad のみ塗布対象にする（無効除外はここ一点）
        enabled_pads = select_enabled_pads(top_pads, hierarchy, model)
        disabled_count = len(top_pads) - len(enabled_pads)
        ctx.log(
            f"塗布対象: 有効 {len(enabled_pads)} / 全 {len(top_pads)} pads"
            f"（無効 {disabled_count} 件スキップ）"
        )

        # 塗布順路（同種類連続・大面積優先）を先に決める。
        # 初回パージ pad 未指定時は、この route の先頭 pad を使う。
        routed_pads = [stop.pad for stop in plan_paste_route(enabled_pads)]
        initial_purge, initial_purge_error = resolve_initial_purge(
            amount_ul=result.machine.paste_dispenser.initial_purge_ul,
            pad_id=model.initial_purge_pad_id,
            hierarchy=hierarchy,
            routed_pads=routed_pads,
            layer=Layer.TOP,
        )
        if initial_purge_error is not None:
            raise ValueError(initial_purge_error)
        if initial_purge is not None:
            ctx.log(
                f"初回パージ: {initial_purge.pad_id} に "
                f"{initial_purge.amount_ul:.3f} uL"
            )

        alignment_pads = list(routed_pads)
        if initial_purge is not None and not any(
            pad.designator == initial_purge.pad.designator
            and pad.pad_number == initial_purge.pad.pad_number
            for pad in alignment_pads
        ):
            alignment_pads.append(initial_purge.pad)
        prepared = _prepare_paste_workflow(
            ctx,
            result,
            routed_pads=routed_pads,
            alignment_pads=alignment_pads,
            hierarchy=hierarchy,
            resolved=resolved,
        )
        pairs = [
            (
                pad,
                prepared.pad_transform(pad),
                _resolved_paste_for_pad(hierarchy, resolved, pad),
            )
            for pad in routed_pads
        ]
        purge_transform = (
            prepared.pad_transform(initial_purge.pad)
            if initial_purge is not None
            else None
        )
        session = prepared.session
        stage = prepared.session.stage

        calibration_prefix: tuple[_PastePair, ...] = ()
        calibration_suffix: tuple[_PastePair, ...] = tuple(pairs)
        selected_calibration_pairs: tuple[_PastePair, ...] = ()
        pre_images: dict[tuple[str, str], PadImageCrop] = {}
        if estimator is not None:
            (
                calibration_prefix,
                calibration_suffix,
                selected_calibration_pairs,
            ) = _paste_volume_execution_split(
                pairs,
                None if initial_purge is None else initial_purge.pad,
                int(ctx.params["calibration_pad_count"]),
            )
            try:
                pre_images = _capture_paste_volume_pre_images(
                    ctx,
                    result,
                    prepared,
                    selected_calibration_pairs,
                )
            except ValueError as exc:
                from pcbasm.pasting.paste_volume.calibration import (
                    failed_paste_volume_calibration,
                )

                ctx.log(f"画像ベース吐出量補正を無効にしました: {exc}")
                calibration_result = failed_paste_volume_calibration(
                    old_rotations_per_ul,
                    len(selected_calibration_pairs),
                    str(exc),
                    model_id=estimator.model_info.model_id,
                )
                _log_paste_volume_calibration(ctx, calibration_result)
                calibration_prefix = ()
                calibration_suffix = tuple(pairs)
                selected_calibration_pairs = ()

        total = LoadingTotals()
        with session.make_applicator() as applicator:
            if ctx.params["interactive_loading"]:
                pos = stage.get_position()
                session.klipper.send_gcode(stage.move(x=0, y=0, z=0))
                total = _run_loading_loop(ctx, session.klipper, stage, applicator)
                session.klipper.send_gcode(
                    stage.move(x=pos.x, y=pos.y, z=pos.z) + gcode.wait_for_done()
                )

            ctx.progress("リトラクション")
            applicator.retract()

            if initial_purge is not None and purge_transform is not None:
                ctx.progress("初回パージ")
                ctx.checkpoint()
                applicator.deposit_at(
                    initial_purge.pad.center,
                    amount=initial_purge.amount_ul,
                    transform=purge_transform,
                )

            calibration_samples: list[PasteVolumeCalibrationSample] = []
            selected_keys = {
                (pad.designator, pad.pad_number)
                for pad, _transform, _resolved in selected_calibration_pairs
            }
            for index, pair in enumerate(calibration_prefix):
                ctx.progress("塗布", 100.0 * index / len(pairs))
                ctx.checkpoint()
                execution = _apply_paste_pair(applicator, prepared, pair)
                pad = pair[0]
                key = (pad.designator, pad.pad_number)
                if key not in selected_keys:
                    continue
                try:
                    post = _capture_dataset_pad(
                        ctx,
                        result,
                        prepared,
                        pad,
                        DatasetView(number=0),
                        margin_mm=_PASTE_VOLUME_CROP_MARGIN_MM,
                        mask_margin_mm=_PASTE_VOLUME_MASK_MARGIN_MM,
                    )
                    pre = pre_images[key]
                    if post.pixel_rect != pre.pixel_rect:
                        raise ValueError(
                            f"{pad.designator}.{pad.pad_number} のpre/post crop位置が"
                            "一致しません"
                        )
                except ValueError as exc:
                    ctx.log(
                        f"{pad.designator}.{pad.pad_number}: "
                        f"吐出量推定を棄却しました: {exc}"
                    )
                    assert estimator is not None
                    calibration_samples.append(
                        _rejected_paste_volume_sample(
                            commanded_volume_ul=execution.commanded_volume_ul,
                            model_id=estimator.model_info.model_id,
                            reason=str(exc),
                        )
                    )
                    continue
                assert estimator is not None
                try:
                    prediction = estimator.predict(
                        pad_image_crop_to_rgb(pre),
                        pad_image_crop_to_rgb(post),
                        pixel_per_mm=result.calibration.pixel_per_mm,
                    )
                except Exception as exc:
                    ctx.log(
                        f"{pad.designator}.{pad.pad_number}: "
                        f"吐出量推定に失敗しました: {exc}"
                    )
                    calibration_samples.append(
                        _rejected_paste_volume_sample(
                            commanded_volume_ul=execution.commanded_volume_ul,
                            model_id=estimator.model_info.model_id,
                            reason=str(exc),
                        )
                    )
                    continue
                ctx.log(
                    f"{pad.designator}.{pad.pad_number}: "
                    f"mean={prediction.mean_volume_ul:.6f} uL / "
                    f"std={prediction.std_volume_ul:.6f} uL / "
                    f"accepted={prediction.accepted} / "
                    f"reason={prediction.rejection_reason or '-'}"
                )
                from pcbasm.pasting.paste_volume.calibration import (
                    PasteVolumeCalibrationSample,
                )

                calibration_samples.append(
                    PasteVolumeCalibrationSample(
                        commanded_volume_ul=execution.commanded_volume_ul,
                        prediction=prediction,
                    )
                )

            if selected_calibration_pairs and estimator is not None:
                from pcbasm.pasting.paste_volume.calibration import (
                    calibrate_rotations_per_ul,
                )

                calibration_result = calibrate_rotations_per_ul(
                    old_rotations_per_ul,
                    calibration_samples,
                )
                _log_paste_volume_calibration(ctx, calibration_result)

            if calibration_result is None or not calibration_result.applied:
                for index, pair in enumerate(
                    calibration_suffix, start=len(calibration_prefix)
                ):
                    ctx.progress("塗布", 100.0 * index / len(pairs))
                    ctx.checkpoint()
                    _apply_paste_pair(applicator, prepared, pair)

        if calibration_result is not None and calibration_result.applied:
            with session.make_applicator(
                rotations_per_ul=calibration_result.new_rotations_per_ul
            ) as calibrated_applicator:
                for index, pair in enumerate(
                    calibration_suffix, start=len(calibration_prefix)
                ):
                    ctx.progress("塗布", 100.0 * index / len(pairs))
                    ctx.checkpoint()
                    _apply_paste_pair(calibrated_applicator, prepared, pair)

    return JobResult(
        summary=(
            f"照合成功 {prepared.aligned_count}/{prepared.region_count} 領域 / "
            f"pad中心照合 {prepared.refinement_success_count}/"
            f"{prepared.refinement_target_count} pads / "
            f"塗布 有効 {len(pairs)} / 全 {len(top_pads)} pads"
            f"（無効 {disabled_count} 件スキップ・"
            f"初回パージ {initial_purge.amount_ul if initial_purge else 0.0:.3f} uL・"
            f"押出合計 {total.amount_ul:+.3f} uL） / "
            + _paste_volume_summary(
                estimator,
                calibration_result,
                old_rotations_per_ul=old_rotations_per_ul,
            )
        )
    )


@attrs.frozen
class _DatasetPadPlan:
    """装置を動かす前に検証したpurgeと収集padの計画."""

    hierarchy: PadHierarchy
    resolved: Mapping[PadRef, ResolvedPaste]
    purge_pad: Pad
    purge_pad_id: str
    sample_pads: tuple[Pad, ...]


def _pad_id_or_none(hierarchy: PadHierarchy, pad: Pad) -> str | None:
    """Component無しpadではNone、それ以外は一意pad IDを返す."""
    try:
        return hierarchy.pad_id_for_pad(pad)
    except KeyError:
        return None


def _dataset_pad_plan(ctx: JobContext, pcb: PcbFile) -> _DatasetPadPlan:
    """任意PCBからpurgeを除く有効top pad routeを装置非依存で解決する."""
    top_pads = [pad for pad in pcb.pads if pad.layer == Layer.TOP]
    hierarchy = build_pad_hierarchy(pcb.components, pcb.pads)
    model = _resolve_paste_model(ctx, hierarchy)
    resolved = resolve_pad_settings(hierarchy, model)
    purge, purge_error = resolve_dataset_initial_purge(
        amount_ul=ctx.machine.paste_dispenser.initial_purge_ul,
        pad_id=model.initial_purge_pad_id,
        hierarchy=hierarchy,
    )
    if purge_error is not None:
        raise ValueError(purge_error)
    if purge is None:
        raise ValueError("dataset収集には初回パージパッドが必要です")
    purge_pad_id = purge.pad_id
    purge_pad = purge.pad
    enabled = select_enabled_pads(top_pads, hierarchy, model)
    sample_pads = tuple(
        stop.pad
        for stop in plan_paste_route(pad for pad in enabled if pad is not purge_pad)
    )
    if not sample_pads:
        raise ValueError("purge以外の収集対象padがありません")
    orphan_ids = [
        f"{pad.designator}.{pad.pad_number}"
        for pad in sample_pads
        if _pad_id_or_none(hierarchy, pad) is None
    ]
    if orphan_ids:
        raise ValueError(
            "dataset収集対象padに対応するComponentがありません: "
            + ", ".join(orphan_ids)
        )
    return _DatasetPadPlan(
        hierarchy=hierarchy,
        resolved=resolved,
        purge_pad=purge_pad,
        purge_pad_id=purge_pad_id,
        sample_pads=sample_pads,
    )


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
    prepared: _PreparedPasteWorkflow,
    pad: Pad,
    view: DatasetView,
    *,
    margin_mm: float,
    mask_margin_mm: float,
) -> PadImageCrop:
    """補正済みpad位置へcameraを動かしてcrop/maskを1組取得する."""
    correction = prepared.alignment.correction_for(
        pad.center, designator=pad.designator
    )
    base_target = correction.apply(result.board_transform.apply(pad.center))
    target = Point2d(
        base_target.x + view.offset_x_mm,
        base_target.y + view.offset_y_mm,
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
    return DatasetExecution(
        applied_mode=result.applied_mode,
        path_length_mm=result.path_length_mm,
        commanded_volume_ul=result.commanded_volume_ul,
        prime_extra_volume_ul=result.prime_extra_volume_ul,
        effective_rate_ul_s=result.effective_rate_ul_s,
        rotations=result.rotations,
    )


def _dataset_resolved(paste: ResolvedPaste) -> DatasetResolvedPaste:
    return DatasetResolvedPaste(
        dispense_mode=paste.dispense_mode,
        line_direction=paste.line_direction,
        paste_height=paste.paste_height,
        ul_per_mm2=paste.ul_per_mm2,
        prime_extra_delay=paste.prime_extra_delay,
        bead_width_factor=paste.bead_width_factor,
        overlap=paste.overlap,
        boundary_margin=paste.boundary_margin,
    )


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
    _dataset_pad_plan(ctx, PcbFile(ctx.pcb_path))
    _confirm_dataset_collection(ctx)

    started_at = datetime.now().astimezone()
    writer: PasteDatasetWriter | None = None
    with ctx.open_camera() as camera:
        result = setup_board(ctx, camera)
        plan = _dataset_pad_plan(ctx, result.pcb)
        prepared = _prepare_paste_workflow(
            ctx,
            result,
            routed_pads=plan.sample_pads,
            alignment_pads=(*plan.sample_pads, plan.purge_pad),
            hierarchy=plan.hierarchy,
            resolved=plan.resolved,
        )
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

            with prepared.session.make_applicator() as applicator:
                ctx.progress("リトラクション")
                applicator.retract()
                ctx.progress("パージ")
                ctx.checkpoint()
                purge_result = applicator.deposit_at(
                    plan.purge_pad.center,
                    amount=dispenser_config.initial_purge_ul,
                    transform=prepared.pad_transform(plan.purge_pad),
                )
                executions[plan.purge_pad_id] = purge_result

                for index, pad in enumerate(plan.sample_pads):
                    ctx.progress("塗布", 100.0 * index / len(plan.sample_pads))
                    ctx.checkpoint()
                    pad_ref = plan.hierarchy.pad_ref_for_pad(pad)
                    pad_id = plan.hierarchy.pad_id_for_pad(pad)
                    resolved = plan.resolved[pad_ref]
                    executions[pad_id] = applicator.apply(
                        [pad.polygon],
                        transform=prepared.pad_transform(pad),
                        line_reference=prepared.component_positions.get(pad.designator),
                        paste_height=resolved.paste_height,
                        ul_per_mm2=resolved.ul_per_mm2,
                        dispense_mode=resolved.dispense_mode,
                        line_direction=resolved.line_direction,
                        prime_extra_delay=resolved.prime_extra_delay,
                        bead_width_factor=resolved.bead_width_factor,
                        overlap=resolved.overlap,
                        boundary_margin=resolved.boundary_margin,
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

            measured_mass_mg = _prompt_positive_number(
                ctx,
                "TAREした電子天秤で塗布済み基板を計量し、増加質量 [mg] を入力してください。",
            )
            assert measured_mass_mg is not None
            measured_volume_ul = (
                measured_mass_mg / dispenser_config.solder_paste_density
            )
            rotations = {
                key: execution.rotations for key, execution in executions.items()
            }
            allocated = allocate_volume_by_rotations(measured_volume_ul, rotations)
            purge_execution = executions[plan.purge_pad_id]
            pads_metadata = tuple(
                PasteDatasetPad(
                    index=index,
                    pad_id=(pad_id := plan.hierarchy.pad_id_for_pad(pad)),
                    source_pad_id=f"{pad.designator}.{pad.pad_number}",
                    polygon=DatasetPolygon.from_polygon(pad.polygon),
                    resolved=_dataset_resolved(
                        plan.resolved[plan.hierarchy.pad_ref_for_pad(pad)]
                    ),
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


def _run_height_plane(ctx: JobContext) -> JobResult:
    """計画点プレビュー → confirm → 高さ計測 → ヒートマップ生成を実行する."""
    assert ctx.pcb_path is not None  # requires_pcb=True
    pcb = PcbFile(ctx.pcb_path)
    top_coppers = [c for c in pcb.copper if c.layer == Layer.TOP]
    probe_config = ctx.machine.probe
    planned_points = sample_points_in_polygons(
        (c.polygon for c in top_coppers),
        min_radius=probe_config.min_radius,
        min_samples=probe_config.min_samples,
        max_samples=probe_config.max_samples,
        outline=pcb.outline.polygon,
        outline_margin=probe_config.board_edge_margin,
    )

    planned_path = ctx.artifacts_dir / "planned_points.png"
    render_planned_points(
        planned_points,
        pcb=pcb,
        title=(
            f"Planned probe points: {ctx.pcb_path.stem} ({len(planned_points)} points)"
        ),
        output_path=planned_path,
    )
    preview = cv2.imread(str(planned_path))
    if preview is not None:
        ctx.frame(Image(preview), persist=True)
    ctx.log(f"計測予定点: /artifacts/{ctx.artifacts_dir.name}/planned_points.png")
    diagnostics = sampling_diagnostics(
        planned_points, [c.polygon for c in top_coppers], pcb.outline.polygon
    )
    if diagnostics is not None:
        ctx.log(
            f"計画点 {diagnostics.point_count} 点 / "
            f"min_clearance {diagnostics.min_clearance:.3f} mm / "
            f"凸包/外形面積比 {diagnostics.hull_area_ratio:.3f}"
        )

    try:
        proceed = ctx.prompt(
            PromptSpec(
                kind="confirm",
                message=f"{len(planned_points)} 点を計測します。続行しますか?",
                default=True,
                true_label="続行",
                false_label="中止",
            )
        )
    finally:
        if preview is not None:
            ctx.clear_frame()
    if not proceed:
        raise JobAborted()

    with ctx.open_camera() as camera:
        result = setup_board(ctx, camera)
        session = PasteSession.from_calibration(result)
        ctx.progress("高さ計測")
        height_plane = session.height_measurer.measure(
            coppers=top_coppers,
            board_to_machine=session.board_to_machine,
            outline=pcb.outline.polygon,
        )

    render_height_plane(
        height_plane,
        pcb=pcb,
        title=f"Height Plane: {ctx.pcb_path.stem}",
        output_path=ctx.artifacts_dir / "height_plane.png",
        pcb_to_plane=session.board_to_machine,
    )
    zs = [p.z for p in height_plane.points]
    return JobResult(
        summary=f"{len(zs)} 点計測 / Z {min(zs):.3f}〜{max(zs):.3f} mm",
        artifacts=(
            ctx.artifact("計測予定点", "planned_points.png", "image"),
            ctx.artifact("ヒートマップ", "height_plane.png", "image"),
        ),
    )


def _run_loading(ctx: JobContext) -> JobResult:
    """全軸 homing と任意位置への移動後、command 駆動ローディングを実行する."""
    klipper = create_command_klipper(ctx.machine)
    stage = XYZStage(klipper.readonly)

    target = {
        axis: float(value)
        for param, axis in _LOADING_POSITION_PARAMS
        if (value := ctx.params.get(param)) is not None
    }

    ctx.progress("ホーミング")
    ctx.log("全軸ホーミングを実行します")
    klipper.send_gcode(gcode.homing(x=True, y=True, z=True) + gcode.wait_for_done())

    if target:
        position_label = ", ".join(
            f"{axis.upper()}={value:.3f} mm" for axis, value in target.items()
        )
        ctx.progress("ローディング位置へ移動")
        ctx.log(f"ローディング位置へ移動します: {position_label}")
        klipper.send_gcode(
            stage.move(
                x=target.get("x"),
                y=target.get("y"),
                z=target.get("z"),
            )
            + gcode.wait_for_done()
        )

    dispenser = PasteDispenser(
        klipper=klipper.readonly,
        rotations_per_ul=ctx.machine.paste_dispenser.rotations_per_ul,
    )
    applicator = PasteApplicator.from_config(
        klipper, dispenser, stage, ctx.machine.paste_dispenser
    )
    with applicator:
        total = _run_loading_loop(ctx, klipper, stage, applicator)
    return JobResult(
        summary=(
            f"押出合計 {total.amount_ul:+.3f} uL / 回転合計 {total.rotations:+.3f} rev"
        )
    )


class _CalibrationContext:
    """吐出量キャリブレーション統合ジョブのワーカーローカル状態.

    共通土台（銅板 + ボード計測 + 平面計測 + transform）を確立し、メニューループ中
    保持し続ける applicator を束ねる。① の検証ループは新 ``rotations_per_ul`` で
    applicator を作り直すため、現在の applicator と現在の ``rotations_per_ul`` /
    ``dispense_accel`` をここで一元管理する。
    """

    def __init__(self, session: PasteSession, transform: Transform) -> None:
        self._session = session
        self._transform = transform
        config = session.machine.paste_dispenser
        self._rotations_per_ul = config.rotations_per_ul
        self._dispense_accel = config.dispense_accel
        self._applicator = session.make_applicator(transform=transform)
        self._applicator.__enter__()

    @property
    def session(self) -> PasteSession:
        return self._session

    @property
    def applicator(self) -> PasteApplicator:
        return self._applicator

    @property
    def rotations_per_ul(self) -> float:
        return self._rotations_per_ul

    @property
    def dispense_accel(self) -> float:
        return self._dispense_accel

    def rebuild_applicator(
        self, *, rotations_per_ul: float, dispense_accel: float
    ) -> None:
        """新 ``rotations_per_ul`` で applicator を作り直す（① 採用時）.

        ``dispense_accel`` も連動更新する。古い applicator は閉じる。
        """
        self._applicator.__exit__(None, None, None)
        self._rotations_per_ul = rotations_per_ul
        self._dispense_accel = dispense_accel
        self._applicator = self._session.make_applicator(
            transform=self._transform, rotations_per_ul=rotations_per_ul
        )
        self._applicator.__enter__()

    def close(self) -> None:
        self._applicator.__exit__(None, None, None)


def _run_dispense_calibration(ctx: JobContext) -> JobResult:
    """銅板に線を引いて ①②③ を検証ループまで回す統合キャリブレーション.

    共通土台（その場生成した矩形銅板 → ボード計測 → 平面計測 → applicator）を
    確立し、メニュー（``run_calib`` コマンド）で ① rotations_per_ul /
    ② max_dispense_rate / ③ max_fill_speed を順次/個別に実行する。各キャリブの
    確定値は採用時点で machine.toml へ即時反映する（中止・失敗でも失われない）。

    算出/判定はすべて pcbasm（``FlowCalibrationSet`` / ``DispenseRateCalibration`` /
    ``FillSpeedSweep`` / ``LineLayout``）に委譲し、ここはループ制御と入出力に徹する。
    """
    board_width = float(ctx.params["board_width"])
    board_height = float(ctx.params["board_height"])
    tolerance = float(ctx.params["tolerance"])

    # 開始時点の設定で ①（line_count 本）②（レート掃引点数）③（速度掃引点数）の
    # いずれかが銅板に収まらない場合はセットアップ前に失敗させる
    # （線パラメータは実行中変更可のため、各キャリブ開始時にも再検証する）。
    needed_lines = max(
        max(1, int(ctx.params["line_count"])),
        max(1, int(ctx.params["rate_divisions"])),
        max(1, int(ctx.params["speed_divisions"])),
    )
    try:
        _build_line_layout(ctx, line_count=needed_lines)
    except LineLayoutOverflowError as exc:
        raise ValueError(_layout_overflow_message(exc)) from exc

    with ctx.open_camera() as camera:
        pcb_path = _generate_calibration_board(ctx, board_width, board_height)
        result = setup_board(ctx, camera, tolerance=tolerance, pcb_path=pcb_path)
        session = PasteSession.from_calibration(result)

        ctx.progress("高さ計測")
        outline_polygon = session.pcb.outline.polygon
        board_copper = Copper(layer=Layer.TOP, polygon=outline_polygon)
        height_plane = session.height_measurer.measure(
            coppers=[board_copper],
            board_to_machine=session.board_to_machine,
            outline=outline_polygon,
        )
        transform = Compose(
            [session.board_transform, session.toolhead_offset, height_plane]
        )

        calib = _CalibrationContext(session, transform)
        try:
            results = _calibration_menu_loop(ctx, calib)
        finally:
            calib.close()

    return _dispense_calibration_result(results)


def _generate_calibration_board(ctx: JobContext, width: float, height: float) -> Path:
    """キャリブ用の外形だけ矩形銅板を artifacts へ生成し、そのパスを返す."""
    # pcbnew 依存はジョブ実行時のみ（KiCAD 未導入でも webui は起動可）
    from pcbasm.pcb.generate import generate_rect_pcb, save_board

    filename = f"dispense_calibration_rect_{width:g}x{height:g}.kicad_pcb"
    path = ctx.artifacts_dir / filename
    save_board(generate_rect_pcb(width, height), path)
    ctx.log(f"キャリブレーション銅板を生成: {filename}（{width:g}x{height:g} mm）")
    return path


@attrs.frozen
class _DispenseCalibrationResults:
    """実施したキャリブの確定値（未実施は None）.

    ``finish`` は ① の選択肢「採用して吐出量キャリブレーションを終了する」で True
    になり、メニューループが検知してジョブ全体を終了する（設定反映へ進む）。
    """

    rotations_per_ul: float | None = None
    dispense_accel: float | None = None
    max_dispense_rate: float | None = None
    max_fill_speed: float | None = None
    finish: bool = False


def parse_run_calib_command(command: Mapping[str, Any]) -> str | None:
    """メニュー用 WS command から実行対象キャリブ名を取り出す.

    ``{type:"run_calib", which: "rotations_per_ul"|"max_dispense_rate"|
    "max_fill_speed"|"all"|"finish"}`` の ``which`` を返す。未知 type /
    未知 which は None。
    """
    match command:
        case {"type": "run_calib", "which": str(which)} if which in _CALIB_WHICH:
            return which
    return None


_CALIB_WHICH = frozenset(
    {"rotations_per_ul", "max_dispense_rate", "max_fill_speed", "all", "finish"}
)


def _calibration_menu_loop(
    ctx: JobContext, calib: _CalibrationContext
) -> _DispenseCalibrationResults:
    """メニューコマンド（``run_calib``）で ①②③ を順次/個別実行するループ.

    ``finish`` で離脱し、それまでに確定した値を返す。``all`` は ①→②→③ を続けて
    実行する。各キャリブの確定値はローカルに蓄積する。

    Raises:
        JobAborted: 待機中に abort された場合
    """
    ctx.progress(CALIBRATION_MENU_STAGE)

    # 滞留コマンドを drain（メニュー段階以前のボタン/ジョグの遅延実行を防ぐ）
    _drain_commands(ctx)
    ctx.log(
        "メニューから ① rotations_per_ul / ② max_dispense_rate / "
        "③ max_fill_speed を選んで実行し、終了ボタンで設定反映へ進んでください"
    )

    results = _DispenseCalibrationResults()
    while True:
        command = ctx.next_command(timeout=None)
        assert command is not None  # timeout=None は取得（or abort）までブロック
        which = parse_run_calib_command(command)
        if which is None:
            _handle_menu_loading_or_machine(ctx, calib, command)
            continue

        if which == "finish":
            ctx.log("吐出量キャリブレーションを終了します")
            return results
        # サブキャリブ中の「中止」はメニューへ戻る（誤選択のやり直し）。all 実行中の
        # 中止は以降のサブキャリブをスキップしてメニューへ。JobAborted は捕捉しない。
        try:
            if which in ("rotations_per_ul", "all"):
                results = _calibrate_rotations_per_ul(ctx, calib, results)
                if results.finish:  # ① の「採用して終了」でジョブ全体を終了
                    ctx.log("吐出量キャリブレーションを終了します")
                    return results
            if which in ("max_dispense_rate", "all"):
                results = _calibrate_max_dispense_rate(ctx, calib, results)
            if which in ("max_fill_speed", "all"):
                results = _calibrate_max_fill_speed(ctx, calib, results)
        except _CalibrationCancelled:
            ctx.log("キャリブレーションを中止しました。メニューへ戻ります")
        ctx.progress(CALIBRATION_MENU_STAGE)
        ctx.log("メニューに戻りました。次のキャリブを選ぶか終了してください")


def _handle_menu_loading_or_machine(
    ctx: JobContext, calib: _CalibrationContext, command: Mapping[str, Any]
) -> None:
    """メニュー段階の非 run_calib コマンドを処理する.

    プライム用の押出/吸引（``extrude`` / ``suck``）は applicator へ、マシン操作は
    ``handle_machine_command`` へ委譲する。loading_controls の終了ボタン
    （``finish``）は誤操作ガードとして無視する（終了はメニューの終了ボタンを使う）。
    """
    action = parse_loading_command(command)
    match action:
        case Extrude(amount=amount):
            calib.applicator.load(amount)
            ctx.log(f"プライム押出: {amount:+.3f} uL")
        case Finish():
            ctx.log("終了はメニューの「終了」ボタンを使ってください")
        case InvalidLoadingCommand(reason=reason):
            ctx.log(reason)
        case _:
            klipper = calib.session.klipper
            stage = calib.session.stage
            if not handle_machine_command(ctx, klipper, stage, command, focus_z=None):
                ctx.log(f"未知のコマンドです: {command.get('type')!r}")


def _build_line_layout(ctx: JobContext, line_count: int | None = None) -> LineLayout:
    """段ずらしレイアウトをパラメータから構築する.

    ``line_count`` 省略時はパラメータ ``line_count``（① 用）。②③ は掃引点数を
    渡し、掃引点ごとに専用の線位置を確保する（位置の再利用＝重ね書きをしない）。

    Raises:
        LineLayoutOverflowError: 折り返しても線が銅板の描画領域に収まらない場合
    """
    if line_count is None:
        line_count = max(1, int(ctx.params["line_count"]))
    return LineLayout(
        line_length=float(ctx.params["line_length"]),
        line_count=line_count,
        row_pitch=float(ctx.params["row_pitch"]),
        board_width=float(ctx.params["board_width"]),
        board_height=float(ctx.params["board_height"]),
        margin=DISPENSE_CALIBRATION_LAYOUT_MARGIN,
    )


def _layout_overflow_message(exc: LineLayoutOverflowError) -> str:
    """レイアウト超過をユーザーに調整を促す文言へ変換する."""
    return (
        f"線 {exc.line_count} 本は折り返しても銅板の描画領域に収まりません"
        f"（最大 {exc.capacity} 本）。線の本数/分割数・線の長さ・段ずらし間隔を"
        "調整してください"
    )


def _line_layout(ctx: JobContext, line_count: int | None = None) -> LineLayout:
    """各キャリブ用のレイアウトを構築する（収まらなければメニューへ戻す）.

    線パラメータは実行中変更可のため、超過時はエラーを log してメニューへ
    戻し、調整して再実行できるようにする。

    Raises:
        _CalibrationCancelled: 線が銅板の描画領域に収まらない場合
    """
    try:
        return _build_line_layout(ctx, line_count)
    except LineLayoutOverflowError as exc:
        ctx.log(_layout_overflow_message(exc))
        raise _CalibrationCancelled() from exc


def _removal_z(ctx: JobContext, calib: _CalibrationContext) -> float:
    """計量で基板を取り出すときの退避 Z = max(z_min, z_max - offset).

    既定 offset=0 で z_max（フルリトラクト）。``removal_z_offset`` は実行中変更可で、
    純粋に ``ctx.params`` を読むため呼び出しごとに最新値を反映する。
    """
    z = calib.session.stage.limits.z
    return max(z.min, z.max - float(ctx.params["removal_z_offset"]))


def _move_to_removal_z(ctx: JobContext, calib: _CalibrationContext) -> None:
    """線引き後にヘッドを退避 Z（= max(z_min, z_max - offset)）へ上げる.

    計量のため基板を取り出しやすくする退避。
    """
    removal_z = _removal_z(ctx, calib)
    ctx.log(
        f"ヘッドを退避 Z={removal_z:.3f} へ移動します。基板を取り出して計測してください"
    )
    calib.session.klipper.send_gcode(
        calib.session.stage.move(z=removal_z) + gcode.wait_for_done()
    )


def _weighed_draw(
    ctx: JobContext,
    calib: _CalibrationContext,
    *,
    tare_message: str,
    mass_message: str,
    draw: Callable[[], None],
) -> float:
    """タール confirm → ``draw()`` → 退避 → 質量入力の 1 計量ラウンドを実行する.

    線引き前に基板ごとタール（ゼロ）しておき、線引き後の計量値がそのまま
    ペーストの質量になるようにする。タール／質量入力の中止はいずれも
    ``_CalibrationCancelled`` でメニューへ戻る。

    Returns:
        入力された質量 (mg)
    """
    _prompt_confirm(ctx, tare_message)
    draw()
    _move_to_removal_z(ctx, calib)
    return _prompt_mass(ctx, mass_message)


def _layout_for_sweep(
    ctx: JobContext, points: Sequence[float], empty_message: str
) -> LineLayout | None:
    """掃引点ごとに専用の線位置を確保したレイアウトを作る.

    掃引列が空なら ``empty_message`` を log して None（呼び出し側がスキップ）。
    line_count に関係なく掃引点数ぶんの線位置を取り、重ね書きをしない。

    Raises:
        _CalibrationCancelled: 線が銅板の描画領域に収まらない場合
    """
    if not points:
        ctx.log(empty_message)
        return None
    return _line_layout(ctx, line_count=len(points))


def _calibrate_rotations_per_ul(
    ctx: JobContext,
    calib: _CalibrationContext,
    results: _DispenseCalibrationResults,
) -> _DispenseCalibrationResults:
    """① rotations_per_ul をローディング → 線引き → 計量 → 採用/再計測ループで確定する.

    各ラウンドの先頭でヘッドを Z=0 に上げてプライム/ふき取り（専用ローディング段階）を
    行い、電子天秤にセットしてタール（ゼロ）してから ``line_count`` 本の線を段ずらしで
    引く。線引き後はヘッドを退避 Z（z_max − removal_z_offset、既定は全退避）へ上げ、
    基板を取り出して計量しやすくする。合計質量から ``FlowCalibrationSet`` で新 ``rotations_per_ul`` を算出する。
    採用すると新値で applicator を作り直し、``dispense_accel`` も回転加速度を保って
    連動更新する。採用時点で両値を machine.toml へ即時反映するため、以降の中止・失敗
    でも計測結果は失われない。収束（前後の相対差が許容内）はヒントとして表示するのみで、
    ループ継続はユーザー判断。タール前の中止・質量入力の中止はいずれもメニューへ戻る。
    """
    # 比重はマシン設定 (solder_paste_density [mg/uL]。水基準なので比重と数値が一致) を
    # 真実とする。② が密度を machine から直接読むのと同じ扱い。
    specific_gravity = ctx.machine.paste_dispenser.solder_paste_density
    ctx.log(
        f"ペースト比重（machine.toml の solder_paste_density）= {specific_gravity:.3f}"
    )

    while True:
        # 線設定・塗布量は実行中変更可。ラウンド先頭で読み直し次ラウンドから反映する。
        layout = _line_layout(ctx)
        amount = float(ctx.params["line_amount"])
        # ── 専用ローディング段階：ヘッドを Z=0 に上げてプライム/ふき取り ──
        # Z=0 へ上げることでローディング中のノズルふき取りがしやすくなる。
        ctx.log("ヘッドを Z=0 に上げます。プライム/ふき取りをしてください")
        calib.session.klipper.send_gcode(
            calib.session.stage.move(z=0.0) + gcode.wait_for_done()
        )
        _run_loading_loop(
            ctx, calib.session.klipper, calib.session.stage, calib.applicator
        )

        previous_rpu = calib.rotations_per_ul
        rotations_used = layout.line_count * amount * previous_rpu

        def draw_lines() -> None:
            ctx.progress("① rotations_per_ul: 線引き")
            # プライム済みのペーストを baseline まで引き戻してから引き始める
            # （各 draw_line の FillSequence が prime→吐出→retract を内包するので、
            #   線間・線後の追加 retract は不要）
            calib.applicator.retract()
            for index in range(layout.line_count):
                ctx.checkpoint()
                start, end = layout.line(index)
                calib.applicator.draw_line(start, end, amount=amount)
                ctx.log(f"線 {index + 1}/{layout.line_count} を {amount:.3f} uL で塗布")

        # 電子天秤にセットしてタール（ゼロ）→ 線引き → 退避 → 計量の 1 ラウンド
        # （タール前・質量入力の中止はいずれもメニューへ戻る）。
        mass = _weighed_draw(
            ctx,
            calib,
            tare_message=(
                "基板を電子天秤に載せてタール（ゼロ）し、基板を装置へ戻してから"
                "続行を押してください。続行すると線引きへ進みます。"
            ),
            mass_message=(
                f"基板を取り出して計量し、{layout.line_count} 本の線の合計質量 (mg) "
                f"を入力（回転数 {rotations_used:.4f} rev 相当）"
            ),
            draw=draw_lines,
        )

        flow = FlowCalibrationSet(
            rotations=rotations_used,
            masses_mg=(mass,),
            specific_gravity=specific_gravity,
        )
        computed_rpu = flow.rotations_per_ul
        # 回転加速度 [rev/sec²] を保ったまま dispense_accel を新 rpu で再算出する
        computed_accel = flow.rescaled_dispense_accel(
            previous_dispense_accel=calib.dispense_accel,
            previous_rotations_per_ul=previous_rpu,
        )
        round_result = RotationsPerUlRound(previous=previous_rpu, computed=computed_rpu)
        ctx.log(
            f"算出 rotations_per_ul = {computed_rpu:.6f} rev/uL "
            f"(前回 {previous_rpu:.6f}, 相対変化 "
            f"{round_result.relative_change * 100:.2f}%)"
        )
        ctx.log(f"連動 dispense_accel = {computed_accel:.6f} uL/s^2")
        if round_result.converged(DISPENSE_CALIBRATION_CONVERGENCE_REL_TOL):
            ctx.log(
                f"相対変化が許容 {DISPENSE_CALIBRATION_CONVERGENCE_REL_TOL * 100:.0f}% "
                "以内です（収束）"
            )

        choice = ctx.prompt(
            PromptSpec(
                kind="choice",
                message="算出した rotations_per_ul をどうしますか?",
                choices=(
                    "採用して再計測する",
                    "採用せず再計測する",
                    "採用して他のキャリブレーションへ進む",
                    "採用して吐出量キャリブレーションを終了する",
                ),
                default="採用して他のキャリブレーションへ進む",
            )
        )
        if choice == "採用せず再計測する":
            # 算出値は採用せず、現状の rotations_per_ul のまま次ラウンドへ。
            continue

        # 残る 3 つはいずれも算出値を採用する。採用時点で machine.toml へ反映し、
        # 以降の中止・失敗で計測結果を失わないようにする。
        _apply_to_machine_toml(
            ctx,
            {
                "paste_dispenser.rotations_per_ul": computed_rpu,
                "paste_dispenser.dispense_accel": computed_accel,
            },
        )
        adopted = attrs.evolve(
            results, rotations_per_ul=computed_rpu, dispense_accel=computed_accel
        )
        if choice == "採用して吐出量キャリブレーションを終了する":
            # 再描画しないので applicator の作り直しは不要。finish でジョブを終了。
            return attrs.evolve(adopted, finish=True)
        # 「再計測」「他のキャリブへ進む」は新値で applicator を作り直す。
        calib.rebuild_applicator(
            rotations_per_ul=computed_rpu, dispense_accel=computed_accel
        )
        ctx.log("新 rotations_per_ul で applicator を再構成しました")
        if choice == "採用して再計測する":
            continue
        return adopted  # 採用して他のキャリブレーションへ進む


def _calibrate_max_dispense_rate(
    ctx: JobContext,
    calib: _CalibrationContext,
    results: _DispenseCalibrationResults,
) -> _DispenseCalibrationResults:
    """② max_dispense_rate を吐出効率の落ち検出で確定する.

    レート列の各点で線を引いて計量し、効率 ``measured_ul / commanded_ul`` の
    落ちから ``DispenseRateCalibration`` が ``max_dispense_rate`` を判定する。
    ``FillSequence`` は移動速度から吐出レートを導出し ``rate_cap`` は頭打ちに
    しか働かないため、移動速度（``max_fill_speed``）は固定したまま吐出量を
    ``rate_sweep_amount``（= rate × 線長 / 速度）でレートに比例させて指令
    レートを実現する（``line_amount`` は使わない。cap を超えるのはこの掃引の
    線引きだけで、実塗布の clamp 動作は変えない）。各点は独立計測のため、
    線引き前に基板ごとタール（ゼロ）を確認してから引き、線引き後に退避 Z
    （z_max − removal_z_offset）へ上げて質量を入力させる。タール／質量入力の
    中止はいずれもメニューへ戻る。自動判定値を default に手動上書き可能で、
    確定値は machine.toml へ即時反映する。
    """
    fill_speed = ctx.machine.paste_dispenser.max_fill_speed
    rate_min = float(ctx.params["rate_min"])
    rate_max = float(ctx.params["rate_max"])
    divisions = max(1, int(ctx.params["rate_divisions"]))
    density = ctx.machine.paste_dispenser.solder_paste_density

    rates = dispense_rate_schedule(rate_min, rate_max, divisions)
    layout = _layout_for_sweep(
        ctx,
        rates,
        "吐出レート列が生成できません（rate_min / rate_max / divisions を確認）。"
        "② をスキップします",
    )
    if layout is None:
        return results

    ctx.log(
        f"移動速度 {fill_speed:.3f} mm/s 固定・"
        "吐出量 = レート × 線長 / 速度 でレートを掃引します"
    )
    measurements: list[RateMeasurement] = []
    # baseline まで引き戻してから引き始める（draw_line が retract を内包するので線間は不要）
    calib.applicator.retract()
    for index, rate in enumerate(rates):
        ctx.progress("② max_dispense_rate: 線引き", 100.0 * index / len(rates))
        ctx.checkpoint()
        # 移動速度は変えず、吐出量をレートに比例させて指令レートを実現する
        # （固定量のままだと吐出レートが移動速度由来の導出値で頭打ちされ、
        #   掃引しても全点が同一レートになる）。
        amount = rate_sweep_amount(rate, layout.line_length, fill_speed)
        # レートごとの専用位置に段ずらし（折り返し込み）で引く
        start, end = layout.line(index)

        def draw_line() -> None:
            calib.applicator.draw_line(
                start, end, amount=amount, max_fill_speed=fill_speed, rate_cap=rate
            )

        # 各レートは独立計測。タール（ゼロ）→ 線引き → 退避 → 計量の 1 ラウンド
        # （中止でメニューへ）。
        mass = _weighed_draw(
            ctx,
            calib,
            tare_message=(
                f"[{index + 1}/{len(rates)}] 基板を電子天秤に載せてタール（ゼロ）し、"
                "基板を装置へ戻してから続行を押してください。"
                f"続行するとレート {rate:.3f} uL/s（吐出量 {amount:.3f} uL）の"
                "線引きへ進みます"
            ),
            mass_message=(
                f"[{index + 1}/{len(rates)}] 基板を取り出して計量し、"
                f"レート {rate:.3f} uL/s の線の質量 (mg) を入力"
            ),
            draw=draw_line,
        )
        measured_ul = mass / density
        measurement = RateMeasurement(
            rate=rate, measured_ul=measured_ul, commanded_ul=amount
        )
        measurements.append(measurement)
        ctx.log(
            f"レート {rate:.3f} uL/s: 実測 {measured_ul:.4f} uL / "
            f"指令 {amount:.4f} uL → 効率 {measurement.efficiency:.3f}"
        )

    calibration = DispenseRateCalibration(measurements=tuple(measurements))
    auto = calibration.max_dispense_rate
    if auto is None:
        ctx.log(
            "効率の落ちを自動判定できませんでした"
            f"（baseline 効率 {calibration.baseline_efficiency:.3f}）。"
            "手動で max_dispense_rate を入力してください"
        )
    else:
        ctx.log(f"自動判定 max_dispense_rate = {auto:.3f} uL/s")

    chosen = _prompt_positive_number(
        ctx, "採用する max_dispense_rate (uL/s) を入力", default=auto
    )
    assert chosen is not None  # cancel_label 無しの prompt は常に正数を返す
    _apply_to_machine_toml(ctx, {"paste_dispenser.max_dispense_rate": chosen})
    return attrs.evolve(results, max_dispense_rate=chosen)


def _calibrate_max_fill_speed(
    ctx: JobContext,
    calib: _CalibrationContext,
    results: _DispenseCalibrationResults,
) -> _DispenseCalibrationResults:
    """③ max_fill_speed を速度スイープの目視選択で確定する.

    速度列の各点で線を引き（実塗布同等の総量を固定）、番号→速度の対応を全 log
    する。各線は ``draw_line(max_fill_speed=v, rate_cap=inf)`` で引き、実効塗布速度を
    v 自体に固定する。``max_fill_speed`` を per-line に上書きするのは、③ が config の
    現行値を超える速度域まで測る必要があるため（既定値で頭打ちさせない）。``rate_cap``
    を無効化するのは、q 非依存の移動速度限界を測る目的上、吐出レート上限で move 速度を
    律速させないため。連続して綺麗に引けた最大の番号を choice prompt で選び、
    ``FillSpeedSweep.speed_at(index)`` で ``max_fill_speed`` を確定する。
    """
    speed_min = float(ctx.params["speed_min"])
    speed_max = float(ctx.params["speed_max"])
    divisions = max(1, int(ctx.params["speed_divisions"]))
    ul_per_mm2 = ctx.machine.paste_dispenser.ul_per_mm2
    bead_width = (
        ctx.machine.paste_dispenser.nozzle_diameter
        * ctx.machine.paste_dispenser.bead_width_factor
    )

    speeds = fill_speed_schedule(speed_min, speed_max, divisions)
    layout = _layout_for_sweep(
        ctx,
        speeds,
        "塗布速度列が生成できません（speed_min / speed_max / divisions を確認）。"
        "③ をスキップします",
    )
    if layout is None:
        return results
    # 実塗布同等の総量。q = total_amount / line_length（実効単位長さ量）は
    # FillSequence 側が rate から逆算するため、ここでは move 速度を直接渡す。
    total_amount = ul_per_mm2 * slot_area(layout.line_length, bead_width)

    sweep = FillSpeedSweep(speeds=tuple(speeds))
    # baseline まで引き戻してから引き始める（draw_line が retract を内包するので線間は不要）
    calib.applicator.retract()
    for index, speed in enumerate(speeds):
        ctx.progress("③ max_fill_speed: 線引き", 100.0 * index / len(speeds))
        ctx.checkpoint()
        start, end = layout.line(index)
        actual = calib.applicator.draw_line(
            start, end, amount=total_amount, max_fill_speed=speed, rate_cap=math.inf
        )
        actual_mm_s = (
            actual.resolve(calib.session.stage.max_velocity)
            if actual is not None
            else None
        )
        actual_text = (
            f" / 実効 {actual_mm_s:.2f} mm/s" if actual_mm_s is not None else ""
        )
        ctx.log(f"番号 {index}: 速度 {speed:.2f} mm/s{actual_text}")

    selection = ctx.prompt(
        PromptSpec(
            kind="choice",
            message="連続して綺麗に引けた最大の番号を選んでください",
            choices=tuple(
                f"{index}: {speed:.2f} mm/s" for index, speed in enumerate(speeds)
            ),
            default=f"0: {speeds[0]:.2f} mm/s",
        )
    )
    chosen_index = int(str(selection).split(":", 1)[0])
    chosen = sweep.speed_at(chosen_index)
    assert chosen is not None  # 選択肢は speeds の範囲内
    _apply_to_machine_toml(ctx, {"paste_dispenser.max_fill_speed": chosen})
    return attrs.evolve(results, max_fill_speed=chosen)


def _dispense_calibration_result(
    results: _DispenseCalibrationResults,
) -> JobResult:
    """実施したキャリブの確定値から summary を組む（値は採用時に反映済み）."""
    summary_parts = [
        f"{field} = {value:.6f}"
        for field in (
            "rotations_per_ul",
            "dispense_accel",
            "max_dispense_rate",
            "max_fill_speed",
        )
        if (value := getattr(results, field)) is not None
    ]
    if not summary_parts:
        return JobResult(summary="キャリブレーションを実施せず終了しました")
    return JobResult(
        summary=(
            "キャリブレーション結果（machine.toml 反映済み）: "
            + " / ".join(summary_parts)
        )
    )


def _run_generate_rect_pcb(ctx: JobContext) -> JobResult:
    """Flow calibration 用の外形だけ矩形 PCB を生成する（装置・カメラ不要）."""
    # pcbnew 依存はジョブ実行時のみ（KiCAD 未導入でも webui は起動可）
    from pcbasm.pcb.generate import generate_rect_pcb, save_board

    width = float(ctx.params["width"])
    height = float(ctx.params["height"])

    ctx.progress("生成")
    filename = f"flow_calibration_rect_{width:g}x{height:g}.kicad_pcb"
    save_board(generate_rect_pcb(width, height), ctx.artifacts_dir / filename)
    ctx.log(f"生成: {filename}（{width:g}x{height:g} mm）")
    ctx.progress("完了", 100.0)

    return JobResult(
        summary=f"{width:g}x{height:g} mm の矩形 PCB を生成しました",
        artifacts=(ctx.artifact("キャリブレーション矩形 PCB", filename, "file"),),
    )


def _run_toolhead_offset(ctx: JobContext) -> JobResult:
    """複数点のペースト吐出と円検出からツールヘッドXYオフセットを計測する."""
    tolerance = float(ctx.params["tolerance"])
    lift_height = float(ctx.params["lift_height"])
    diameter_min = float(ctx.params["paste_diameter_min"])
    diameter_max = float(ctx.params["paste_diameter_max"])
    point_count = int(ctx.params["point_count"])
    point_spacing = float(ctx.params["point_spacing"])
    edge_margin = float(ctx.params["edge_margin"])
    if not 0 <= diameter_min < diameter_max:
        raise ValueError("検出円の直径は 0 <= 最小直径 < 最大直径 である必要があります")

    # 配置不能ならカメラやKlipperを開始する前に中止する。
    assert ctx.pcb_path is not None  # requires_pcb=True
    planned_points = plan_toolhead_offset_points(
        PcbFile(ctx.pcb_path).outline.polygon,
        point_count=point_count,
        point_spacing=point_spacing,
        edge_margin=edge_margin,
        paste_diameter_max=diameter_max,
    )
    total_points = len(planned_points)
    ctx.log(
        f"計測点を {total_points} 点配置"
        f"（最小間隔 {point_spacing:g} mm / 外周margin {edge_margin:g} mm）"
    )

    with ctx.open_camera() as camera:
        result = setup_board(ctx, camera, tolerance=tolerance)
        machine = result.machine
        klipper = result.klipper
        stage = result.stage
        calibration = result.calibration
        probe_config = machine.probe
        probe_executor = ProbeExecutor(
            klipper=klipper,
            stage=stage,
            lift_height=probe_config.lift_height,
        )
        dispenser_config = machine.paste_dispenser
        paste_dispenser = PasteDispenser(
            klipper=klipper.readonly,
            rotations_per_ul=dispenser_config.rotations_per_ul,
        )
        toolhead_transform = dispenser_config.toolhead.to_transform()

        # 高さ計測フェーズ: 全計測点を先にプローブし、後続フェーズで使う絶対Zを保存する。
        measured_points: list[tuple[Point2d, Point2d, Point2d, float]] = []
        for index, board_position in enumerate(planned_points, start=1):
            ctx.checkpoint()
            camera_position = result.board_transform.apply(board_position)
            dispense_position = toolhead_transform.apply(camera_position)
            ctx.progress(
                f"高さ計測 {index}/{total_points}",
                100.0 * (index - 1) / (3 * total_points),
            )
            klipper.send_gcode(
                stage.move(x=dispense_position.x, y=dispense_position.y)
                + gcode.wait_for_done()
            )
            board_surface_z = probe_executor.probe()
            measured_points.append(
                (
                    board_position,
                    camera_position,
                    dispense_position,
                    board_surface_z,
                )
            )
            ctx.log(
                f"高さ {index}/{total_points}: "
                f"board=({board_position.x:.3f}, {board_position.y:.3f}) / "
                f"dispense=({dispense_position.x:.3f}, "
                f"{dispense_position.y:.3f}) / "
                f"surface Z={board_surface_z:.4f}"
            )

        with PasteApplicator.from_config(
            klipper,
            paste_dispenser,
            stage,
            dispenser_config,
            transform=Identity(),
            lift_height=lift_height,
        ) as applicator:
            # ペーストフェーズの直前に一度だけロードする。
            klipper.send_gcode(stage.move(z=0.0) + gcode.wait_for_done())
            _run_loading_loop(
                ctx, klipper, stage, applicator, focus_z=calibration.z_position
            )
            applicator.retract()

            dispense_amount = float(ctx.params["dispense_amount"])
            paste_height = resolve_paste_height(
                dispenser_config.paste_height, dispenser_config.ul_per_mm2
            )
            for index, (
                _board_position,
                _camera_position,
                dispense_position,
                board_surface_z,
            ) in enumerate(measured_points, start=1):
                ctx.checkpoint()
                ctx.progress(
                    f"ペースト塗布 {index}/{total_points}",
                    100.0 * (total_points + index - 1) / (3 * total_points),
                )
                # applicatorはIdentity transformなので、machine XYと絶対Zを渡す。
                applicator.deposit_at(
                    dispense_position,
                    amount=dispense_amount,
                    transform=Identity(),
                    paste_height=board_surface_z + paste_height,
                )
                ctx.log(
                    f"塗布 {index}/{total_points}: "
                    f"dispense=({dispense_position.x:.3f}, "
                    f"{dispense_position.y:.3f}) / "
                    f"surface Z={board_surface_z:.4f}"
                )

        # オフセット計測フェーズ: 全点の塗布完了後に画像で位置を計測する。
        paste_roi_side = max(1, round(point_spacing * calibration.pixel_per_mm))
        paste_roi_size = (paste_roi_side, paste_roi_side)
        ctx.log(
            f"円検出ROI: {point_spacing:g} x {point_spacing:g} mm"
            f"（{paste_roi_side} x {paste_roi_side} px）"
        )
        paste_detector = CircleDetector(
            pixel_per_mm=calibration.pixel_per_mm,
            target_diameter_mm=(diameter_min + diameter_max) / 2,
            crop_size=paste_roi_size,
            diameter_tolerance_mm=(diameter_max - diameter_min) / 2,
        )
        paste_observer = OffsetObserver(
            detector=paste_detector,
            camera=result.camera,
            crop_size=paste_roi_size,
            frame_sink=ctx.frame,
            minimum_sample_count=_TOOLHEAD_OFFSET_MIN_FRAME_DETECTIONS,
            max_attempts=_TOOLHEAD_OFFSET_DETECTION_MAX_ATTEMPTS,
            retry_delay=_TOOLHEAD_OFFSET_DETECTION_RETRY_DELAY,
            max_standard_deviation_mm=tolerance,
        )
        paste_adjustor = XYPositionAdjustor(
            observe=paste_observer.observe,
            klipper=klipper,
            stage=stage,
            offset_transform=result.offset_transform,
            tolerance=tolerance,
        )
        samples: list[ToolheadOffsetSample] = []
        diagnostic_path = ctx.artifacts_dir / "toolhead_offset_diagnostics.json"
        detection_failures: list[dict[str, Any]] = []
        diagnostics: dict[str, Any] = {
            "requested_point_count": total_points,
            "minimum_valid_point_count": MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT,
            "successful_point_count": 0,
            "failures": detection_failures,
        }
        failure_images: list[tuple[int, str]] = []
        for index, (
            board_position,
            camera_position,
            dispense_position,
            _board_surface_z,
        ) in enumerate(measured_points, start=1):
            ctx.checkpoint()
            ctx.progress(
                f"オフセット計測 {index}/{total_points}",
                100.0 * (2 * total_points + index - 1) / (3 * total_points),
            )
            klipper.send_gcode(
                stage.move(
                    x=camera_position.x,
                    y=camera_position.y,
                    z=calibration.z_position,
                )
                + gcode.wait_for_done()
            )
            time.sleep(1.0)
            try:
                camera_final_position = paste_adjustor.adjust()
            except CircleDetectionError as exc:
                failure_image = result.camera.capture().crop_center(paste_roi_size)
                filename = f"toolhead_offset_failure_{index:02d}.png"
                failure_image.save(ctx.artifacts_dir / filename)
                ctx.frame(failure_image, persist=True)
                failure_images.append((index, filename))
                detection_failures.append(
                    {
                        "index": index,
                        "board_position": {
                            "x": board_position.x,
                            "y": board_position.y,
                        },
                        "reason": str(exc),
                        "image": filename,
                    }
                )
                diagnostics["successful_point_count"] = len(samples)
                diagnostic_path.write_text(
                    json.dumps(diagnostics, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                ctx.log(
                    f"オフセット計測 {index}/{total_points}: 円検出失敗のためスキップ"
                    f"（{exc}）"
                )
                ctx.log(f"失敗画像: /artifacts/{ctx.artifacts_dir.name}/{filename}")
                continue
            sample = ToolheadOffsetSample.from_positions(
                board_position=board_position,
                dispense_position=dispense_position,
                camera_position=camera_final_position,
            )
            samples.append(sample)
            ctx.log(
                f"オフセット {index}/{total_points}: "
                f"board=({board_position.x:.3f}, {board_position.y:.3f}) / "
                f"dispense=({dispense_position.x:.3f}, "
                f"{dispense_position.y:.3f}) / "
                f"camera=({camera_final_position.x:.3f}, "
                f"{camera_final_position.y:.3f}) / "
                f"offset=({sample.offset.x:+.4f}, {sample.offset.y:+.4f})"
            )

    if failure_images:
        diagnostics["successful_point_count"] = len(samples)
        diagnostic_path.write_text(
            json.dumps(diagnostics, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        ctx.log(
            "円検出診断: " f"/artifacts/{ctx.artifacts_dir.name}/{diagnostic_path.name}"
        )
    if len(samples) < MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT:
        raise RuntimeError(
            "ツールヘッドオフセットの有効な計測点が不足しています"
            f"（有効 {len(samples)} 点 / "
            f"最低 {MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT} 点）"
        )

    # オフセット算出 & 保存
    offset_result = ToolheadOffsetResult.measure(
        samples,
        tolerance=tolerance,
        point_spacing=point_spacing,
        edge_margin=edge_margin,
        calibrated_at=datetime.now(),
    )
    measured_offset = offset_result.offset
    standard_deviation = offset_result.standard_deviation
    offset_result.save(ctx.artifacts_dir / "toolhead_offset.json")
    if not offset_result.is_within_tolerance:
        ctx.log(
            "ばらつきの大きい計測結果: "
            f"/artifacts/{ctx.artifacts_dir.name}/toolhead_offset.json"
        )
        raise RuntimeError(
            "ツールヘッドオフセットの標準偏差が位置合わせ許容誤差を超えました"
            f"（X={standard_deviation.x:.4f}, Y={standard_deviation.y:.4f} mm / "
            f"上限={tolerance:.4f} mm）"
        )

    current_toolhead = dispenser_config.toolhead
    diff_x = measured_offset.x - current_toolhead.x
    diff_y = measured_offset.y - current_toolhead.y
    ctx.progress("完了", 100.0)
    artifacts = [
        ctx.artifact("計測結果 JSON", "toolhead_offset.json", "file"),
    ]
    if failure_images:
        artifacts.append(
            ctx.artifact("円検出診断 JSON", "toolhead_offset_diagnostics.json", "file")
        )
        artifacts.extend(
            ctx.artifact(f"円検出失敗 {index}", filename, "image")
            for index, filename in failure_images
        )
    return JobResult(
        summary=(
            f"{len(samples)}/{total_points}点の平均オフセット "
            f"X={measured_offset.x:+.4f} Y={measured_offset.y:+.4f} mm"
            f"（標準偏差 X={standard_deviation.x:.4f} "
            f"Y={standard_deviation.y:.4f} mm / 現在設定との差 "
            f"dX={diff_x:+.4f} dY={diff_y:+.4f}）"
        ),
        artifacts=tuple(artifacts),
        apply=ApplyPayload(
            label=(
                f"[paste_dispenser.toolhead] x={measured_offset.x:.4f}, "
                f"y={measured_offset.y:.4f} を設定に反映"
            ),
            values={
                "paste_dispenser.toolhead.x": round(measured_offset.x, 4),
                "paste_dispenser.toolhead.y": round(measured_offset.y, 4),
            },
        ),
    )
