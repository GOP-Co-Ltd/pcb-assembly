"""Posctrl タブのジョブ定義（基準点設定 / カメラキャリブレーション / 巡回系 / グリッド PCB 生成）."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

import attrs
import cv2

from pcbasm import gcode
from pcbasm.config import Machine
from pcbasm.geometry import Compose, Point2d, Point3d, sort_by_nearest
from pcbasm.hal import Camera, Klipper, Speed, XYZStage
from pcbasm.pcb import Layer, Pad
from pcbasm.posctrl import (
    BoardCalibrationResult,
    CheckerboardScanner,
    ComponentPads,
    CopperProjector,
    OrthogonalityMetrics,
    PadAlignmentResult,
    PadAlignmentSession,
    PadResultRenderer,
    ScanFailure,
    ScanOutcome,
    ScanProgress,
    render_label,
    sorted_top_component_pads,
)
from pcbasm.vision import (
    MINIMUM_SCAN_VIEWS,
    CalibrationResult,
    CheckerboardDetector,
    CheckerboardView,
    CircleDetector,
    Image,
    IntrinsicsCalibrator,
    ScanGrid,
    draw_detected_circle,
    draw_overlay,
    draw_scan_coverage,
    measure_pixel_per_mm,
    undistort_views,
)
from pcbasm.visualization import render_scan_residuals
from webui.jobs.board_ops import (
    align_component_groups,
    confirm_next_point,
    setup_board,
)
from webui.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from webui.jobs.context import (
    ApplyFile,
    ApplyPayload,
    Artifact,
    JobAborted,
    JobContext,
    JobResult,
    PromptSpec,
)
from webui.jobs.machine_commands import create_command_klipper, handle_machine_command

# camera_calibration の corner_coverage.png に重ねる関心領域の候補サイズ [px]
CROP_CANDIDATES_PX = ((300, 300), (600, 600))
# 巡回先 1 点あたりのフレーム配信時間 [sec]
RESULT_DISPLAY_SEC = 1.0
# reference_point_setup の現在位置キャッシュ TTL [sec]
POSITION_CACHE_SEC = 0.5

_TEXT_COLOR = (0, 255, 255)  # 現在位置テキストの色 (BGR: 黄)


def register_posctrl_jobs(catalog: JobCatalog) -> None:
    """Posctrl タブの 5 ジョブを登録する."""
    catalog.register(
        JobDefinition(
            name="reference_point_setup",
            label="基準点設定",
            tab="posctrl",
            run=_run_reference_point_setup,
            uses_machine=True,
            accepts_commands=True,
        )
    )
    catalog.register(
        JobDefinition(
            name="camera_calibration",
            label="カメラキャリブレーション",
            tab="posctrl",
            run=_run_camera_calibration,
            params=(
                ParamSpec(
                    "square_size", "チェッカーボードの1マス", "float", 1.5, unit="mm"
                ),
                ParamSpec(
                    "residual_limit",
                    "残差の上限",
                    "float",
                    30.0,
                    unit="um",
                    minimum=0.0,
                    help="補正後の残差 RMS がこれを超えたら失敗扱い",
                ),
            ),
            persisted_params=("square_size", "residual_limit"),
            uses_machine=True,
        )
    )
    catalog.register(
        JobDefinition(
            name="board_tour",
            label="ボード巡回",
            tab="posctrl",
            run=_run_board_tour,
            params=(
                ParamSpec("tolerance", "位置合わせ許容誤差", "float", 0.1, unit="mm"),
            ),
            requires_pcb=True,
            uses_machine=True,
        )
    )
    catalog.register(
        JobDefinition(
            name="orthogonality_test",
            label="直行性テスト",
            tab="posctrl",
            run=_run_orthogonality_test,
            params=(
                ParamSpec("tolerance", "位置合わせ許容誤差", "float", 0.1, unit="mm"),
            ),
            requires_pcb=True,
            uses_machine=True,
        )
    )
    catalog.register(
        JobDefinition(
            name="generate_grid_pcb",
            label="グリッド PCB 生成",
            tab="posctrl",
            run=_run_generate_grid_pcb,
            params=(
                ParamSpec("size", "基板の一辺", "float", 40.0, unit="mm"),
                ParamSpec(
                    "divisions", "グリッド分割数", "int", 3, help="パッド数 = n^2"
                ),
                ParamSpec("pad_size", "パッドの一辺", "float", 0.5, unit="mm"),
            ),
            uses_machine=False,
        )
    )


class _CachedPosition:
    """stage.get_position() の短期キャッシュ（毎フレームの往復を回避）."""

    def __init__(self, stage: XYZStage, ttl: float = POSITION_CACHE_SEC) -> None:
        self._stage = stage
        self._ttl = ttl
        self._cached: Point3d | None = None
        self._expires = 0.0

    def get(self) -> Point3d:
        now = time.monotonic()
        if self._cached is None or now >= self._expires:
            self._cached = self._stage.get_position()
            self._expires = now + self._ttl
        return self._cached

    def invalidate(self) -> None:
        self._cached = None


def _run_reference_point_setup(ctx: JobContext) -> JobResult:
    """基準点 (top left) をジョグで合わせ、現在位置を設定へ保存する.

    マシン操作パネル（ジョブモード）の WS command でジョグし、record で 現在位置を確定・即時反映、quit で中止する。
    """
    machine = ctx.machine
    klipper = create_command_klipper(machine)
    stage = XYZStage(klipper.readonly)
    cam_config = machine.camera
    try:
        calibration = CalibrationResult.load(cam_config.calibration_file)
    except Exception as exc:
        raise RuntimeError(f"カメラキャリブレーションが読み込めません: {exc}") from exc
    detector = CircleDetector(
        pixel_per_mm=calibration.pixel_per_mm,
        target_diameter_mm=machine.reference_point.target_diameter,
        crop_size=cam_config.crop.size,
        diameter_tolerance_mm=0.5,
    )

    ctx.progress("ホーミング")
    klipper.send_gcode(gcode.homing(x=True, y=True, z=True) + gcode.wait_for_done())
    if calibration.z_position is not None:
        ctx.log(f"カメラ Z 高さへ移動: {calibration.z_position:.3f} mm")
        klipper.send_gcode(stage.move(z=calibration.z_position) + gcode.wait_for_done())
    else:
        ctx.log("キャリブレーションに Z 位置がありません。Z は移動しません")

    position = _CachedPosition(stage)
    with ctx.open_camera() as camera:
        ctx.progress("ジョグ待機")
        ctx.log("マシン操作パネルでジョグし、Record で現在位置を記録してください")
        while True:
            ctx.frame(_reference_point_frame(camera, detector, machine, position))
            command = ctx.next_command(timeout=0)
            if command is not None and _dispatch_reference_command(
                ctx, klipper, stage, calibration, position, command
            ):
                break
            ctx.checkpoint()

    pos = stage.get_position()
    ctx.apply_machine_settings(
        {
            "reference_point.x": round(pos.x, 3),
            "reference_point.y": round(pos.y, 3),
        }
    )
    ctx.log(f"基準点を設定に反映しました: x={pos.x:.3f}, y={pos.y:.3f}")
    return JobResult(
        summary=f"基準点を設定に反映しました: x={pos.x:.3f}, y={pos.y:.3f} mm"
    )


def _reference_point_frame(
    camera: Camera,
    detector: CircleDetector,
    machine: Machine,
    position: _CachedPosition,
) -> Image:
    """円検出注釈 + 現在位置テキスト入りのプレビューフレームを合成する."""
    crop_size = machine.camera.crop.size
    image = camera.capture()
    result = detector.detect_nearest_center(image)
    offset = result.offset.mm if result is not None else None
    preview = draw_overlay(image, crop_size, offset).numpy()
    if result is not None:
        draw_detected_circle(preview, result, crop_size)
    pos = position.get()
    cv2.putText(
        preview,
        f"Pos: X{pos.x:.3f}  Y{pos.y:.3f}",
        (10, 100),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        _TEXT_COLOR,
        2,
    )
    return Image(preview)


def _dispatch_reference_command(
    ctx: JobContext,
    klipper: Klipper,
    stage: XYZStage,
    calibration: CalibrationResult,
    position: _CachedPosition,
    command: dict[str, Any],
) -> bool:
    """WS command を 1 件処理する.

    Returns:
        record によりループを離脱する場合 True

    Raises:
        JobAborted: quit コマンドを受けた場合
    """
    match command:
        case {"type": "record"}:
            return True
        case {"type": "quit"}:
            ctx.log("中止しました。設定は変更していません")
            raise JobAborted()
    if handle_machine_command(
        ctx, klipper, stage, command, focus_z=calibration.z_position
    ):
        position.invalidate()
    else:
        ctx.log(f"未知のコマンドです: {command.get('type')!r}")
    return False


def _run_camera_calibration(ctx: JobContext) -> JobResult:
    """チェッカーボードを固定しステージを蛇行させ、レンズ歪みまで含めて校正する.

    - ホーミングも Z 移動もしない。操作者が合わせたフォーカス Z を壊さないため、
      実行前に手でホーミング + ジョグしておく前提
    - 校正は必ず歪み補正前のフレーム（``raw=True``）で行う。補正済みフレームで
      再校正すると残差歪みモデルになり、Apply で元の補正が静かに失われる
    """
    square_size = float(ctx.params["square_size"])
    residual_limit = float(ctx.params["residual_limit"])

    with ctx.open_camera(raw=True) as camera:
        resolution = camera.resolution.size
        camera_name = camera.info.name
        plan_view = _detect_planning_shot(ctx, camera)
        pixel_per_mm = measure_pixel_per_mm(plan_view, square_size)
        columns, rows = plan_view.pattern_size
        ctx.log(
            f"内部コーナー {columns}x{rows}"
            f" / 計画用ショットの pixel/mm {pixel_per_mm:.2f}"
        )

        klipper = create_command_klipper(ctx.machine)
        stage = XYZStage(klipper.readonly)
        homed_axes = klipper.get_status("toolhead", "homed_axes")
        if not all(axis in homed_axes for axis in "xy"):
            raise RuntimeError(
                "先にホーミングし、カメラのフォーカス位置へジョグしてから"
                "実行してください"
            )
        start = stage.get_position()

        grid = ScanGrid.plan(
            image_size=resolution,
            corners=plan_view.corners,
            pattern_size=plan_view.pattern_size,
            pixel_per_mm=pixel_per_mm,
        )
        if grid is None:
            raise RuntimeError(
                "チェッカーボードが視野に対して大きすぎてステージスキャンできません"
                "（マス目の小さいボードに替えるか、視野の半分程度に収まるボードを"
                "使ってください）"
            )
        ctx.log(
            f"格子 {grid.columns}x{grid.rows} = {len(grid.positions)}点"
            f" / 移動幅 X={grid.span_mm[0]:.2f}mm Y={grid.span_mm[1]:.2f}mm"
            f" / 想定コーナー被覆半径 {grid.max_corner_radius_px:.0f}px"
        )

        scanner = CheckerboardScanner(
            klipper, stage, camera, pattern_size=plan_view.pattern_size
        )
        outcome = scanner.scan(grid, on_view=_report_scan_progress(ctx))

    failure_artifacts = _save_scan_failures(ctx, outcome.failures)
    if len(outcome.views) < MINIMUM_SCAN_VIEWS:
        raise RuntimeError(
            f"コーナーを検出できた視点が不足しています"
            f"（有効 {len(outcome.views)}点 / 最低 {MINIMUM_SCAN_VIEWS}点）"
        )

    ctx.progress("校正")
    try:
        solved = IntrinsicsCalibrator(square_size, resolution).solve(outcome.views)
    except ValueError as exc:
        raise RuntimeError(f"内部パラメータを校正できません: {exc}") from exc
    result = attrs.evolve(solved, z_position=start.z)

    for line in result.quality.summary_lines():
        ctx.log(line)
    crop_side = result.usable_crop_side_px(residual_limit)
    ctx.log(f"残差 {residual_limit:.0f}um 以内に収まる正方 crop の辺長: {crop_side}px")

    filename = f"{camera_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_artifacts = _save_calibration_reports(ctx, result, outcome, grid, filename)
    if result.quality.after.rms_um > residual_limit:
        raise RuntimeError(
            f"補正後の残差 RMS が上限を超えました"
            f"（{result.quality.after.rms_um:.1f}um / 上限 {residual_limit:.1f}um）。"
            "artifacts の residuals.png と scan_verification.json で"
            "レンズ歪み以外の要因（ボードの傾き・フォーカス）を確認してください"
        )

    ctx.progress("完了", 100.0)
    return JobResult(
        summary=(
            f"pixel/mm {result.pixel_per_mm:.2f}（ステージ定規）"
            f" / 残差 RMS 補正前 {result.quality.before.rms_um:.0f}um"
            f" → 補正後 {result.quality.after.rms_um:.0f}um"
            f"（上限 {residual_limit:.0f}um）"
            f" / 有効視点 {len(outcome.views)}/{len(grid.positions)}"
            f" / crop 使用可 辺長 {crop_side}px"
            f" / Z={start.z:.3f}"
        ),
        artifacts=(*report_artifacts, *failure_artifacts),
        apply=ApplyPayload(
            label=f"{filename} を保存し [camera].calibration_file に設定",
            values={"camera.calibration_file": filename},
            files=(ApplyFile(filename, (ctx.artifacts_dir / filename).read_bytes()),),
        ),
    )


def _detect_planning_shot(ctx: JobContext, camera: Camera) -> CheckerboardView:
    """確認プロンプト → 1 枚撮影して計画用ショットのコーナーを検出する.

    パターンサイズ・pixel/mm・コーナー外接矩形の測定専用で、校正データには使わない
    （中心は 15 点の格子に含まれる）。検出できなければ警告を出して再プロンプトする。
    装置に触る前にカメラだけで完結する確認を済ませるため、Klipper 接続より先に行う。

    Raises:
        JobAborted: プロンプトで中止を選んだ場合
    """
    detector = CheckerboardDetector()
    ctx.progress("撮影待ち")
    while True:
        proceed = ctx.prompt(
            PromptSpec(
                kind="confirm",
                message=(
                    "十字線にチェッカーボードのマス目を合わせ、"
                    "フォーカスをプレビューで確認してください（いいえで中止）"
                ),
                default=True,
                true_label="続行",
                false_label="中止",
            )
        )
        if not proceed:
            raise JobAborted()
        image = camera.capture()
        ctx.frame(image)
        ctx.progress("検出")
        # ステージ位置は校正に使わない（この視点は格子に含まれない）
        view = detector.detect(image, Point2d(0.0, 0.0))
        if view is not None:
            ctx.frame(detector.draw(image, view))
            return view
        ctx.log("チェッカーボードが検出できませんでした")


def _report_scan_progress(ctx: JobContext) -> Callable[[ScanProgress], None]:
    """CheckerboardScanner の 1 点ごとのコールバックを作る."""

    def report(progress: ScanProgress) -> None:
        ctx.checkpoint()
        done = progress.index + 1
        ctx.progress(f"スキャン {done}/{progress.total}", 100.0 * done / progress.total)
        if progress.annotated is None:
            ctx.log(f"視点 {done}/{progress.total}: コーナーを検出できませんでした")
        else:
            ctx.frame(progress.annotated)

    return report


def _save_scan_failures(
    ctx: JobContext, failures: Sequence[ScanFailure]
) -> tuple[Artifact, ...]:
    """検出できなかった視点の生フレームを診断用に保存する."""
    artifacts: list[Artifact] = []
    for failure in failures:
        filename = f"scan_failure_{failure.index}.png"
        failure.raw.save(ctx.artifacts_dir / filename)
        ctx.frame(failure.raw, persist=True)
        artifacts.append(
            ctx.artifact(f"検出失敗 視点{failure.index}", filename, "image")
        )
        ctx.log(
            f"検出失敗 視点{failure.index}: /artifacts/{ctx.artifacts_dir.name}/{filename}"
        )
    return tuple(artifacts)


def _save_calibration_reports(
    ctx: JobContext,
    result: CalibrationResult,
    outcome: ScanOutcome,
    grid: ScanGrid,
    filename: str,
) -> tuple[Artifact, ...]:
    """残差レポート・被覆図・検証 JSON・キャリブレーション JSON を保存する.

    残差の上限超過でも診断できるよう、合否判定より先に呼ぶ。
    """
    render_scan_residuals(
        result.quality,
        outcome.views,
        undistort_views(outcome.views, result.intrinsics),
        ctx.artifacts_dir / "residuals.png",
    )
    draw_scan_coverage(result.resolution, outcome.views, CROP_CANDIDATES_PX).save(
        ctx.artifacts_dir / "corner_coverage.png"
    )
    (ctx.artifacts_dir / "scan_verification.json").write_text(
        json.dumps(
            {
                "quality": result.to_dict()["quality"],
                "grid": {
                    "columns": grid.columns,
                    "rows": grid.rows,
                    "point_count": len(grid.positions),
                    "span_mm": list(grid.span_mm),
                    "max_corner_radius_px": grid.max_corner_radius_px,
                },
                "effective_view_count": len(outcome.views),
                "failed_view_indices": [f.index for f in outcome.failures],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result.save(ctx.artifacts_dir / filename)
    # 残差超過で FAILED になると JobResult.artifacts が返らないので、図と JSON の
    # URL はログに出しておく（エラー文がこの 2 つを見るよう案内している）
    for label, name in (
        ("残差レポート", "residuals.png"),
        ("検証 JSON", "scan_verification.json"),
    ):
        ctx.log(f"{label}: /artifacts/{ctx.artifacts_dir.name}/{name}")
    return (
        ctx.artifact("残差レポート", "residuals.png", "image"),
        ctx.artifact("コーナー被覆", "corner_coverage.png", "image"),
        ctx.artifact("検証 JSON", "scan_verification.json", "file"),
        ctx.artifact("キャリブレーション JSON", filename, "file"),
    )


def _board_corners(result: BoardCalibrationResult) -> list[tuple[str, Point2d]]:
    """ボード四隅の (ラベル, board 座標) を返す."""
    outline = result.pcb.outline
    return [
        ("Top-Left", Point2d(0.0, 0.0)),
        ("Top-Right", Point2d(outline.width, 0.0)),
        ("Bottom-Right", Point2d(outline.width, outline.height)),
        ("Bottom-Left", Point2d(0.0, outline.height)),
    ]


def _move_to(
    result: BoardCalibrationResult,
    machine_pt: Point2d,
    speed: Speed = Speed.absolute(30),
) -> None:
    """指定の機械座標へ移動し完了を待つ."""
    result.klipper.send_gcode(
        result.stage.move(x=machine_pt.x, y=machine_pt.y, speed=speed)
        + gcode.wait_for_done()
    )


def _pad_renderer(
    result: BoardCalibrationResult,
    session: PadAlignmentSession,
    projector: CopperProjector,
    pads: Sequence[Pad],
    position: Point2d,
) -> PadResultRenderer:
    """Pad 群の照合結果 overlay 合成器を構築する."""
    return PadResultRenderer(
        projector=projector,
        edge_detector=session.edge_detector,
        roi_polygons=[p.copper_polygon for p in pads],
        paste_polygons=[p.polygon for p in pads],
        pad_align=result.machine.paste_dispenser.pad_align,
        position=position,
    )


def _stream_labeled_frames(
    ctx: JobContext,
    result: BoardCalibrationResult,
    label: str,
    duration: float = RESULT_DISPLAY_SEC,
) -> None:
    """ラベル付きフレームを duration 秒間プレビューへ配信する."""
    crop_size = result.machine.camera.crop.size
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        ctx.frame(render_label(result.camera.capture(), crop_size, label))
        ctx.checkpoint()


def _labeled_frame_sink(
    ctx: JobContext, result: BoardCalibrationResult, label: str
) -> Callable[[], None]:
    """ラベル付きライブフレームを 1 枚プレビューへ出すコールバックを作る.

    カメラ取得の失敗でツアーを落とさない。最初の 1 回だけログへ警告し、以降は黙って更新を見送る。
    """
    crop_size = result.machine.camera.crop.size
    warned = False

    def submit() -> None:
        nonlocal warned
        try:
            ctx.frame(render_label(result.camera.capture(), crop_size, label))
        except Exception as exc:
            if not warned:
                warned = True
                ctx.log(f"プレビュー更新に失敗しました（巡回は継続します）: {exc}")

    return submit


def _stream_pad_result(
    ctx: JobContext,
    result: BoardCalibrationResult,
    renderer: PadResultRenderer,
    lines: list[str],
    duration: float = RESULT_DISPLAY_SEC,
) -> None:
    """Pad 照合結果 overlay を duration 秒間プレビューへ配信する."""
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        ctx.frame(renderer.render(result.camera.capture(), lines))
        ctx.checkpoint()


def _run_board_tour(ctx: JobContext) -> JobResult:
    """四隅巡回 → 銅箔照合 → 補正適用済み全 pad 巡回を実行する."""
    with ctx.open_camera() as camera:
        result = setup_board(ctx, camera)
        board_transform = result.board_transform

        # 四隅巡回（左上に戻る 5 点）
        corners = [*_board_corners(result), ("Top-Left", Point2d(0.0, 0.0))]
        for index, (name, board_pt) in enumerate(corners):
            ctx.progress("四隅巡回", 100.0 * index / len(corners))
            machine_pt = board_transform.apply(board_pt)
            ctx.log(
                f"{name}: Board({board_pt.x:.1f}, {board_pt.y:.1f}) -> "
                f"Machine({machine_pt.x:.3f}, {machine_pt.y:.3f})"
            )
            _move_to(result, machine_pt)
            _stream_labeled_frames(ctx, result, f"Corner: {name}")

        # 銅箔照合（部品単位の自動位置合わせ）。失敗時は FAILED overlay を配信する
        groups = sorted_top_component_pads(result)
        ctx.log(f"padを持つ部品数: {len(groups)}")
        session = PadAlignmentSession.from_calibration(result, frame_sink=ctx.frame)

        def render_failed(group: ComponentPads, index: int) -> None:
            renderer = _pad_renderer(
                result,
                session,
                session.projector,
                group.pads,
                result.stage.get_position().to2d(),
            )
            lines = [
                f"{group.component.designator} {index + 1}/{len(groups)}",
                "FAILED",
            ]
            _stream_pad_result(ctx, result, renderer, lines)

        alignments = align_component_groups(
            ctx, session, groups, on_failure=render_failed
        )

        # 補正適用済みの全 pad 巡回
        entries = _corrected_entries(result, session, alignments)
        for index, (pad, renderer_projector, target) in enumerate(entries):
            ctx.progress("補正巡回", 100.0 * index / len(entries))
            ctx.checkpoint()
            _move_to(result, target, speed=Speed.rate(0.5))
            renderer = _pad_renderer(result, session, renderer_projector, [pad], target)
            lines = [f"{pad.designator}.{pad.pad_number} {index + 1}/{len(entries)}"]
            _stream_pad_result(ctx, result, renderer, lines)

        # board 原点へ戻して終了
        _move_to(result, board_transform.apply(Point2d(0.0, 0.0)))

    aligned_pads = sum(len(group.pads) for group, _ in alignments)
    return JobResult(
        summary=(
            f"照合成功 {len(alignments)}/{len(groups)} 部品"
            f"（{aligned_pads} pads）/ 補正巡回 {len(entries)} pads"
        )
    )


def _corrected_entries(
    result: BoardCalibrationResult,
    session: PadAlignmentSession,
    alignments: list[tuple[ComponentPads, PadAlignmentResult]],
) -> list[tuple[Pad, CopperProjector, Point2d]]:
    """補正適用済みの pad 巡回先を nearest neighbor 順で構築する."""
    entries: list[tuple[Pad, CopperProjector, Point2d]] = []
    for group, alignment in alignments:
        corrected_transform = Compose(
            [result.board_transform, alignment.machine_transform]
        )
        corrected_projector = session.corrected_projector(alignment.machine_transform)
        for pad in group.pads:
            entries.append(
                (pad, corrected_projector, corrected_transform.apply(pad.center))
            )

    current = result.stage.get_position()
    return sort_by_nearest(
        entries, current.to2d().to3d(), key=lambda entry: entry[2].to3d()
    )


def _orthogonality_points(
    result: BoardCalibrationResult,
) -> list[tuple[str, Point2d]]:
    """直行性テストの巡回点列（四隅 + TOP 層 pad 中心）を構築する.

    pad 中心は現在位置からの nearest neighbor 順に並べる（machine 座標で比較）。
    """
    board_transform = result.board_transform
    top_pads = [p for p in result.pcb.pads if p.layer == Layer.TOP]
    centers = sort_by_nearest(
        [p.center for p in top_pads],
        result.stage.get_position().to2d().to3d(),
        key=lambda center: board_transform.apply(center).to3d(),
    )
    return [
        *_board_corners(result),
        *(
            (f"Grid {index + 1}/{len(centers)}", center)
            for index, center in enumerate(centers)
        ),
    ]


def _run_orthogonality_test(ctx: JobContext) -> JobResult:
    """調整前の直行性指標を計測し、四隅・グリッド交点を対話的に巡回する.

    各巡回先では確認プロンプトの応答を待つ間、ラベル付き overlay（十字線・
    関心領域・ラベル）を載せたライブフレームを配信し続ける。

    「次へ」で次の点へ進み、一周したら四隅から再開する。「終了」で正常終了する。
    巡回点列は開始時に 1 回だけ構築し、`Grid k/n` と pad の対応を周回間で固定する。
    指標は開始時の 1 回計測なので、巡回中のテンション調整は反映されない。

    Raises:
        JobAborted: 巡回中またはプロンプト待機中に abort された場合
    """
    total_points = 0
    cycle = 1
    finished = False
    with ctx.open_camera() as camera:
        result = setup_board(ctx, camera)
        board_transform = result.board_transform

        metrics = OrthogonalityMetrics.from_transform(board_transform)
        ctx.log("調整前の直行性指標:")
        ctx.log(f"軸間角の 90° からのずれ: {metrics.axis_angle_error_deg:+.3f} deg")
        ctx.log(f"スケール X: {metrics.scale_x:.5f} / Y: {metrics.scale_y:.5f}")
        ctx.log(
            "各巡回先で確認プロンプトが出ます。"
            "ベルトテンションを調整して「次へ」、やめるときは「終了」を押してください"
        )

        points = _orthogonality_points(result)
        while not finished:
            for index, (label, board_pt) in enumerate(points):
                ctx.progress(f"巡回 {cycle} 周目", 100.0 * index / len(points))
                ctx.checkpoint()
                machine_pt = board_transform.apply(board_pt)
                ctx.log(
                    f"{label}: Board({board_pt.x:.1f}, {board_pt.y:.1f}) -> "
                    f"Machine({machine_pt.x:.3f}, {machine_pt.y:.3f})"
                )
                _move_to(result, machine_pt)
                total_points += 1
                if not confirm_next_point(
                    ctx, label, while_waiting=_labeled_frame_sink(ctx, result, label)
                ):
                    finished = True
                    break
            else:
                cycle += 1

    return JobResult(
        summary=(
            f"調整前の軸間角ずれ {metrics.axis_angle_error_deg:+.3f} deg / "
            f"調整前 scale X {metrics.scale_x:.5f} Y {metrics.scale_y:.5f} / "
            f"巡回 {total_points} 点（{cycle} 周目で終了）"
        )
    )


def _run_generate_grid_pcb(ctx: JobContext) -> JobResult:
    """直行性テスト用グリッド PCB を生成する（装置・カメラ不要）."""
    # pcbnew 依存はジョブ実行時のみ（KiCAD 未導入でも webui は起動可）
    from pcbasm.pcb.generate import generate_grid_pcb

    size = float(ctx.params["size"])
    divisions = int(ctx.params["divisions"])
    pad_size = float(ctx.params["pad_size"])

    ctx.progress("生成")
    filename = f"grid_{divisions}x{divisions}.kicad_pcb"
    generate_grid_pcb(size, divisions, pad_size, ctx.artifacts_dir / filename)
    ctx.log(f"生成: {filename}（{size}x{size} mm, {divisions ** 2} パッド）")
    ctx.progress("完了", 100.0)

    return JobResult(
        summary=f"{size:g}x{size:g} mm / {divisions}x{divisions} = "
        f"{divisions ** 2} パッド",
        artifacts=(ctx.artifact("グリッド PCB", filename, "file"),),
    )
