"""Pasting タブのジョブ定義（塗布 / 高さ計測 / ローディング / キャリブレーション / 塗布パスシミュレート）."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime
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
    sample_points_in_polygons,
    sampling_diagnostics,
    sort_by_nearest,
    transform_polygon,
)
from pcbasm.hal import (
    Camera,
    Klipper,
    PasteDispenser,
    ProbeGround,
    ServoGroundProbe,
    XYZStage,
)
from pcbasm.pasting import (
    FlowCalibration,
    PasteApplicator,
    PasteSettingsModel,
    ProbeExecutor,
    ResolvedPaste,
    ToolheadOffsetResult,
    base_override_from_config,
    resolve_pad_settings,
)
from pcbasm.pasting.fill_path import build_paste_fill_path
from pcbasm.pcb import Layer, Pad, PadList, PcbFile, build_pad_hierarchy
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
    render_fill_paths,
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


@attrs.frozen
class Extrude:
    """ローディング中の押出/吸引 1 回分.

    Attributes:
        amount: 符号付き押出量 [uL]（吸引は負）
    """

    amount: float


@attrs.frozen
class Finish:
    """ローディング終了."""


type LoadingAction = Extrude | Finish


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
        case {"type": "finish"}:
            return Finish()
    return None


def _positive_amount(value: object) -> float | None:
    """正の数値なら float、それ以外は None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def register_pasting_jobs(catalog: JobCatalog) -> None:
    """Pasting タブの 7 ジョブを登録する."""
    catalog.register(
        JobDefinition(
            name="paste_solder",
            label="Paste Solder",
            tab="pasting",
            run=_run_paste_solder,
            params=(
                ParamSpec("tolerance", "位置合わせ許容誤差", "float", 0.1, unit="mm"),
                ParamSpec("amount", "ローディング既定量", "float", 0.1, unit="uL"),
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
                ParamSpec("amount", "ローディング既定量", "float", 0.1, unit="uL"),
            ),
            uses_machine=True,
            accepts_commands=True,
        )
    )
    catalog.register(
        JobDefinition(
            name="flow_calibration",
            label="Flow Calibration",
            tab="pasting",
            run=_run_flow_calibration,
            params=(
                ParamSpec("rotations", "回転数", "float", 30, unit="rev"),
                ParamSpec("rate", "角速度", "float", 5.0, unit="rev/s"),
                ParamSpec("accel", "角加速度", "float", 10.0, unit="rev/s^2"),
                ParamSpec("load_amount", "ローディング既定量", "float", 0.1, unit="uL"),
            ),
            uses_machine=True,
            accepts_commands=True,
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
                ParamSpec("dispense_amount", "吐出量", "float", 0.1, unit="uL"),
                ParamSpec(
                    "loading_amount", "ローディング既定量", "float", 0.1, unit="uL"
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
    catalog.register(
        JobDefinition(
            name="probe_gnd_down_adjust",
            label="Probe Gnd Down Adjust",
            tab="pasting",
            run=_run_probe_gnd_down_adjust,
            uses_machine=True,
        )
    )
    catalog.register(
        JobDefinition(
            name="fill_path_simulate",
            label="Fill Path Simulate",
            tab="pasting",
            run=_run_fill_path_simulate,
            params=(
                ParamSpec("nozzle_diameter", "ノズル内径", "float", 0.4, unit="mm"),
                ParamSpec("layer", "レイヤ", "choice", "top", ("top", "bottom")),
                ParamSpec(
                    "bead_width_factor",
                    "ビード幅係数",
                    "float",
                    1.0,
                    help="w = nozzle * factor",
                ),
                ParamSpec(
                    "overlap",
                    "行間オーバーラップ",
                    "float",
                    0.0,
                    help="ジグザグ行間 [0,1)",
                ),
                ParamSpec("boundary_margin", "外周マージン", "float", 0.0, unit="mm"),
            ),
            requires_pcb=True,
            uses_machine=False,
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
) -> float:
    """ローディング段階の command 駆動ループを実行し、押出合計 [uL] を返す.

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
    total = 0.0
    while True:
        command = ctx.next_command(timeout=None)
        assert command is not None  # timeout=None は取得（or abort）までブロック
        action = parse_loading_command(command)
        match action:
            case Extrude(amount=amount):
                applicator.load(amount)
                total += amount
                ctx.log(f"ローディング: {amount:+.3f} uL（累計 {total:+.3f} uL）")
            case Finish():
                ctx.log(f"ローディング終了（押出合計 {total:+.3f} uL）")
                return total
            case None:
                if not handle_machine_command(
                    ctx, klipper, stage, command, focus_z=focus_z
                ):
                    ctx.log(f"未知のコマンドです: {command.get('type')!r}")


def _prompt_positive_number(
    ctx: JobContext, message: str, default: float | None = None
) -> float:
    """正数が入力されるまで number プロンプトを繰り返す."""
    while True:
        answer = ctx.prompt(PromptSpec(kind="number", message=message, default=default))
        assert isinstance(answer, float)
        if answer > 0:
            return answer
        ctx.log(f"正の数値を入力してください（与えられた値: {answer}）")


# --- ジョブ実装 ---


def _resolve_paste_model(ctx: JobContext) -> PasteSettingsModel:
    """基板設定ストア（あれば）から塗布設定モデルを取得する.

    ストア／PCB が未配線なら ``machine.toml`` の ``[paste_dispenser]`` を
    L0 デフォルトに据えた全 pad 有効のモデルを返す（= 現行等価のフォールバック）。
    """
    if ctx.board_store is not None and ctx.source_pcb is not None:
        return ctx.board_store.load_or_init(
            ctx.machine_name, ctx.source_pcb, ctx.machine.paste_dispenser
        )
    return PasteSettingsModel(
        base=base_override_from_config(ctx.machine.paste_dispenser),
        base_enabled=True,
    )


def _is_pad_enabled(
    pad: Pad, resolved: Mapping[tuple[str, str], ResolvedPaste]
) -> bool:
    """Pad が塗布対象か判定する.

    階層から除外された pad（対応 Component 無し = ``resolved`` に不在）は
    後方互換で有効扱い、それ以外は解決済み ``enabled`` に従う。
    """
    r = resolved.get((pad.designator, pad.pad_number))
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
        model = _resolve_paste_model(ctx)
        resolved = resolve_pad_settings(hierarchy, model)

        # 有効 top pad のみ塗布対象にする（無効除外はここ一点）
        enabled_pads = [p for p in top_pads if _is_pad_enabled(p, resolved)]
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

        # 補正適用（未照合 pad は無補正）→ (polygon, ResolvedPaste) ペアで保持
        pairs: list[tuple[Polygon, ResolvedPaste | None]] = []
        for pad in enabled_pads:
            r = resolved.get((pad.designator, pad.pad_number))
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
        sorted_pairs = sort_by_nearest(
            pairs,
            stage.get_position().to2d().to3d(),
            key=lambda pair: Point2d(x=pair[0].centroid.x, y=pair[0].centroid.y).to3d(),
        )

        # board→machine全変換 (board_transform + toolhead_offset + height_plane)
        transform = Compose(
            [session.board_transform, session.toolhead_offset, height_plane]
        )
        total = 0.0
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
            for index, (polygon, r) in enumerate(sorted_pairs):
                ctx.progress("塗布", 100.0 * index / len(sorted_pairs))
                ctx.checkpoint()
                if r is None:
                    applicator.apply([polygon])
                else:
                    applicator.apply(
                        [polygon],
                        fill_speed=r.fill_speed,
                        paste_height=r.paste_height,
                        ul_per_mm2=r.ul_per_mm2,
                        prime_extra_delay=r.prime_extra_delay,
                        bead_width_factor=r.bead_width_factor,
                        overlap=r.overlap,
                        boundary_margin=r.boundary_margin,
                    )

    return JobResult(
        summary=(
            f"照合成功 {len(aligned)}/{len(groups)} 部品 / "
            f"塗布 有効 {len(sorted_pairs)} / 全 {len(top_pads)} pads"
            f"（無効 {disabled_count} 件スキップ・押出合計 {total:+.3f} uL）"
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
            f"Planned probe points: {ctx.pcb_path.stem} "
            f"({len(planned_points)} points)"
        ),
        output_path=planned_path,
    )
    preview = cv2.imread(str(planned_path))
    if preview is not None:
        ctx.frame(Image(preview))
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

    proceed = ctx.prompt(
        PromptSpec(
            kind="confirm",
            message=f"{len(planned_points)} 点を計測します。続行しますか?",
            default=True,
        )
    )
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
    return JobResult(summary=f"押出合計 {total:+.3f} uL")


def _run_flow_calibration(ctx: JobContext) -> JobResult:
    """N 回転の実押出から rotations_per_ul を算出し、設定反映候補にする."""
    rotations = float(ctx.params["rotations"])
    klipper, stage, applicator = _dispenser_rig(ctx.machine)
    with applicator:
        _run_loading_loop(ctx, klipper, stage, applicator)

        proceed = ctx.prompt(
            PromptSpec(
                kind="confirm",
                message="はかりにキャッチ皿を置き、タール (0g) にしましたか?",
                default=True,
            )
        )
        if not proceed:
            raise JobAborted()

        ctx.progress("キャリブレーション回転")
        applicator.calibrate(
            rotations, float(ctx.params["rate"]), float(ctx.params["accel"])
        )
        mass = _prompt_positive_number(
            ctx, "ペーストが安定したら計測した質量 (mg) を入力"
        )
        applicator.retract()
        sg = _prompt_positive_number(ctx, "ペーストの比重（水比重, データシート値）")

    result = FlowCalibration(rotations=rotations, mass_mg=mass, specific_gravity=sg)
    return JobResult(
        summary=f"rotations_per_ul = {result.rotations_per_ul:.6f}",
        apply=ApplyPayload(
            label=(
                f"[paste_dispenser] rotations_per_ul = "
                f"{result.rotations_per_ul:.6f} を設定に反映"
            ),
            values={
                "paste_dispenser.rotations_per_ul": round(result.rotations_per_ul, 6)
            },
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
        probe = ServoGroundProbe(
            klipper.readonly,
            servo_name=probe_config.servo_name,
            revolution_distance=probe_config.revolution_distance,
            down_distance=probe_config.down_distance,
        )
        probe_executor = ProbeExecutor(klipper=klipper, probe=probe, stage=stage)
        dispenser_config = machine.paste_dispenser
        paste_dispenser = PasteDispenser(
            klipper=klipper.readonly,
            rotations_per_ul=dispenser_config.rotations_per_ul,
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
            dispense_z = board_surface_z + dispenser_config.paste_height
            klipper.send_gcode(
                stage.move(x=center_toolhead.x, y=center_toolhead.y, z=dispense_z)
                + gcode.wait_for_done()
            )
            klipper.send_gcode(
                paste_dispenser.pushpull(
                    float(ctx.params["dispense_amount"]),
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
                    dispenser_config.retract_rate,
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


def _run_probe_gnd_down_adjust(ctx: JobContext) -> JobResult:
    """グラウンドピンのダウン距離を対話的に調整し、設定反映候補にする.

    終了時（abort / 失敗時を含む）はダウン距離 0 へ best-effort で戻す。
    """
    machine = ctx.machine
    probe_config = machine.probe
    klipper = create_command_klipper(machine)

    # ProbeGround は構築時に Klipper の config を読む（= 接続する）ため、
    # 最初の prompt より後で初回送信時に遅延構築する
    ground: ProbeGround | None = None

    def make_ground() -> ProbeGround:
        nonlocal ground
        if ground is None:
            ground = ProbeGround(
                klipper.readonly,
                probe_config.servo_name,
                probe_config.revolution_distance,
            )
        return ground

    distance = probe_config.down_distance
    try:
        ctx.progress("距離調整")
        while True:
            answer = ctx.prompt(
                PromptSpec(
                    kind="number",
                    message="ダウン距離 [mm] を入力",
                    default=distance,
                )
            )
            assert isinstance(answer, float)
            if answer < 0:
                ctx.log(f"0 以上の数値を入力してください（与えられた値: {answer}）")
                continue
            distance = answer
            klipper.send_gcode(make_ground().down(distance) + gcode.wait_for_done())
            ctx.log(f"ダウン: {distance:.3f} mm")
            confirmed = ctx.prompt(
                PromptSpec(
                    kind="confirm",
                    message=(
                        f"down_distance = {distance:.3f} mm で確定しますか?"
                        "（いいえで再調整）"
                    ),
                    default=False,
                )
            )
            if confirmed:
                break
    finally:
        # 終了時は必ずダウン距離 0 へ戻す（送信失敗は log のみ）
        try:
            klipper.send_gcode(make_ground().down(0.0) + gcode.wait_for_done())
            ctx.log("ダウン距離 0 へ戻しました")
        except Exception as exc:
            ctx.log(f"ダウン距離 0 への復帰に失敗しました: {exc}")

    return JobResult(
        summary=f"down_distance: {distance:.3f} mm",
        apply=ApplyPayload(
            label=f"[probe] down_distance = {distance:.3f} を設定に反映",
            values={"probe.down_distance": round(distance, 3)},
        ),
    )


def _run_fill_path_simulate(ctx: JobContext) -> JobResult:
    """Paste pad ごとの fill path を生成し可視化 PNG を描く（装置・カメラ不要）."""
    assert ctx.pcb_path is not None  # requires_pcb=True
    nozzle_diameter = float(ctx.params["nozzle_diameter"])
    bead_width_factor = float(ctx.params["bead_width_factor"])
    overlap = float(ctx.params["overlap"])
    boundary_margin = float(ctx.params["boundary_margin"])
    layer = Layer.TOP if ctx.params["layer"] == "top" else Layer.BOTTOM

    ctx.progress("読込")
    ctx.log(f"PCBファイルを読み込み中: {ctx.pcb_path}")
    pcb = PcbFile(ctx.pcb_path)
    pads = PadList(pad for pad in pcb.pads if pad.layer == layer)
    ctx.log(f"{layer.value} レイヤの paste pad 数: {len(pads)}")

    paths = []
    for index, pad in enumerate(pads):
        ctx.checkpoint()
        ctx.progress("fill path 生成", 100.0 * index / len(pads))
        paths.append(
            build_paste_fill_path(
                pad.polygon,
                nozzle_diameter,
                bead_width_factor=bead_width_factor,
                overlap=overlap,
                boundary_margin=boundary_margin,
            )
        )
    non_empty = sum(1 for components in paths if components)
    summary = f"fill path 生成: {non_empty} / {len(paths)} 成功"
    ctx.log(summary)

    ctx.progress("描画")
    png_name = f"{ctx.pcb_path.stem}_fill_path.png"
    render_fill_paths(
        outline=pcb.outline,
        pads=pads,
        paths=paths,
        nozzle_diameter=nozzle_diameter,
        bead_width_factor=bead_width_factor,
        overlap=overlap,
        boundary_margin=boundary_margin,
        layer=layer,
        output_path=ctx.artifacts_dir / png_name,
    )
    ctx.log(f"画像 -> {png_name}")
    ctx.progress("完了", 100.0)

    return JobResult(
        summary=summary,
        artifacts=(
            Artifact(
                "Fill Path 可視化", f"{ctx.artifacts_dir.name}/{png_name}", "image"
            ),
        ),
    )
