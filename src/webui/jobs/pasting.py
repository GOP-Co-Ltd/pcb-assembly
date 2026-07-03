"""Pasting タブのジョブ定義（塗布 / 高さ計測 / ローディング / キャリブレーション）."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import attrs
import cv2
from shapely import Polygon

from pcbasm import gcode
from pcbasm.config import Machine
from pcbasm.geometry import (
    Compose,
    Identity,
    Point2d,
    Transform,
    sample_points_in_polygons,
    sampling_diagnostics,
    transform_polygon,
)
from pcbasm.hal import (
    Camera,
    Klipper,
    PasteDispenser,
    XYZStage,
)
from pcbasm.pasting import (
    DispenseRateCalibration,
    FillSpeedSweep,
    FlowCalibrationSet,
    LineLayout,
    PasteApplicator,
    PasteSettingsModel,
    ProbeExecutor,
    RateMeasurement,
    ResolvedPaste,
    RotationsPerUlRound,
    ToolheadOffsetResult,
    base_override_from_config,
    dispense_rate_schedule,
    fill_speed_schedule,
    plan_paste_route,
    resolve_pad_settings,
    slot_area,
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
    BoardCalibrationResult,
    ComponentAlignments,
    ComponentPads,
    OffsetObserver,
    PadAlignmentResult,
    PadAlignmentSession,
    XYPositionAdjustor,
    setup_board_calibration,
    sorted_top_component_pads,
)
from pcbasm.session import PasteSession
from pcbasm.vision import CircleDetector, Image
from pcbasm.visualization import (
    render_height_plane,
    render_planned_points,
)
from webui.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from webui.jobs.context import JobAborted, JobContext, PromptSpec
from webui.jobs.machine_commands import create_command_klipper, handle_machine_command
from webui.jobs.manager import ApplyPayload, Artifact, JobResult

# ローディングフェーズの progress stage 名
# （loading_controls.html の data 属性・テストでピンする契約値）
LOADING_STAGE = "ローディング"
LOADING_DEFAULT_AMOUNT = 0.1
LOADING_DEFAULT_ROTATIONS = 5.0
LOADING_DEFAULT_ROTATION_RATE = 0.5
LOADING_DEFAULT_ROTATION_ACCEL = 0.5
LOADING_DEFAULT_RETRACT_ROTATIONS = 0.0
APPLY_DIGITS = 6

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
# ① 段ずらしレイアウトの origin（銅板左下からのマージン）
DISPENSE_CALIBRATION_LAYOUT_MARGIN = 5.0


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
class LoadingTotals:
    """ローディングループ内の押出合計."""

    amount_ul: float = 0.0
    rotations: float = 0.0


type LoadingAction = Extrude | Rotate | Finish


def parse_loading_command(command: Mapping[str, Any]) -> LoadingAction | None:
    """ローディング用 WS command を LoadingAction へ変換する.

    ``{type:"extrude", amount: 正数}`` → ``Extrude(+amount)``、
    ``{type:"suck", amount: 正数}`` → ``Extrude(-amount)``、
    ``{type:"finish"}`` → ``Finish()``。
    amount 欠落・非正・非数・未知 type は None。
    """
    match command:
        case {"type": "extrude" | "suck" as kind, "amount": amount}:
            value = _positive_amount(amount)
            if value is None:
                return None
            return Extrude(value if kind == "extrude" else -value)
        case {
            "type": "extrude_rotations" | "suck_rotations" as kind,
            "rotations": rotations,
            "rate": rate,
            "accel": accel,
        }:
            rotation_value = _positive_amount(rotations)
            rate_value = _positive_amount(rate)
            accel_value = _positive_amount(accel)
            if rotation_value is None or rate_value is None or accel_value is None:
                return None
            if kind == "suck_rotations":
                return Rotate(-rotation_value, rate_value, accel_value)
            retract_value = _non_negative_amount(command.get("retract_rotations", 0.0))
            if retract_value is None:
                return None
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
            label="Paste Solder",
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
            ),
            requires_pcb=True,
            uses_machine=True,
            accepts_commands=True,
        )
    )
    catalog.register(
        JobDefinition(
            name="height_plane",
            label="Height Plane",
            tab="pasting",
            run=_run_height_plane,
            params=(
                ParamSpec("tolerance", "位置合わせ許容誤差", "float", 0.1, unit="mm"),
            ),
            requires_pcb=True,
            uses_machine=True,
        )
    )
    catalog.register(
        JobDefinition(
            name="loading",
            label="Loading",
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
        )
    )
    catalog.register(
        JobDefinition(
            name="dispense_calibration",
            label="Dispense Calibration",
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
                # 線の共通設定（①②③ 共有・実行中変更可）
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
                    runtime_editable=True,
                ),
                ParamSpec(
                    "line_amount",
                    "1 線あたりの塗布量",
                    "float",
                    DISPENSE_CALIBRATION_DEFAULT_LINE_AMOUNT,
                    unit="uL",
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
                ParamSpec(
                    "removal_z_offset",
                    "計量退避 Z オフセット",
                    "float",
                    DISPENSE_CALIBRATION_DEFAULT_REMOVAL_Z_OFFSET,
                    unit="mm",
                    runtime_editable=True,
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
        )
    )
    catalog.register(
        JobDefinition(
            name="generate_rect_pcb",
            label="Generate Rect Pcb",
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
            label="Toolhead Offset",
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
            ),
            requires_pcb=True,
            uses_machine=True,
            accepts_commands=True,
        )
    )


# --- 共有ヘルパ ---


def _setup_calibration(
    ctx: JobContext, camera: Camera, tolerance: float
) -> BoardCalibrationResult:
    """Progress("セットアップ") → ボード計測セットアップの定型."""
    assert ctx.pcb_path is not None  # requires_pcb=True
    ctx.progress("セットアップ")
    return setup_board_calibration(
        machine=ctx.machine,
        pcb_file_path=ctx.pcb_path,
        tolerance=tolerance,
        camera=camera,
        frame_sink=ctx.frame,
    )


def _dispenser_rig(machine: Machine) -> tuple[Klipper, XYZStage, PasteApplicator]:
    """移動コマンド用 Klipper / ステージ / config 構成済み applicator の定型 3 点を作る."""
    klipper = create_command_klipper(machine)
    stage = XYZStage(klipper.readonly)
    dispenser = PasteDispenser(
        klipper=klipper.readonly,
        rotations_per_ul=machine.paste_dispenser.rotations_per_ul,
        air_pump_enabled=machine.paste_dispenser.air_pump_enabled,
    )
    applicator = PasteApplicator.from_config(
        klipper, dispenser, stage, machine.paste_dispenser
    )
    return klipper, stage, applicator


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
    drained = 0
    while ctx.next_command(timeout=0) is not None:
        drained += 1
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
            ctx.machine_name,
            ctx.source_pcb,
            ctx.machine.paste_dispenser,
            board_signature=hierarchy.signature(),
        )
    return PasteSettingsModel(
        base=base_override_from_config(ctx.machine.paste_dispenser),
        base_enabled=True,
    )


def _is_pad_enabled(
    pad: Pad, hierarchy: PadHierarchy, resolved: Mapping[PadRef, ResolvedPaste]
) -> bool:
    """Pad が塗布対象か判定する.

    階層から除外された pad（対応 Component 無し = ``resolved`` に不在）は
    後方互換で有効扱い、それ以外は解決済み ``enabled`` に従う。
    """
    try:
        pad_ref = hierarchy.pad_ref_for_pad(pad)
    except KeyError:
        return True
    r = resolved.get(pad_ref)
    return r is None or r.enabled


def _run_paste_solder(ctx: JobContext) -> JobResult:
    """ボード計測 → 銅箔照合 → 高さ計測 → 補正適用 → ペースト塗布を通しで実行する.

    塗布対象は基板ごとの pad 有効/無効 + 階層 override 設定で絞り込み、各 pad に
    解決済みの塗布設定を適用する。設定ファイル不在時は ``machine.toml`` デフォルトで
    全 pad 有効 = 現行等価で動く。
    """
    with ctx.open_camera() as camera:
        result = _setup_calibration(ctx, camera, float(ctx.params["tolerance"]))
        session = PasteSession.from_calibration(result)
        top_coppers = [c for c in session.pcb.copper if c.layer == Layer.TOP]
        top_pads = [p for p in session.pcb.pads if p.layer == Layer.TOP]

        # pad 階層 + 基板ごとの塗布設定（装置不要・前段で解決）
        hierarchy = build_pad_hierarchy(session.pcb.components, session.pcb.pads)
        model = _resolve_paste_model(ctx, hierarchy)
        resolved = resolve_pad_settings(hierarchy, model)

        # 有効 top pad のみ塗布対象にする（無効除外はここ一点）
        enabled_pads = [p for p in top_pads if _is_pad_enabled(p, hierarchy, resolved)]
        disabled_count = len(top_pads) - len(enabled_pads)
        ctx.log(
            f"塗布対象: 有効 {len(enabled_pads)} / 全 {len(top_pads)} pads"
            f"（無効 {disabled_count} 件スキップ）"
        )
        enabled_designators = {p.designator for p in enabled_pads}

        # 銅箔照合（部品単位）。有効 pad を 1 つ以上持つ部品のみ照合する。
        groups = [
            g
            for g in sorted_top_component_pads(result)
            if g.component.designator in enabled_designators
        ]
        ctx.log(f"照合対象の部品数: {len(groups)}")
        align_session = PadAlignmentSession.from_calibration(
            result, frame_sink=ctx.frame
        )
        aligned: list[tuple[ComponentPads, PadAlignmentResult]] = []
        for index, group in enumerate(groups):
            ctx.progress("銅箔照合", 100.0 * index / len(groups))
            ctx.checkpoint()
            designator = group.component.designator
            alignment = align_session.align(group)
            if alignment is None:
                ctx.log(f"警告: {designator} の照合に失敗")
                continue
            translation = alignment.translation
            ctx.log(
                f"{designator}: dx={translation.x:+.4f} dy={translation.y:+.4f} mm, "
                f"theta={alignment.rotation.degrees:+.3f} deg"
            )
            aligned.append((group, alignment))
        alignments = ComponentAlignments(
            board_transform=result.board_transform, results=tuple(aligned)
        )
        aligned_pads = sum(len(group.pads) for group, _ in aligned)
        ctx.log(
            f"位置合わせ成功: {len(aligned)}/{len(groups)} 部品（{aligned_pads} pads）"
        )

        # 高さ計測
        ctx.progress("高さ計測")
        height_plane = session.height_measurer.measure(
            coppers=top_coppers,
            board_to_machine=session.board_to_machine,
            outline=session.pcb.outline.polygon,
        )

        # 塗布順路（同種類連続・大面積優先）を決めてから補正を適用する
        routed_pads = [stop.pad for stop in plan_paste_route(enabled_pads)]

        # 補正適用（未照合 pad は無補正）→ 順路順の (polygon, ResolvedPaste) ペア
        pairs: list[tuple[Polygon, ResolvedPaste | None]] = []
        for pad in routed_pads:
            r = resolved.get(hierarchy.pad_ref_for_pad(pad))
            correction = alignments.board_correction(pad.designator)
            if correction is None:
                ctx.log(
                    f"警告: {pad.designator}.{pad.pad_number} は"
                    "未照合のため無補正で塗布します"
                )
                pairs.append((pad.polygon, r))
            else:
                pairs.append((transform_polygon(pad.polygon, correction), r))
        stage = session.stage

        # board→machine全変換 (board_transform + toolhead_offset + height_plane)
        transform = Compose(
            [session.board_transform, session.toolhead_offset, height_plane]
        )
        total = LoadingTotals()
        with session.make_applicator(transform=transform) as applicator:
            if ctx.params["interactive_loading"]:
                pos = stage.get_position()
                session.klipper.send_gcode(stage.move(x=0, y=0, z=0))
                total = _run_loading_loop(ctx, session.klipper, stage, applicator)
                session.klipper.send_gcode(
                    stage.move(x=pos.x, y=pos.y, z=pos.z) + gcode.wait_for_done()
                )

            ctx.progress("リトラクション")
            applicator.retract()

            # pad を 1 件ずつ apply して per-pad の進捗・設定・abort 境界を確保
            for index, (polygon, r) in enumerate(pairs):
                ctx.progress("塗布", 100.0 * index / len(pairs))
                ctx.checkpoint()
                if r is None:
                    applicator.apply([polygon])
                else:
                    applicator.apply(
                        [polygon],
                        paste_height=r.paste_height,
                        ul_per_mm2=r.ul_per_mm2,
                        dispense_mode=r.dispense_mode,
                        prime_extra_delay=r.prime_extra_delay,
                        bead_width_factor=r.bead_width_factor,
                        overlap=r.overlap,
                        boundary_margin=r.boundary_margin,
                    )

    return JobResult(
        summary=(
            f"照合成功 {len(aligned)}/{len(groups)} 部品 / "
            f"塗布 有効 {len(pairs)} / 全 {len(top_pads)} pads"
            f"（無効 {disabled_count} 件スキップ・"
            f"押出合計 {total.amount_ul:+.3f} uL）"
        )
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
        result = _setup_calibration(ctx, camera, float(ctx.params["tolerance"]))
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
            Artifact(
                "計測予定点",
                f"{ctx.artifacts_dir.name}/planned_points.png",
                "image",
            ),
            Artifact(
                "ヒートマップ",
                f"{ctx.artifacts_dir.name}/height_plane.png",
                "image",
            ),
        ),
    )


def _run_loading(ctx: JobContext) -> JobResult:
    """ペーストの command 駆動ローディングを実行する（カメラ・PCB 不要）."""
    klipper, stage, applicator = _dispenser_rig(ctx.machine)
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
    確定値はワーカーローカルに集約し、終了時に実施分のみ ``ApplyPayload`` に入れる。

    算出/判定はすべて pcbasm（``FlowCalibrationSet`` / ``DispenseRateCalibration`` /
    ``FillSpeedSweep`` / ``LineLayout``）に委譲し、ここはループ制御と入出力に徹する。
    """
    board_width = float(ctx.params["board_width"])
    board_height = float(ctx.params["board_height"])
    tolerance = float(ctx.params["tolerance"])

    with ctx.open_camera() as camera:
        ctx.progress("セットアップ")
        pcb_path = _generate_calibration_board(ctx, board_width, board_height)
        result = setup_board_calibration(
            machine=ctx.machine,
            pcb_file_path=pcb_path,
            tolerance=tolerance,
            camera=camera,
            frame_sink=ctx.frame,
        )
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
    while ctx.next_command(timeout=0) is not None:
        pass
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
        case _:
            klipper = calib.session.klipper
            stage = calib.session.stage
            if not handle_machine_command(ctx, klipper, stage, command, focus_z=None):
                ctx.log(f"未知のコマンドです: {command.get('type')!r}")


