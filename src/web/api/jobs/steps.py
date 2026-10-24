"""複数のジョブが使う台本の部品（基板の計測、マシン操作の command、ローディング、塗布量校正のフィットと報告）.

ドメインの手順は ``pcbasm`` に置き、ここは ``JobContext`` を使う対話（prompt / progress / log /
checkpoint）とループ制御だけを担う。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import attrs
import cv2
from pydantic import ValidationError

from pcbasm.align import AlignEvent, AlignStep, BoardFrame, measure_board_frame
from pcbasm.board import Board, Outline
from pcbasm.gcode import GCode
from pcbasm.geometry import Point2d
from pcbasm.hal import Camera, KlipperClient, Speed, XYZStage
from pcbasm.machine.commands import COMMAND_TIMEOUT, to_gcode
from pcbasm.pasting.applicator import PasteApplicator
from pcbasm.pasting.paste_volume.calibration import (
    auto_calibration_path,
    calibration_path,
    write_calibration,
)
from pcbasm.pasting.paste_volume.dataset.reader import DatasetSession
from pcbasm.pasting.paste_volume.detect import DotDetectionSpec, detection_mask
from pcbasm.pasting.paste_volume.fit import CalibrationFit, CellMeasurement, fit_session
from pcbasm.pasting.paste_volume.plot import (
    render_calibration_scatter,
    render_detection_montage,
)
from pcbasm.pasting.session import PasteSession
from pcbasm.validators import is_finite_number
from pcbasm.vision.image import ImageArray
from web.api.jobs.runtime.catalog import ParamSpec
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import Artifact, ParamValue
from web.api.machine_commands import to_command
from web.contract.models import MachineCommandRequest

logger = logging.getLogger(__name__)

# --- 基板の計測と移動 ---


def setup_board(
    ctx: JobContext,
    klipper: KlipperClient,
    stage: XYZStage,
    camera: Camera,
    *,
    tolerance: float | None = None,
    board: Board | None = None,
) -> BoardFrame:
    """Progress("セットアップ") → ボード計測セットアップの定型.

    Args:
        ctx: 実行中ジョブのコンテキスト
        klipper: このジョブの Klipper への接続（``ctx.klipper()``）
        stage: klipper のステージ
        camera: 撮像に使うカメラ
        tolerance: 位置合わせ許容誤差。省略時は ``ctx.params["tolerance"]``
        board: 計測対象の基板。省略時は選択 PCB（``ctx.board()``）
    """
    if tolerance is None:
        tolerance = float(ctx.params["tolerance"])
    if board is None:
        board = ctx.board()
    ctx.progress("セットアップ")
    return measure_board_frame(
        board,
        ctx.machine,
        klipper=klipper,
        stage=stage,
        camera=camera,
        tolerance=tolerance,
        frame_sink=ctx.frame,
    )


def setup_paste_session(
    ctx: JobContext,
    camera: Camera,
    *,
    tolerance: float | None = None,
    board: Board | None = None,
) -> PasteSession:
    """ボード計測セットアップ（:func:`setup_board`）から塗布セッションを組む定型.

    Klipper はこのジョブの接続（``ctx.klipper()``）を使う。
    引数は :func:`setup_board` と同じ。
    """
    klipper = ctx.klipper()
    stage = XYZStage(klipper)
    board_frame = setup_board(
        ctx, klipper, stage, camera, tolerance=tolerance, board=board
    )
    return PasteSession.from_board_frame(
        board_frame, ctx.machine, klipper=klipper, stage=stage, camera=camera
    )


def align_events(ctx: JobContext) -> Callable[[AlignEvent], None]:
    """位置合わせの経過を、進捗・中断の確認・照合ログへ流すコールバックを作る.

    コールバックは、照合の 1 件に取りかかる前に abort されていれば ``JobAborted`` を送出する。
    """

    def on_event(event: AlignEvent) -> None:
        match event:
            case AlignStep(stage=stage, index=index, total=total):
                ctx.progress(stage, 100.0 * index / total)
                ctx.checkpoint()
            case _:
                ctx.log(event.message)

    return on_event


def board_corners(outline: Outline) -> list[tuple[str, Point2d]]:
    """ボード四隅の (ラベル, board 座標) を返す."""
    return [
        ("Top-Left", Point2d(0.0, 0.0)),
        ("Top-Right", Point2d(outline.width, 0.0)),
        ("Bottom-Right", Point2d(outline.width, outline.height)),
        ("Bottom-Left", Point2d(0.0, outline.height)),
    ]


def move_to(
    klipper: KlipperClient,
    stage: XYZStage,
    machine_pt: Point2d,
    speed: Speed | None = None,
) -> None:
    """指定の機械座標へ移動し完了を待つ."""
    klipper.send_gcode(
        stage.move(x=machine_pt.x, y=machine_pt.y, speed=speed) + GCode.wait_for_done()
    )


# --- マシン操作パネルの command ---

# command の type がマシン操作でない（ジョブ固有の command）ときの検証エラー
_NOT_MACHINE_COMMAND = frozenset({"union_tag_invalid", "union_tag_not_found"})


def handle_machine_command(
    ctx: JobContext,
    klipper: KlipperClient,
    stage: XYZStage,
    command: Mapping[str, Any],
) -> bool:
    """対話ジョブの WS command がマシン操作なら、REST と同じ列を同じ timeout で送る.

    Returns:
        マシン操作なら True（形の誤りや実行できない理由は log してジョブを続ける）。
        type がマシン操作でない command は False（log は呼び出し側の責務）。
        送信の失敗はジョブを失敗させるため握りつぶさない
    """
    try:
        request = MachineCommandRequest.model_validate(command)
    except ValidationError as exc:
        errors = exc.errors()
        if errors[0]["type"] in _NOT_MACHINE_COMMAND:
            return False
        reason = ", ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errors
        )
        ctx.log(f"コマンドを実行できません: {reason}")
        return True
    try:
        commands = to_gcode(to_command(request), stage, lambda: ctx.machine)
    except ValueError as exc:
        ctx.log(f"コマンドを実行できません: {exc}")
        return True
    klipper.send_gcode(commands, timeout=COMMAND_TIMEOUT)
    return True


# --- ローディング ---

# ローディング段階の progress stage 名（ローディング欄のボタンを押せる段階）
LOADING_STAGE = "ローディング"
LOADING_DEFAULT_AMOUNT = 0.1
LOADING_DEFAULT_ROTATIONS = 5.0
LOADING_DEFAULT_ROTATION_RATE = 0.5
LOADING_DEFAULT_ROTATION_ACCEL = 0.5
LOADING_DEFAULT_RETRACT_ROTATIONS = 0.0


@attrs.frozen
class LoadingRotation:
    """ローディング欄の回転押出の既定値を持つ ParamSpec 名."""

    rotations: str
    rate: str
    accel: str
    retract_rotations: str


@attrs.frozen
class LoadingPanel:
    """ジョブ画面のローディング欄（:func:`loading_loop` へ command を送るボタン群）の宣言.

    Attributes:
        amount: 体積ローディング量の既定値を持つ ParamSpec 名
        rotation: 回転ローディングの既定値を持つ ParamSpec 名（None なら回転の欄を出さない）
        stages: ボタンを押せる progress の段階名
    """

    amount: str
    rotation: LoadingRotation | None = None
    stages: tuple[str, ...] = (LOADING_STAGE,)


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
    """有限な正の数値なら float、それ以外は None."""
    if not is_finite_number(value):
        return None
    return float(value) if value > 0 else None


def _non_negative_amount(value: object) -> float | None:
    """有限な非負（0 含む）の数値なら float、それ以外は None."""
    if not is_finite_number(value):
        return None
    return float(value) if value >= 0 else None


def drain_commands(ctx: JobContext) -> int:
    """滞留コマンドを破棄し、破棄した件数を返す（段階開始前のボタン/ジョグの遅延実行を防ぐ）."""
    drained = 0
    while ctx.next_command(timeout=0) is not None:
        drained += 1
    return drained


def loading_loop(
    ctx: JobContext,
    klipper: KlipperClient,
    stage: XYZStage,
    applicator: PasteApplicator,
) -> LoadingTotals:
    """ローディング段階の command 駆動ループを実行し、押出合計を返す.

    extrude / suck は ``applicator.load`` へ、回転は ``applicator.load_rotations`` へ、
    マシン操作コマンドは ``handle_machine_command`` へ委譲する。Finish で離脱する。

    Raises:
        JobAborted: 待機中に abort された場合
    """
    ctx.progress(LOADING_STAGE)

    drained = drain_commands(ctx)
    if drained:
        ctx.log(f"ローディング開始前のコマンド {drained} 件を破棄しました")

    ctx.log("押出 / 吸引ボタンでローディングし、終了ボタンで完了してください")
    ctx.notify_operator()
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
                applicator.load_rotations(
                    rotations, rate, accel, retract_rotations=retract_rotations
                )
                total_rotations += rotations
                ctx.log(
                    f"回転ローディング: {rotations:+.3f} rev "
                    f"@ {rate:.3f} rev/s, accel={accel:.3f} rev/s^2"
                    f"（累計 {total_rotations:+.3f} rev）"
                )
                if rotations > 0 and retract_rotations > 0:
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
                if not handle_machine_command(ctx, klipper, stage, command):
                    ctx.log(f"未知のコマンドです: {command.get('type')!r}")


# --- 塗布量校正のフィットと報告（収集ジョブと再フィットのジョブ） ---

# モンタージュに並べる塗布セルの数（直径順の端と中間を等間隔に選ぶ）。blank を 1 つ足す
_MONTAGE_DISPENSED = 3

# 既定値の出典を DotDetectionSpec ひとつにして、片方のジョブだけ既定値が変わることを防ぐ
_DETECTION_DEFAULTS = DotDetectionSpec()

DETECTION_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec(
        "min_contrast",
        "最小コントラスト",
        "float",
        default=_DETECTION_DEFAULTS.min_contrast,
        minimum=0.0,
        help=(
            "塗布前後の差分がこの値に届かないセルは直径 0 とします。"
            "blank を誤検出しないための下限です。"
        ),
    ),
    ParamSpec(
        "contrast_percentile",
        "コントラストの分位点",
        "float",
        default=_DETECTION_DEFAULTS.contrast_percentile,
        minimum=0.0,
        unit="%",
        help="差分のどの分位点を最小コントラストと比べるかです。",
    ),
    ParamSpec(
        "threshold_floor_ratio",
        "Otsu閾値の下限比",
        "float",
        default=_DETECTION_DEFAULTS.threshold_floor_ratio,
        minimum=0.0,
        help="2値化の閾値の下限を「最小コントラスト × この比」に置きます。",
    ),
    ParamSpec(
        "open_kernel_px",
        "openカーネル",
        "int",
        default=_DETECTION_DEFAULTS.open_kernel_px,
        minimum=0,
        unit="px",
        help="孤立点を落とすモルフォロジー openのサイズ（0か正の奇数）です。",
    ),
    ParamSpec(
        "min_area_px",
        "最小面積",
        "int",
        default=_DETECTION_DEFAULTS.min_area_px,
        minimum=1,
        unit="px",
        help="最大連結成分がこれ未満なら直径 0 とします。",
    ),
)

REQUIRE_BLANK_ZERO_PARAM = ParamSpec(
    "require_blank_zero",
    "blankの直径0を必須にする",
    "bool",
    default=True,
    help=(
        "blank セルで直径が 0 にならなければ校正を作りません。"
        "外すとハイパラ探索の途中経過も見られます。"
    ),
)

# 検出ハイパラの名前（persisted_params や検証で参照する）
DETECTION_PARAM_NAMES: tuple[str, ...] = tuple(spec.name for spec in DETECTION_PARAMS)


def _detection_spec_from_params(params: Mapping[str, ParamValue]) -> DotDetectionSpec:
    """ジョブパラメータを検出ハイパラへ写す."""
    return DotDetectionSpec(
        min_contrast=float(params["min_contrast"]),
        contrast_percentile=float(params["contrast_percentile"]),
        threshold_floor_ratio=float(params["threshold_floor_ratio"]),
        open_kernel_px=int(params["open_kernel_px"]),
        min_area_px=int(params["min_area_px"]),
    )


def validate_detection_params(params: Mapping[str, ParamValue]) -> str | None:
    """検出ハイパラの整合を先に確かめる（不正なら理由文）.

    収集の前と session 選択の前に呼ぶ。1 時間の収集や session の選択を終えてから 「open
    カーネルが偶数です」と言われても遅い。
    """
    return _detection_spec_from_params(params).validate()


def build_calibration(
    ctx: JobContext, session: DatasetSession
) -> tuple[CalibrationFit | None, str | None]:
    """Session を計測して校正へ組み立てる（検出ハイパラは ctx.params から）."""
    spec = _detection_spec_from_params(ctx.params)
    error = spec.validate()
    if error is not None:
        return None, error
    ctx.log(f"session {session.label} / {len(session.cells())} セルを計測します")
    return fit_session(
        session,
        spec=spec,
        require_blank_zero=bool(ctx.params["require_blank_zero"]),
    )


def report_calibration(
    ctx: JobContext,
    session: DatasetSession,
    fit: CalibrationFit,
    *,
    calibration_dir: Path,
    always_save: bool = False,
) -> tuple[str, tuple[Artifact, ...]]:
    """診断をログへ出し、校正を保存し、図を描く.

    Args:
        ctx: 実行中のジョブ
        session: 材料にした session
        fit: フィット結果
        calibration_dir: 校正の保存先
        always_save: ``save_name`` が空でも自動命名して保存するか

    Returns:
        ``(まとめの断片, artifact)``
    """
    for line in _diagnostic_lines(fit):
        ctx.log(line)

    # 校正ファイルを図より先に書く。図は診断の補助で、図の生成に失敗しても校正は成果として
    # 残したい（順序が逆だと matplotlib の失敗で校正が 1 件も残らない）
    saved = _save(ctx, fit, calibration_dir, always=always_save)
    artifacts = (
        () if saved is None else (ctx.artifact("校正ファイル", saved.name, "file"),)
    )

    ctx.progress("成果物の生成", None)
    figures, figure_error = _render_artifacts(ctx, session, fit)
    if figure_error is not None:
        ctx.log(f"診断図を作れませんでした（校正と診断は残っています）: {figure_error}")

    diagnostics = fit.calibration.diagnostics
    summary = (
        f"総体積誤差 {diagnostics.total_relative_error * 100:+.2f}% / "
        f"点ごと残差std {diagnostics.residual_relative_std * 100:.1f}% / "
        + ("保存なし（診断のみ）" if saved is None else f"保存先 {saved.name}")
        + ("" if figure_error is None else " / 診断図なし")
    )
    return summary, (*artifacts, *figures)


def _diagnostic_lines(fit: CalibrationFit) -> tuple[str, ...]:
    """診断をログ 1 行ずつへ整える（表示文字列はサーバー側で組む）."""
    diagnostics = fit.calibration.diagnostics
    model = fit.calibration.model
    return (
        f"被覆域 {model.diameter_min_mm:.3f}〜{model.diameter_max_mm:.3f} mm / "
        f"単調 {'はい' if diagnostics.monotonic_in_range else 'いいえ'}",
        f"blank誤検出 {diagnostics.blank_false_positive_count} 件 / "
        f"検出失敗 {diagnostics.detection_failure_count} 件",
        f"点ごと相対誤差 平均 {diagnostics.point_relative_mae * 100:.1f}% / "
        f"最大 {diagnostics.point_relative_max * 100:.1f}% / "
        f"std {diagnostics.residual_relative_std * 100:.1f}%",
        f"session総体積の相対誤差 {diagnostics.total_relative_error * 100:+.2f}%",
    )


def _render_artifacts(
    ctx: JobContext, session: DatasetSession, fit: CalibrationFit
) -> tuple[tuple[Artifact, ...], str | None]:
    """散布図と検出モンタージュを artifacts_dir へ描く（失敗は理由を返す）.

    図は診断の補助にすぎないので、描けなくても校正やジョブを失敗させない。

    検出ハイパラは ``fit.calibration.detection`` を使う。フィットに実際に使った
    spec がそこに埋まっているので、図と係数が食い違わない。
    """
    scatter = "paste_volume_calibration.png"
    montage = "paste_volume_detection.png"
    try:
        render_calibration_scatter(
            fit.cells,
            fit.calibration.model,
            f"{session.label} / diameter to volume",
            ctx.artifacts_dir / scatter,
        )
        render_detection_montage(
            _montage_panels(session, fit, fit.calibration.detection),
            f"{session.label} / detection",
            ctx.artifacts_dir / montage,
        )
    except Exception as error:
        logger.warning("診断図を作れませんでした", exc_info=True)
        return (), f"{type(error).__name__}: {error}"
    return (
        ctx.artifact("直径と体積の散布図", scatter, "image"),
        ctx.artifact("検出モンタージュ", montage, "image"),
    ), None


def _montage_panels(
    session: DatasetSession, fit: CalibrationFit, spec: DotDetectionSpec
) -> list[tuple[str, ImageArray, ImageArray, ImageArray]]:
    """大中小と blank の代表セルを pre / post / マスクで並べる材料を作る."""
    dispensed = sorted(
        (cell for cell in fit.cells if not cell.blank),
        key=lambda cell: cell.diameter.diameter_mm,
    )
    blanks = [cell for cell in fit.cells if cell.blank]
    chosen = _spread(dispensed, _MONTAGE_DISPENSED) + blanks[:1]

    panels: list[tuple[str, ImageArray, ImageArray, ImageArray]] = []
    views = {cell.index: cell for cell in session.cells()}
    for cell in chosen:
        view = views[cell.index].views[0]
        pre = _read(session, view.pre)
        post = _read(session, view.post)
        mask, error = detection_mask(pre, post, spec=spec)
        if mask is None:
            raise ValueError(f"セル{cell.index}: {error}")
        label = (
            "blank"
            if cell.blank
            else f"#{cell.index} {cell.diameter.diameter_mm:.2f}mm"
        )
        panels.append((label, pre, post, mask))
    return panels


def _spread(cells: list[CellMeasurement], count: int) -> list[CellMeasurement]:
    """直径順に並んだセルから、端と中間を等間隔に選ぶ."""
    if count <= 1:
        return list(cells[:count])
    if len(cells) <= count:
        return list(cells)
    step = (len(cells) - 1) / (count - 1)
    return [cells[round(index * step)] for index in range(count)]


def _read(session: DatasetSession, relative: str) -> ImageArray:
    """Session の相対 path から画像を読む（読めなければ ValueError）."""
    path, error = session.image_path(relative)
    if path is None:
        raise ValueError(error)
    image = cv2.imread(str(path))
    if image is None:
        raise ValueError(f"画像を読めません: {relative}")
    return image


def _save(
    ctx: JobContext, fit: CalibrationFit, calibration_dir: Path, *, always: bool
) -> Path | None:
    """校正ファイルを保存する.

    Args:
        ctx: 実行中のジョブ
        fit: 保存する校正
        calibration_dir: 校正の保存先
        always: ``save_name`` が空でも自動命名して保存するか（収集ジョブは True）

    Returns:
        保存先（保存しなかったときは ``None``）
    """
    name = str(ctx.params.get("save_name", "")).strip()
    if not name and not always:
        ctx.log("保存名が空なので校正ファイルは保存しません（診断のみ）")
        return None
    # 自動命名のラベルは既に生成時刻を含むので、名前へ時刻を重ねない
    path = (
        calibration_path(calibration_dir, name)
        if name
        else auto_calibration_path(calibration_dir, fit.calibration.label)
    )
    if path.exists():
        ctx.log(f"同名の校正ファイルを上書きします: {path.name}")
    write_calibration(path, fit.calibration)
    ctx.log(f"校正ファイルを保存しました: {path}")
    return path
