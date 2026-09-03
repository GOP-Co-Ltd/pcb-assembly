"""吐出量キャリブレーション統合ジョブ（①②③ をメニュー駆動で回す）."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import attrs

from pcbasm import gcode
from pcbasm.geometry import (
    Compose,
    Transform,
)
from pcbasm.pasting.applicator import (
    PasteApplicator,
)
from pcbasm.pasting.calibration import FlowCalibrationSet
from pcbasm.pasting.dispense_calibration import (
    DispenseRateCalibration,
    FillSpeedSweep,
    LineLayout,
    LineLayoutOverflowError,
    RateMeasurement,
    RotationsPerUlRound,
    dispense_rate_schedule,
    fill_speed_schedule,
    rate_sweep_amount,
    slot_area,
)
from pcbasm.pasting.session import PasteSession
from pcbasm.pcb import (
    Copper,
    Layer,
)
from web.api.jobs.board_ops import setup_board
from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.context import (
    JobContext,
    JobResult,
    PromptSpec,
)
from web.api.jobs.machine_commands import handle_machine_command
from web.api.jobs.pasting.common import (
    CalibrationCancelled,
    Extrude,
    Finish,
    InvalidLoadingCommand,
    apply_to_machine_toml,
    drain_commands,
    parse_loading_command,
    prompt_confirm,
    prompt_mass,
    prompt_positive_number,
    run_loading_loop,
)

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


def register(catalog: JobCatalog) -> None:
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
        self._applicator = session.make_applicator()
        self._applicator.__enter__()

    @property
    def session(self) -> PasteSession:
        return self._session

    @property
    def transform(self) -> Transform:
        """銅板 board 座標 → 機械座標（高さ面込み）."""
        return self._transform

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
            rotations_per_ul=rotations_per_ul
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
    drain_commands(ctx)
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
        except CalibrationCancelled:
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
        CalibrationCancelled: 線が銅板の描画領域に収まらない場合
    """
    try:
        return _build_line_layout(ctx, line_count)
    except LineLayoutOverflowError as exc:
        ctx.log(_layout_overflow_message(exc))
        raise CalibrationCancelled() from exc


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
    ``CalibrationCancelled`` でメニューへ戻る。

    Returns:
        入力された質量 (mg)
    """
    prompt_confirm(ctx, tare_message)
    draw()
    _move_to_removal_z(ctx, calib)
    return prompt_mass(ctx, mass_message)


def _layout_for_sweep(
    ctx: JobContext, points: Sequence[float], empty_message: str
) -> LineLayout | None:
    """掃引点ごとに専用の線位置を確保したレイアウトを作る.

    掃引列が空なら ``empty_message`` を log して None（呼び出し側がスキップ）。
    line_count に関係なく掃引点数ぶんの線位置を取り、重ね書きをしない。

    Raises:
        CalibrationCancelled: 線が銅板の描画領域に収まらない場合
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
        run_loading_loop(
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
                calib.applicator.draw_line(
                    start, end, amount_ul=amount, transform=calib.transform
                )
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
        apply_to_machine_toml(
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
                start,
                end,
                amount_ul=amount,
                transform=calib.transform,
                fill_speed=fill_speed,
                rate_cap=rate,
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

    chosen = prompt_positive_number(
        ctx, "採用する max_dispense_rate (uL/s) を入力", default=auto
    )
    assert chosen is not None  # cancel_label 無しの prompt は常に正数を返す
    apply_to_machine_toml(ctx, {"paste_dispenser.max_dispense_rate": chosen})
    return attrs.evolve(results, max_dispense_rate=chosen)


def _calibrate_max_fill_speed(
    ctx: JobContext,
    calib: _CalibrationContext,
    results: _DispenseCalibrationResults,
) -> _DispenseCalibrationResults:
    """③ max_fill_speed を速度スイープの目視選択で確定する.

    速度列の各点で線を引き（実塗布同等の総量を固定）、番号→速度の対応を全 log
    する。各線は ``draw_line(fill_speed=v, rate_cap=inf)`` で引き、実効塗布速度を
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
            start,
            end,
            amount_ul=total_amount,
            transform=calib.transform,
            fill_speed=speed,
            rate_cap=math.inf,
        ).fill_speed
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
    apply_to_machine_toml(ctx, {"paste_dispenser.max_fill_speed": chosen})
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
