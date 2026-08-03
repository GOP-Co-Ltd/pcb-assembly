"""Posctrl タブのジョブ定義（基準点設定 / カメラキャリブレーション / 巡回系 / グリッド PCB 生成）."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

import attrs
import cv2
import numpy as np

from pcbasm import gcode
from pcbasm.config import Machine
from pcbasm.geometry import Point2d, Point3d, Shift, sort_by_nearest
from pcbasm.hal import Camera, Klipper, Speed, XYZStage, create_xyz_stage
from pcbasm.pcb import Layer, Pad
from pcbasm.posctrl import (
    AlignmentRegion,
    BoardAlignment,
    BoardCalibrationResult,
    CopperProjector,
    OffsetObserver,
    OffsetTransformMeasurer,
    OrthogonalityMetrics,
    PadResultRenderer,
    RegionAlignmentSession,
    XYPositionAdjustor,
    render_label,
)
from pcbasm.vision import (
    CalibrationResult,
    CheckerboardCalibrator,
    CircleDetector,
    Image,
    draw_detected_circle,
    draw_overlay,
    safe_move_distance,
)
from pcbasm.xy_calibration import (
    XYCalibrationGrid,
    XYCalibrationResult,
    XYCalibrationTransform,
    migrate_machine_xy_settings,
)
from webui.jobs.board_ops import align_regions, confirm_next_point, setup_board
from webui.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from webui.jobs.context import (
    ApplyFile,
    ApplyPayload,
    JobAborted,
    JobContext,
    JobResult,
    PromptSpec,
)
from webui.jobs.machine_commands import create_command_klipper, handle_machine_command

# camera_calibration の Z 取得（best-effort）のタイムアウト [sec]
Z_QUERY_TIMEOUT = 5.0
# 巡回先 1 点あたりのフレーム配信時間 [sec]
RESULT_DISPLAY_SEC = 1.0
# reference_point_setup の現在位置キャッシュ TTL [sec]
POSITION_CACHE_SEC = 0.5

_TEXT_COLOR = (0, 255, 255)  # 現在位置テキストの色 (BGR: 黄)

XY_SAMPLE_COUNT = 30
XY_MINIMUM_SAMPLE_COUNT = 20
XY_MAX_STANDARD_DEVIATION_MM = 0.03
XY_CENTER_TOLERANCE_MM = 0.02
XY_CENTER_MAX_ITERATIONS = 10
XY_RMS_LIMIT_MM = 0.03
XY_MAX_ERROR_LIMIT_MM = 0.05


def register_posctrl_jobs(catalog: JobCatalog) -> None:
    """Posctrl タブのジョブを登録する."""
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
            ),
            persisted_params=("square_size",),
            uses_machine=True,
        )
    )
    catalog.register(
        JobDefinition(
            name="xy_calibration",
            label="XYキャリブレーション",
            tab="posctrl",
            run=_run_xy_calibration,
            params=(
                ParamSpec(
                    "hole_diameter",
                    "ホールサイズ",
                    "float",
                    3.0,
                    unit="mm",
                    minimum=0.001,
                ),
                ParamSpec(
                    "spacing",
                    "グリッド間隔",
                    "float",
                    10.0,
                    unit="mm",
                    minimum=0.001,
                ),
                ParamSpec("rows", "縦のホール数", "int", 5, minimum=2),
                ParamSpec("columns", "横のホール数", "int", 5, minimum=2),
            ),
            persisted_params=("hole_diameter", "spacing", "rows", "columns"),
            uses_machine=True,
            accepts_commands=True,
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


def _run_xy_calibration(ctx: JobContext) -> JobResult:
    """穴グリッドを計測してXYステージ補正を生成する."""
    grid = XYCalibrationGrid(
        hole_diameter_mm=float(ctx.params["hole_diameter"]),
        spacing_mm=float(ctx.params["spacing"]),
        rows=int(ctx.params["rows"]),
        columns=int(ctx.params["columns"]),
    )
    machine = ctx.machine
    calibration = CalibrationResult.load(machine.camera.calibration_file)
    if calibration.z_position is None:
        raise RuntimeError("カメラキャリブレーションにfocus Zがありません")
    old_transform = _load_machine_xy_transform(machine)

    detector = CircleDetector(
        pixel_per_mm=calibration.pixel_per_mm,
        target_diameter_mm=grid.hole_diameter_mm,
        crop_size=machine.camera.crop.size,
        diameter_tolerance_mm=min(0.5, grid.hole_diameter_mm * 0.25),
    )
    klipper = create_command_klipper(machine)
    raw_stage = create_xyz_stage(machine, klipper.readonly, calibrated=False)

    ctx.progress("ホーミング")
    klipper.send_gcode(gcode.homing(x=True, y=True, z=True) + gcode.wait_for_done())
    klipper.send_gcode(raw_stage.move(z=calibration.z_position) + gcode.wait_for_done())

    with ctx.open_camera() as camera:
        position = _CachedPosition(raw_stage)
        ctx.progress("左上位置合わせ")
        ctx.log("左上ホールを十字へ合わせ、Recordを押してください")
        while True:
            ctx.frame(_reference_point_frame(camera, detector, machine, position))
            command = ctx.next_command(timeout=0)
            if command is not None and _dispatch_reference_command(
                ctx, klipper, raw_stage, calibration, position, command
            ):
                break
            ctx.checkpoint()
        ctx.set_accepts_commands(False)
        ctx.log("手動操作を終了し、自動計測を開始します")

        observer = OffsetObserver(
            detector,
            camera,
            machine.camera.crop.size,
            frame_sink=ctx.frame,
            sample_count=XY_SAMPLE_COUNT,
            minimum_sample_count=XY_MINIMUM_SAMPLE_COUNT,
            max_standard_deviation_mm=XY_MAX_STANDARD_DEVIATION_MM,
        )
        offset_transform = OffsetTransformMeasurer(
            observe=observer.observe,
            klipper=klipper,
            stage=raw_stage,
            move_distance=min(
                safe_move_distance(machine.camera.crop.size, margin=0.3)
                / calibration.pixel_per_mm,
                grid.spacing_mm * 0.3,
            ),
        ).measure()
        adjustor = XYPositionAdjustor(
            observe=observer.observe,
            klipper=klipper,
            stage=raw_stage,
            offset_transform=offset_transform,
            tolerance=XY_CENTER_TOLERANCE_MM,
            max_iterations=XY_CENTER_MAX_ITERATIONS,
        )

        ctx.progress("四隅計測", 0.0)
        corners = _measure_xy_corners(ctx, grid, klipper, raw_stage, adjustor)
        navigation = _fit_navigation_affine(grid, corners)

        measured: list[Point2d] = []
        local_points = grid.points()
        for index, local in enumerate(local_points):
            ctx.progress("全点計測", 100.0 * index / len(local_points))
            prediction = _apply_affine(navigation, local)
            measured.append(
                _measure_xy_point(grid, klipper, raw_stage, adjustor, prediction)
            )
            ctx.log(
                f"計測 {index + 1}/{len(local_points)}: "
                f"X={measured[-1].x:.4f}, Y={measured[-1].y:.4f}"
            )

        initial_result = XYCalibrationResult.fit(grid, measured)
        calibrated_stage = XYZStage(klipper.readonly, initial_result.transform)
        errors: list[Point2d] = []
        for reverse_index, logical in enumerate(
            reversed(initial_result.logical_points)
        ):
            ctx.progress(
                "検証", 100.0 * reverse_index / len(initial_result.logical_points)
            )
            klipper.send_gcode(
                calibrated_stage.move(
                    x=logical.x, y=logical.y, speed=Speed.absolute(30)
                )
                + gcode.wait(0.5)
                + gcode.wait_for_done()
            )
            observed = observer.observe().apply(Point2d(0.0, 0.0))
            error = offset_transform.apply(observed)
            errors.append(error)
            ctx.log(
                f"検証 {reverse_index + 1}/{len(initial_result.logical_points)}: "
                f"誤差={error.norm:.4f} mm"
            )
        errors.reverse()

    result = XYCalibrationResult.fit(grid, measured, errors)
    if result.rms_error > XY_RMS_LIMIT_MM or result.max_error > XY_MAX_ERROR_LIMIT_MM:
        raise RuntimeError(
            "XYキャリブレーション精度が基準を満たしません: "
            f"RMS={result.rms_error:.4f} mm / 最大={result.max_error:.4f} mm"
        )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"xy_calibration_{timestamp}.json"
    artifact_path = ctx.artifacts_dir / filename
    result.save(artifact_path)

    migrated = migrate_machine_xy_settings(machine, old_transform, result.transform)
    apply_values: dict[str, float | str] = {
        **migrated,
        "xy_calibration.calibration_file": filename,
    }
    ctx.progress("完了", 100.0)
    return JobResult(
        summary=(
            f"{grid.columns}x{grid.rows}点 / RMS={result.rms_error:.4f} mm / "
            f"最大誤差={result.max_error:.4f} mm。適用後に基準点・キャップ・"
            "ツールヘッド位置を再確認してください"
        ),
        artifacts=(ctx.artifact("XYキャリブレーション JSON", filename, "file"),),
        apply=ApplyPayload(
            label=f"{filename} を保存してXY補正を有効化",
            values=apply_values,
            files=(ApplyFile(filename, artifact_path.read_bytes()),),
        ),
    )


def _measure_xy_corners(
    ctx: JobContext,
    grid: XYCalibrationGrid,
    klipper: Klipper,
    stage: XYZStage,
    adjustor: XYPositionAdjustor,
) -> tuple[Point2d, Point2d, Point2d, Point2d]:
    width = (grid.columns - 1) * grid.spacing_mm
    height = (grid.rows - 1) * grid.spacing_mm
    top_left = adjustor.adjust()
    top_right_guess = top_left + Point2d(width, 0.0)
    top_right = _measure_xy_point(grid, klipper, stage, adjustor, top_right_guess)
    x_direction = (top_right - top_left) / width
    y_direction = Point2d(-x_direction.y, x_direction.x)
    bottom_right = _measure_xy_point(
        grid,
        klipper,
        stage,
        adjustor,
        top_right + y_direction * height,
    )
    bottom_left = _measure_xy_point(
        grid,
        klipper,
        stage,
        adjustor,
        top_left + y_direction * height,
    )
    ctx.progress("四隅計測", 100.0)
    return top_left, top_right, bottom_right, bottom_left


def _measure_xy_point(
    grid: XYCalibrationGrid,
    klipper: Klipper,
    stage: XYZStage,
    adjustor: XYPositionAdjustor,
    prediction: Point2d,
) -> Point2d:
    klipper.send_gcode(
        stage.move(x=prediction.x, y=prediction.y, speed=Speed.absolute(30))
        + gcode.wait(0.5)
        + gcode.wait_for_done()
    )
    measured = adjustor.adjust()
    distance = (measured - prediction).norm
    if distance >= grid.spacing_mm * 0.45:
        raise RuntimeError(
            f"予測位置から離れたホールを検出しました: 距離={distance:.3f} mm"
        )
    return measured


def _fit_navigation_affine(
    grid: XYCalibrationGrid,
    corners: tuple[Point2d, Point2d, Point2d, Point2d],
) -> np.ndarray:
    width = (grid.columns - 1) * grid.spacing_mm
    height = (grid.rows - 1) * grid.spacing_mm
    local = np.array(((0.0, 0.0), (width, 0.0), (width, height), (0.0, height)))
    raw = np.array([(point.x, point.y) for point in corners])
    design = np.column_stack((local, np.ones(4)))
    coefficients, _, rank, _ = np.linalg.lstsq(design, raw, rcond=None)
    if rank < 3:
        raise RuntimeError("四隅からnavigation affineを推定できません")
    return coefficients.T


def _apply_affine(affine: np.ndarray, point: Point2d) -> Point2d:
    result = affine @ np.array((point.x, point.y, 1.0))
    return Point2d(float(result[0]), float(result[1]))


def _load_machine_xy_transform(machine: Machine) -> XYCalibrationTransform:
    if machine.xy_calibration is None:
        return XYCalibrationTransform.identity()
    return XYCalibrationResult.load(machine.xy_calibration.calibration_file).transform


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
    stage = create_xyz_stage(machine, klipper.readonly)
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
    """チェッカーボードで pixel/mm をキャリブレーションし JSON を保存候補にする."""
    square_size = float(ctx.params["square_size"])
    crop_size = ctx.machine.camera.crop.size
    calibrator = CheckerboardCalibrator(square_size, crop_size)

    with ctx.open_camera() as camera:
        ctx.progress("撮影待ち")
        while True:
            proceed = ctx.prompt(
                PromptSpec(
                    kind="confirm",
                    message="チェッカーボードを配置して撮影しますか?（いいえで中止）",
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
            detection = calibrator.calibrate(image)
            if detection is not None:
                break
            ctx.log("チェッカーボードが検出できませんでした")
        result, vis = detection
        camera_name = camera.info.name

    ctx.frame(vis)
    ctx.log(f"pixel/mm: {result.pixel_per_mm:.2f}")
    ctx.log(f"1マスの距離の標準偏差: {result.std_distance_px:.2f} px")

    # Z 位置は best-effort（Klipper 不通でも calibration 自体は成立させる）
    z: float | None = None
    try:
        klipper = Klipper(
            host=ctx.machine.klipper.host,
            port=ctx.machine.klipper.port,
            timeout=Z_QUERY_TIMEOUT,
        )
        z = create_xyz_stage(ctx.machine, klipper.readonly).get_position().z
    except Exception as exc:
        ctx.log(f"Z 位置の取得に失敗しました（z_position なしで続行）: {exc}")
    result = attrs.evolve(result, z_position=z)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{camera_name}_{timestamp}.json"
    png_name = f"{camera_name}_{timestamp}.png"
    vis.save(ctx.artifacts_dir / png_name)
    result.save(ctx.artifacts_dir / filename)
    json_bytes = (ctx.artifacts_dir / filename).read_bytes()
    ctx.progress("完了", 100.0)

    return JobResult(
        summary=(
            f"pixel/mm: {result.pixel_per_mm:.2f}"
            f" / σ: {result.std_distance_px:.2f} px"
            f" / Z: {z if z is not None else '未取得'}"
        ),
        artifacts=(
            ctx.artifact("コーナー検出", png_name, "image"),
            ctx.artifact("キャリブレーション JSON", filename, "file"),
        ),
        apply=ApplyPayload(
            label=f"{filename} を保存し [camera].calibration_file に設定",
            values={"camera.calibration_file": filename},
            files=(ApplyFile(filename, json_bytes),),
        ),
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
    session: RegionAlignmentSession,
    projector: CopperProjector,
    pads: Sequence[Pad],
    position: Point2d,
) -> PadResultRenderer:
    """Pad 群の照合結果 overlay 合成器を構築する."""
    return PadResultRenderer(
        projector=projector,
        edge_detector=session.edge_detector,
        roi=session.region_roi,
        paste_polygons=[p.polygon for p in pads],
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

        top_pads = [pad for pad in result.pcb.pads if pad.layer == Layer.TOP]
        session = RegionAlignmentSession(result, frame_sink=ctx.frame)
        regions = session.plan_regions([pad.center for pad in top_pads])
        ctx.log(f"照合対象の領域数: {len(regions)}")

        def render_failed(region: AlignmentRegion, index: int) -> None:
            pads = [pad for pad in top_pads if region.covers(pad.center)]
            renderer = _pad_renderer(
                session,
                session.projector,
                pads,
                result.stage.get_position().to2d(),
            )
            lines = [
                f"Region {region.index + 1}/{len(regions)}",
                "FAILED",
            ]
            _stream_pad_result(ctx, result, renderer, lines)

        aligned = align_regions(ctx, session, regions, on_failure=render_failed)
        alignment = BoardAlignment(results=tuple(aligned))

        # 補正適用済みの全 pad 巡回
        entries = _corrected_entries(result, session, alignment, top_pads)
        for index, (pad, renderer_projector, target) in enumerate(entries):
            ctx.progress("補正巡回", 100.0 * index / len(entries))
            ctx.checkpoint()
            initial_correction = alignment.correction_for(
                pad.center, designator=pad.designator
            )
            refined = session.refine(pad.center, initial_correction, pad.polygon)
            if refined is not None:
                initial = initial_correction.apply(
                    result.board_transform.apply(pad.center)
                )
                residual = refined.displacement - (
                    initial - result.board_transform.apply(pad.center)
                )
                ctx.log(
                    f"{pad.designator}.{pad.pad_number}: "
                    f"residual=({residual.x:+.4f}, {residual.y:+.4f}) mm, "
                    f"passes={refined.passes}"
                )
                correction = Shift.from_point(refined.displacement)
                renderer_projector = session.projector.with_correction(correction)
                target = correction.apply(result.board_transform.apply(pad.center))
            _move_to(result, target, speed=Speed.rate(0.5))
            renderer = _pad_renderer(session, renderer_projector, [pad], target)
            lines = [f"{pad.designator}.{pad.pad_number} {index + 1}/{len(entries)}"]
            _stream_pad_result(ctx, result, renderer, lines)

        # board 原点へ戻して終了
        _move_to(result, board_transform.apply(Point2d(0.0, 0.0)))

    return JobResult(
        summary=(
            f"照合成功 {len(aligned)}/{len(regions)} 領域 / "
            f"補正巡回 {len(entries)} pads"
        )
    )


def _corrected_entries(
    result: BoardCalibrationResult,
    session: RegionAlignmentSession,
    alignment: BoardAlignment,
    pads: Sequence[Pad],
) -> list[tuple[Pad, CopperProjector, Point2d]]:
    """補正適用済みの pad 巡回先を nearest neighbor 順で構築する."""
    entries: list[tuple[Pad, CopperProjector, Point2d]] = []
    for pad in pads:
        correction = alignment.correction_for(pad.center, designator=pad.designator)
        corrected_projector = session.projector.with_correction(correction)
        target = correction.apply(result.board_transform.apply(pad.center))
        entries.append((pad, corrected_projector, target))

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