def _line_layout(ctx: JobContext) -> LineLayout:
    """① / 計測線の段ずらしレイアウトをパラメータから構築する."""
    return LineLayout(
        line_length=float(ctx.params["line_length"]),
        line_count=max(1, int(ctx.params["line_count"])),
        row_pitch=float(ctx.params["row_pitch"]),
        origin=Point2d(
            DISPENSE_CALIBRATION_LAYOUT_MARGIN, DISPENSE_CALIBRATION_LAYOUT_MARGIN
        ),
    )


def _removal_z(ctx: JobContext, calib: _CalibrationContext) -> float:
    """計量で基板を取り出すときの退避 Z = max(z_min, z_max - offset).

    既定 offset=0 で z_max（フルリトラクト）。``removal_z_offset`` は実行中変更可で、
    純粋に ``ctx.params`` を読むため呼び出しごとに最新値を反映する。
    """
    z = calib.session.stage.limits.z
    return max(z.min, z.max - float(ctx.params["removal_z_offset"]))


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
    連動更新する。収束（前後の相対差が許容内）はヒントとして表示するのみで、ループ継続は
    ユーザー判断。タール前の中止・質量入力の中止はいずれもメニューへ戻る。
    """
    # 比重はマシン設定 (solder_paste_density [mg/uL]。水基準なので比重と数値が一致) を
    # 真実とする。② が密度を machine から直接読むのと同じ扱い。
    specific_gravity = ctx.machine.paste_dispenser.solder_paste_density
    ctx.log(
        f"ペースト比重（machine.toml の solder_paste_density）= {specific_gravity:.3f}"
    )

    rotations_per_ul = (
        results.rotations_per_ul
        if results.rotations_per_ul is not None
        else calib.rotations_per_ul
    )
    dispense_accel = (
        results.dispense_accel
        if results.dispense_accel is not None
        else calib.dispense_accel
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

        # ── 電子天秤にセットしてタール（ゼロ）──（中止でメニューへ戻る）
        _prompt_confirm(
            ctx,
            "塗布対象を電子天秤にセットし、タール（ゼロ）してください。"
            "準備ができたら線引きへ進みます。",
        )

        ctx.progress("① rotations_per_ul: 線引き")
        previous_rpu = calib.rotations_per_ul
        # プライム済みのペーストを baseline まで引き戻してから引き始める
        # （各 draw_line の FillSequence が prime→吐出→retract を内包するので、
        #   線間・線後の追加 retract は不要）
        calib.applicator.retract()
        for index in range(layout.line_count):
            ctx.checkpoint()
            start, end = layout.line(index)
            calib.applicator.draw_line(start, end, amount=amount)
            ctx.log(f"線 {index + 1}/{layout.line_count} を {amount:.3f} uL で塗布")

        # 線引き後はヘッドを退避 Z（= max(z_min, z_max - offset)）へ上げ、基板を取り出して計量しやすくする。
        removal_z = _removal_z(ctx, calib)
        ctx.log(
            f"ヘッドを退避 Z={removal_z:.3f} へ移動します。基板を取り出して計測してください"
        )
        calib.session.klipper.send_gcode(
            calib.session.stage.move(z=removal_z) + gcode.wait_for_done()
        )

        rotations_used = layout.line_count * amount * previous_rpu
        mass = _prompt_mass(
            ctx,
            f"{layout.line_count} 本の線の合計質量 (mg) を入力"
            f"（回転数 {rotations_used:.4f} rev 相当）",
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

        # 残る 3 つはいずれも算出値を採用する。
        rotations_per_ul = computed_rpu
        dispense_accel = computed_accel
        if choice == "採用して吐出量キャリブレーションを終了する":
            # 再描画しないので applicator の作り直しは不要。finish でジョブを終了。
            return attrs.evolve(
                results,
                rotations_per_ul=rotations_per_ul,
                dispense_accel=dispense_accel,
                finish=True,
            )
        # 「再計測」「他のキャリブへ進む」は新値で applicator を作り直す。
        calib.rebuild_applicator(
            rotations_per_ul=computed_rpu, dispense_accel=computed_accel
        )
        ctx.log("新 rotations_per_ul で applicator を再構成しました")
        if choice == "採用して再計測する":
            continue
        break  # 採用して他のキャリブレーションへ進む

    return attrs.evolve(
        results,
        rotations_per_ul=rotations_per_ul,
        dispense_accel=dispense_accel,
    )


def _calibrate_max_dispense_rate(
    ctx: JobContext,
    calib: _CalibrationContext,
    results: _DispenseCalibrationResults,
) -> _DispenseCalibrationResults:
    """② max_dispense_rate を吐出効率の落ち検出で確定する.

    レート列の各点で同一 ``line_amount`` の線を引いて計量し、効率
    ``measured_ul / commanded_ul`` の落ちから ``DispenseRateCalibration`` が
    ``max_dispense_rate`` を判定する。各点は独立計測のため、線引き後に退避 Z
    （z_max − removal_z_offset）へ上げ、タール確認 → 質量入力の順で進める。タール／
    質量入力の中止はいずれもメニューへ戻る。自動判定値を default に手動上書き可能。
    """
    layout = _line_layout(ctx)
    amount = float(ctx.params["line_amount"])
    rate_min = float(ctx.params["rate_min"])
    rate_max = float(ctx.params["rate_max"])
    divisions = max(1, int(ctx.params["rate_divisions"]))
    density = ctx.machine.paste_dispenser.solder_paste_density

    rates = dispense_rate_schedule(rate_min, rate_max, divisions)
    if not rates:
        ctx.log(
            "吐出レート列が生成できません（rate_min / rate_max / divisions を確認）。"
            "② をスキップします"
        )
        return results

    measurements: list[RateMeasurement] = []
    # baseline まで引き戻してから引き始める（draw_line が retract を内包するので線間は不要）
    calib.applicator.retract()
    for index, rate in enumerate(rates):
        ctx.progress("② max_dispense_rate: 線引き", 100.0 * index / len(rates))
        ctx.checkpoint()
        # 段ずらしで引く。本数が足りなければ折り返して再利用する。
        start, end = layout.line(index % layout.line_count)
        calib.applicator.draw_line(start, end, amount=amount, rate_cap=rate)
        # 計量のため基板を取り出せるよう、線引き後に退避 Z（z_max - offset）へ上げる。
        removal_z = _removal_z(ctx, calib)
        ctx.log(
            f"ヘッドを退避 Z={removal_z:.3f} へ移動します。基板を取り出して計測してください"
        )
        calib.session.klipper.send_gcode(
            calib.session.stage.move(z=removal_z) + gcode.wait_for_done()
        )
        # 各レートは独立計測。質量入力前に必ず電子天秤のタールを確認する（中止でメニューへ）。
        _prompt_confirm(
            ctx,
            f"[{index + 1}/{len(rates)}] 電子天秤をタール（ゼロ）してから"
            "基板を載せ、続行を押してください",
        )
        mass = _prompt_mass(
            ctx,
            f"[{index + 1}/{len(rates)}] レート {rate:.3f} uL/s の線の質量 (mg) を入力",
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
    layout = _line_layout(ctx)
    speed_min = float(ctx.params["speed_min"])
    speed_max = float(ctx.params["speed_max"])
    divisions = max(1, int(ctx.params["speed_divisions"]))
    ul_per_mm2 = ctx.machine.paste_dispenser.ul_per_mm2
    bead_width = (
        ctx.machine.paste_dispenser.nozzle_diameter
        * ctx.machine.paste_dispenser.bead_width_factor
    )
    # 実塗布同等の総量。q = total_amount / line_length（実効単位長さ量）は
    # FillSequence 側が rate から逆算するため、ここでは move 速度を直接渡す。
    total_amount = ul_per_mm2 * slot_area(layout.line_length, bead_width)

    speeds = fill_speed_schedule(speed_min, speed_max, divisions)
    if not speeds:
        ctx.log(
            "塗布速度列が生成できません（speed_min / speed_max / divisions を確認）。"
            "③ をスキップします"
        )
        return results

    sweep = FillSpeedSweep(speeds=tuple(speeds))
    # baseline まで引き戻してから引き始める（draw_line が retract を内包するので線間は不要）
    calib.applicator.retract()
    for index, speed in enumerate(speeds):
        ctx.progress("③ max_fill_speed: 線引き", 100.0 * index / len(speeds))
        ctx.checkpoint()
        start, end = layout.line(index % layout.line_count)
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
    ctx.log(f"max_fill_speed = {chosen:.2f} mm/s を採用しました")
    return attrs.evolve(results, max_fill_speed=chosen)


def _dispense_calibration_result(
    results: _DispenseCalibrationResults,
) -> JobResult:
    """実施したキャリブの確定値から summary と ApplyPayload を組む."""
    values: dict[str, float] = {}
    summary_parts: list[str] = []
    for field in (
        "rotations_per_ul",
        "dispense_accel",
        "max_dispense_rate",
        "max_fill_speed",
    ):
        value = getattr(results, field)
        if value is None:
            continue
        values[f"paste_dispenser.{field}"] = round(value, APPLY_DIGITS)
        summary_parts.append(f"{field} = {value:.6f}")

    if not values:
        return JobResult(summary="キャリブレーションを実施せず終了しました")

    summary = "キャリブレーション結果: " + " / ".join(summary_parts)
    return JobResult(
        summary=summary,
        apply=ApplyPayload(
            label=f"[paste_dispenser] {' / '.join(summary_parts)} を設定に反映",
            values=values,
        ),
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
        artifacts=(
            Artifact(
                "キャリブレーション矩形 PCB",
                f"{ctx.artifacts_dir.name}/{filename}",
                "file",
            ),
        ),
    )


def _run_toolhead_offset(ctx: JobContext) -> JobResult:
    """ペースト吐出と円検出からカメラ-ツールヘッド間 XY オフセットを計測する."""
    tolerance = float(ctx.params["tolerance"])
    lift_height = float(ctx.params["lift_height"])
    diameter_min = float(ctx.params["paste_diameter_min"])
    diameter_max = float(ctx.params["paste_diameter_max"])

    with ctx.open_camera() as camera:
        result = _setup_calibration(ctx, camera, tolerance)
        machine = result.machine
        klipper = result.klipper
        stage = result.stage
        outline = result.pcb.outline
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
            air_pump_enabled=dispenser_config.air_pump_enabled,
        )

        # ボード中央へツールヘッド移動 & プローブ
        ctx.progress("プローブ")
        board_center = Point2d(outline.width / 2, outline.height / 2)
        center_camera = result.board_transform.apply(board_center)
        center_toolhead = dispenser_config.toolhead.to_transform().apply(center_camera)
        klipper.send_gcode(
            stage.move(x=center_toolhead.x, y=center_toolhead.y) + gcode.wait_for_done()
        )
        board_surface_z = probe_executor.probe()
        ctx.log(f"Board surface Z: {board_surface_z:.4f} mm")

        with PasteApplicator.from_config(
            klipper,
            paste_dispenser,
            stage,
            dispenser_config,
            transform=Identity(),
            lift_height=lift_height,
        ) as applicator:
            # ペーストロード（command 駆動）
            klipper.send_gcode(stage.move(z=0.0) + gcode.wait_for_done())
            _run_loading_loop(
                ctx, klipper, stage, applicator, focus_z=calibration.z_position
            )
            applicator.retract()

            # ペースト吐出
            ctx.progress("吐出")
            dispense_amount = float(ctx.params["dispense_amount"])
            paste_height = _dot_dispense_height(
                dispenser_config.paste_height,
                amount=dispense_amount,
                nozzle_diameter=dispenser_config.nozzle_diameter,
            )
            dispense_z = board_surface_z + paste_height
            klipper.send_gcode(
                stage.move(x=center_toolhead.x, y=center_toolhead.y, z=dispense_z)
                + gcode.wait_for_done()
            )
            klipper.send_gcode(
                paste_dispenser.pushpull(
                    dispense_amount,
                    dispenser_config.max_dispense_rate,
                    dispenser_config.dispense_accel,
                )
                + gcode.wait_for_done()
            )
            retract_accel = (
                dispenser_config.dispense_accel * dispenser_config.retract_accel_factor
            )
            klipper.send_gcode(
                paste_dispenser.pushpull(
                    -dispenser_config.retract_amount,
                    dispenser_config.effective_retract_rate,
                    retract_accel,
                )
                + gcode.wait_for_done()
            )
            klipper.send_gcode(
                stage.move(z=dispense_z + lift_height) + gcode.wait_for_done()
            )
            ctx.log(
                f"吐出位置 (ステージ): "
                f"({center_toolhead.x:.3f}, {center_toolhead.y:.3f})"
            )

            # ペースト検出 & 位置合わせ
            ctx.progress("ペースト検出")
            klipper.send_gcode(
                stage.move(
                    x=center_camera.x,
                    y=center_camera.y,
                    z=calibration.z_position,
                )
                + gcode.wait_for_done()
            )
            time.sleep(1.0)
            paste_detector = CircleDetector(
                pixel_per_mm=calibration.pixel_per_mm,
                target_diameter_mm=(diameter_min + diameter_max) / 2,
                crop_size=machine.camera.crop.size,
                diameter_tolerance_mm=(diameter_max - diameter_min) / 2,
            )
            paste_observer = OffsetObserver(
                detector=paste_detector,
                camera=result.camera,
                crop_size=machine.camera.crop.size,
                frame_sink=ctx.frame,
            )
            paste_adjustor = XYPositionAdjustor(
                observe=paste_observer.observe,
                klipper=klipper,
                stage=stage,
                offset_transform=result.offset_transform,
                tolerance=tolerance,
            )
            camera_final_pos = paste_adjustor.adjust()
            ctx.log(
                f"カメラ最終位置: ({camera_final_pos.x:.3f}, {camera_final_pos.y:.3f})"
            )

    # オフセット算出 & 保存
    measured_offset = Point2d(
        center_toolhead.x - camera_final_pos.x,
        center_toolhead.y - camera_final_pos.y,
    )
    offset_result = ToolheadOffsetResult(
        offset=measured_offset,
        dispense_position=center_toolhead,
        camera_position=camera_final_pos,
        tolerance=tolerance,
        calibrated_at=datetime.now(),
    )
    offset_result.save(ctx.artifacts_dir / "toolhead_offset.json")

    current_toolhead = machine.paste_dispenser.toolhead
    diff_x = measured_offset.x - current_toolhead.x
    diff_y = measured_offset.y - current_toolhead.y
    return JobResult(
        summary=(
            f"オフセット X={measured_offset.x:+.4f} Y={measured_offset.y:+.4f} mm"
            f"（現在設定との差 dX={diff_x:+.4f} dY={diff_y:+.4f}）"
        ),
        artifacts=(
            Artifact(
                "計測結果 JSON",
                f"{ctx.artifacts_dir.name}/toolhead_offset.json",
                "file",
            ),
        ),
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


def _dot_dispense_height(
    paste_height: float | str, *, amount: float, nozzle_diameter: float
) -> float:
    if paste_height != "auto":
        return float(paste_height)
    nozzle_area = math.pi * (nozzle_diameter / 2.0) ** 2
    return amount / nozzle_area
