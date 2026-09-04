"""吐出量キャリブレーション統合ジョブ（①②③ をメニュー駆動で回す）.

数理・レイアウト・機械手順は :mod:`pcbasm.pasting.flowcalib` に置き、ここは
メニュー / prompt / 進捗 / 中断とラウンドの状態機械だけを担う。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import attrs

from pcbasm.pasting.flowcalib.flow import (
    CONVERGENCE_REL_TOL,
    DispenseRateCalibration,
    RateMeasurement,
    commanded_rotations,
    rotations_per_ul_round,
)
from pcbasm.pasting.flowcalib.lines import (
    LineLayout,
    plan_rate_sweep,
    plan_speed_sweep,
)
from pcbasm.pasting.flowcalib.params import CalibrationParams
from pcbasm.pasting.flowcalib.procedure import FlowCalibrationProcedure
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
# ③max_fill_speed をメニュー駆動で順次/個別に回す）の progress stage 名。
# CALIBRATION_MENU_STAGE は calibration_menu.html の data-calib-stage / calibration_menu.js
# と一致させる契約値。
CALIBRATION_MENU_STAGE = "キャリブレーションメニュー"
# ParamSpec 既定値の唯一の出典（pcbasm 側）
_DEFAULTS = CalibrationParams()


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
                    _DEFAULTS.board_width,
                    unit="mm",
                ),
                ParamSpec(
                    "board_height",
                    "銅板高さ",
                    "float",
                    _DEFAULTS.board_height,
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
                    _DEFAULTS.line_length,
                    unit="mm",
                    runtime_editable=True,
                ),
                ParamSpec(
                    "line_count",
                    "線の本数",
                    "int",
                    _DEFAULTS.line_count,
                    unit="本",
                    help="① 専用。②③ は分割数ぶんの線を配置",
                    runtime_editable=True,
                ),
                ParamSpec(
                    "line_amount",
                    "1 線あたりの塗布量",
                    "float",
                    _DEFAULTS.line_amount_ul,
                    unit="uL",
                    help="① 専用。② はレート × 線長 / 速度、③ は面積換算で導出",
                    runtime_editable=True,
                ),
                ParamSpec(
                    "row_pitch",
                    "線の段ずらし間隔",
                    "float",
                    _DEFAULTS.row_pitch,
                    unit="mm",
                    runtime_editable=True,
                ),
                # 計量退避（実行中変更可）。退避 Z = max(z_min, z_max - offset)。
                # 負 offset は退避 Z がはみ出るため minimum=0.0 で拒否する。
                ParamSpec(
                    "removal_z_offset",
                    "計量退避 Z オフセット",
                    "float",
                    _DEFAULTS.removal_z_offset,
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
                    _DEFAULTS.rate_min,
                    unit="uL/s",
                    runtime_editable=True,
                ),
                ParamSpec(
                    "rate_max",
                    "吐出レート最大",
                    "float",
                    _DEFAULTS.rate_max,
                    unit="uL/s",
                    runtime_editable=True,
                ),
                ParamSpec(
                    "rate_divisions",
                    "吐出レート分割数",
                    "int",
                    _DEFAULTS.rate_divisions,
                    runtime_editable=True,
                ),
                # ③ max_fill_speed（連続最大速度・目視選択・実行中変更可）
                ParamSpec(
                    "speed_min",
                    "塗布速度最小",
                    "float",
                    _DEFAULTS.speed_min,
                    unit="mm/s",
                    runtime_editable=True,
                ),
                ParamSpec(
                    "speed_max",
                    "塗布速度最大",
                    "float",
                    _DEFAULTS.speed_max,
                    unit="mm/s",
                    runtime_editable=True,
                ),
                ParamSpec(
                    "speed_divisions",
                    "塗布速度分割数",
                    "int",
                    _DEFAULTS.speed_divisions,
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


def _run_dispense_calibration(ctx: JobContext) -> JobResult:
    """銅板に線を引いて ①②③ を検証ループまで回す統合キャリブレーション.

    共通土台（その場生成した矩形銅板 → ボード計測 → 平面計測 → applicator）を
    :class:`FlowCalibrationProcedure` として確立し、メニュー（``run_calib`` コマンド）で
    ① rotations_per_ul / ② max_dispense_rate / ③ max_fill_speed を順次/個別に実行する。
    各キャリブの確定値は採用時点で machine.toml へ即時反映する（中止・失敗でも失われない）。

    算出/判定はすべて ``pcbasm.pasting.flowcalib`` に委譲し、ここはループ制御と入出力に徹する。
    """
    params = _params(ctx)
    tolerance = float(ctx.params["tolerance"])

    # 開始時点の設定で ①（line_count 本）②（レート掃引点数）③（速度掃引点数）の
    # いずれかが銅板に収まらない場合はセットアップ前に失敗させる
    # （線パラメータは実行中変更可のため、各キャリブ開始時にも再検証する）。
    message = params.line_layout(params.required_line_count).validate()
    if message is not None:
        raise ValueError(message)

    with ctx.open_camera() as camera:
        pcb_path = _generate_calibration_board(
            ctx, params.board_width, params.board_height
        )
        result = setup_board(ctx, camera, tolerance=tolerance, pcb_path=pcb_path)
        ctx.progress("高さ計測")
        with FlowCalibrationProcedure.setup(result) as procedure:
            results = _calibration_menu_loop(ctx, procedure)

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
    ctx: JobContext, procedure: FlowCalibrationProcedure
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
            _handle_menu_loading_or_machine(ctx, procedure, command)
            continue

        if which == "finish":
            ctx.log("吐出量キャリブレーションを終了します")
            return results
        # サブキャリブ中の「中止」はメニューへ戻る（誤選択のやり直し）。all 実行中の
        # 中止は以降のサブキャリブをスキップしてメニューへ。JobAborted は捕捉しない。
        try:
            if which in ("rotations_per_ul", "all"):
                results = _calibrate_rotations_per_ul(ctx, procedure, results)
                if results.finish:  # ① の「採用して終了」でジョブ全体を終了
                    ctx.log("吐出量キャリブレーションを終了します")
                    return results
            if which in ("max_dispense_rate", "all"):
                results = _calibrate_max_dispense_rate(ctx, procedure, results)
            if which in ("max_fill_speed", "all"):
                results = _calibrate_max_fill_speed(ctx, procedure, results)
        except CalibrationCancelled:
            ctx.log("キャリブレーションを中止しました。メニューへ戻ります")
        ctx.progress(CALIBRATION_MENU_STAGE)
        ctx.log("メニューに戻りました。次のキャリブを選ぶか終了してください")


def _handle_menu_loading_or_machine(
    ctx: JobContext, procedure: FlowCalibrationProcedure, command: Mapping[str, Any]
) -> None:
    """メニュー段階の非 run_calib コマンドを処理する.

    プライム用の押出/吸引（``extrude`` / ``suck``）は applicator へ、マシン操作は
    ``handle_machine_command`` へ委譲する。loading_controls の終了ボタン
    （``finish``）は誤操作ガードとして無視する（終了はメニューの終了ボタンを使う）。
    """
    action = parse_loading_command(command)
    match action:
        case Extrude(amount=amount):
            procedure.applicator.load(amount)
            ctx.log(f"プライム押出: {amount:+.3f} uL")
        case Finish():
            ctx.log("終了はメニューの「終了」ボタンを使ってください")
        case InvalidLoadingCommand(reason=reason):
            ctx.log(reason)
        case _:
            klipper = procedure.session.klipper
            stage = procedure.session.stage
            if not handle_machine_command(ctx, klipper, stage, command, focus_z=None):
                ctx.log(f"未知のコマンドです: {command.get('type')!r}")


def _params(ctx: JobContext) -> CalibrationParams:
    """線設定・掃引範囲は実行中変更可のため、使う直前に ``ctx.params`` から読み直す."""
    return CalibrationParams.from_mapping(ctx.params)


def _line_layout(ctx: JobContext, params: CalibrationParams) -> LineLayout:
    """① 用のレイアウトを構築する（収まらなければメニューへ戻す）.

    線パラメータは実行中変更可のため、超過時は文言を log してメニューへ
    戻し、調整して再実行できるようにする。

    Raises:
        CalibrationCancelled: 線が銅板の描画領域に収まらない場合
    """
    layout = params.line_layout()
    if (message := layout.validate()) is not None:
        ctx.log(message)
        raise CalibrationCancelled()
    return layout


def _weighed_draw(
    ctx: JobContext,
    procedure: FlowCalibrationProcedure,
    params: CalibrationParams,
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
    ctx.log(
        f"ヘッドを退避 Z={procedure.removal_z(params.removal_z_offset):.3f} へ移動します。"
        "基板を取り出して計測してください"
    )
    procedure.move_to_removal_z(params.removal_z_offset)
    return prompt_mass(ctx, mass_message)


def _calibrate_rotations_per_ul(
    ctx: JobContext,
    procedure: FlowCalibrationProcedure,
    results: _DispenseCalibrationResults,
) -> _DispenseCalibrationResults:
    """① rotations_per_ul をローディング → 線引き → 計量 → 採用/再計測ループで確定する.

    各ラウンドの先頭でヘッドを Z=0 に上げてプライム/ふき取り（専用ローディング段階）を
    行い、電子天秤にセットしてタール（ゼロ）してから ``line_count`` 本の線を段ずらしで
    引く。線引き後はヘッドを退避 Z（z_max − removal_z_offset、既定は全退避）へ上げ、
    基板を取り出して計量しやすくする。合計質量から :func:`rotations_per_ul_round` で
    新 ``rotations_per_ul`` を算出する。採用すると新値で applicator を作り直し、
    ``dispense_accel`` も回転加速度を保って連動更新する。採用時点で両値を machine.toml へ
    即時反映するため、以降の中止・失敗でも計測結果は失われない。収束（前後の相対差が
    許容内）はヒントとして表示するのみで、ループ継続はユーザー判断。タール前の中止・
    質量入力の中止はいずれもメニューへ戻る。
    """
    # 密度はマシン設定 (solder_paste_density [mg/uL]) を真実とする。② と同じ扱い。
    density = ctx.machine.paste_dispenser.density_mg_per_ul
    ctx.log(
        f"ペースト密度（machine.toml の solder_paste_density）= {density:.3f} mg/uL"
    )

    while True:
        # 線設定・塗布量は実行中変更可。ラウンド先頭で読み直し次ラウンドから反映する。
        params = _params(ctx)
        layout = _line_layout(ctx, params)
        amount = params.line_amount_ul
        # ── 専用ローディング段階：ヘッドを Z=0 に上げてプライム/ふき取り ──
        # Z=0 へ上げることでローディング中のノズルふき取りがしやすくなる。
        ctx.log("ヘッドを Z=0 に上げます。プライム/ふき取りをしてください")
        procedure.move_to_loading_z()
        run_loading_loop(
            ctx,
            procedure.session.klipper,
            procedure.session.stage,
            procedure.applicator,
        )

        previous_rpu = procedure.rotations_per_ul
        rotations_used = commanded_rotations(
            line_count=layout.line_count,
            amount_ul=amount,
            rotations_per_ul=previous_rpu,
        )

        def checkpoint(index: int) -> None:
            ctx.checkpoint()
            ctx.log(f"線 {index + 1}/{layout.line_count} を {amount:.3f} uL で塗布")

        def draw() -> None:
            ctx.progress("① rotations_per_ul: 線引き")
            procedure.draw_lines(layout.lines, amount_ul=amount, checkpoint=checkpoint)

        # 電子天秤にセットしてタール（ゼロ）→ 線引き → 退避 → 計量の 1 ラウンド
        # （タール前・質量入力の中止はいずれもメニューへ戻る）。
        mass = _weighed_draw(
            ctx,
            procedure,
            params,
            tare_message=(
                "基板を電子天秤に載せてタール（ゼロ）し、基板を装置へ戻してから"
                "続行を押してください。続行すると線引きへ進みます。"
            ),
            mass_message=(
                f"基板を取り出して計量し、{layout.line_count} 本の線の合計質量 (mg) "
                f"を入力（回転数 {rotations_used:.4f} rev 相当）"
            ),
            draw=draw,
        )

        round_ = rotations_per_ul_round(
            mass_mg=mass,
            line_count=layout.line_count,
            amount_ul=amount,
            previous_rotations_per_ul=previous_rpu,
            previous_dispense_accel=procedure.dispense_accel,
            density_mg_per_ul=density,
        )
        ctx.log(
            f"算出 rotations_per_ul = {round_.computed:.6f} rev/uL "
            f"(前回 {previous_rpu:.6f}, 相対変化 {round_.relative_change * 100:.2f}%)"
        )
        ctx.log(f"連動 dispense_accel = {round_.dispense_accel:.6f} uL/s^2")
        if round_.converged():
            ctx.log(f"相対変化が許容 {CONVERGENCE_REL_TOL * 100:.0f}% 以内です（収束）")

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
                "paste_dispenser.rotations_per_ul": round_.computed,
                "paste_dispenser.dispense_accel": round_.dispense_accel,
            },
        )
        adopted = attrs.evolve(
            results,
            rotations_per_ul=round_.computed,
            dispense_accel=round_.dispense_accel,
        )
        if choice == "採用して吐出量キャリブレーションを終了する":
            # 再描画しないので applicator の作り直しは不要。finish でジョブを終了。
            return attrs.evolve(adopted, finish=True)
        # 「再計測」「他のキャリブへ進む」は新値で applicator を作り直す。
        procedure.adopt(round_)
        ctx.log("新 rotations_per_ul で applicator を再構成しました")
        if choice == "採用して再計測する":
            continue
        return adopted  # 採用して他のキャリブレーションへ進む


def _calibrate_max_dispense_rate(
    ctx: JobContext,
    procedure: FlowCalibrationProcedure,
    results: _DispenseCalibrationResults,
) -> _DispenseCalibrationResults:
    """② max_dispense_rate を吐出効率の落ち検出で確定する.

    レート列の各点で線を引いて計量し、効率 ``measured_ul / commanded_ul`` の
    落ちから ``DispenseRateCalibration`` が ``max_dispense_rate`` を判定する。
    移動速度（``max_fill_speed``）は固定し、吐出量をレートに比例させて指令レートを
    実現する（:func:`plan_rate_sweep`。``line_amount`` は使わない）。各点は独立計測のため、
    線引き前に基板ごとタール（ゼロ）を確認してから引き、線引き後に退避 Z
    （z_max − removal_z_offset）へ上げて質量を入力させる。タール／質量入力の
    中止はいずれもメニューへ戻る。自動判定値を default に手動上書き可能で、
    確定値は machine.toml へ即時反映する。

    Raises:
        CalibrationCancelled: レート列が空、または線が銅板に収まらない場合
    """
    fill_speed = ctx.machine.paste_dispenser.max_fill_speed
    density = ctx.machine.paste_dispenser.density_mg_per_ul
    params = _params(ctx)

    points, message = plan_rate_sweep(params, fill_speed=fill_speed)
    if points is None:
        ctx.log(message or "")
        raise CalibrationCancelled()

    ctx.log(
        f"移動速度 {fill_speed:.3f} mm/s 固定・"
        "吐出量 = レート × 線長 / 速度 でレートを掃引します"
    )
    measurements: list[RateMeasurement] = []
    # baseline まで引き戻してから引き始める（draw_line が retract を内包するので
    # 線間は不要。retract は相対移動なので点ごとの draw_lines では省く）
    procedure.applicator.retract()
    for point in points:
        ctx.progress("② max_dispense_rate: 線引き", 100.0 * point.index / len(points))
        ctx.checkpoint()

        def draw() -> None:
            procedure.draw_lines(
                [(point.start, point.end)],
                amount_ul=point.amount_ul,
                fill_speed=fill_speed,
                rate_cap=point.rate,
                retract=False,
            )

        # 各レートは独立計測。タール（ゼロ）→ 線引き → 退避 → 計量の 1 ラウンド
        # （中止でメニューへ）。
        position = f"[{point.index + 1}/{len(points)}]"
        mass = _weighed_draw(
            ctx,
            procedure,
            params,
            tare_message=(
                f"{position} 基板を電子天秤に載せてタール（ゼロ）し、"
                "基板を装置へ戻してから続行を押してください。"
                f"続行するとレート {point.rate:.3f} uL/s（吐出量 {point.amount_ul:.3f} uL）の"
                "線引きへ進みます"
            ),
            mass_message=(
                f"{position} 基板を取り出して計量し、"
                f"レート {point.rate:.3f} uL/s の線の質量 (mg) を入力"
            ),
            draw=draw,
        )
        measurement = RateMeasurement(
            rate=point.rate, measured_ul=mass / density, commanded_ul=point.amount_ul
        )
        measurements.append(measurement)
        ctx.log(
            f"レート {point.rate:.3f} uL/s: 実測 {measurement.measured_ul:.4f} uL / "
            f"指令 {point.amount_ul:.4f} uL → 効率 {measurement.efficiency:.3f}"
        )

    calibration = DispenseRateCalibration(measurements=tuple(measurements))
    auto = calibration.max_dispense_rate
    if auto is None:
        baseline = calibration.baseline_efficiency
        baseline_text = f"{baseline:.3f}" if baseline is not None else "-"
        ctx.log(
            "効率の落ちを自動判定できませんでした"
            f"（baseline 効率 {baseline_text}）。"
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
    procedure: FlowCalibrationProcedure,
    results: _DispenseCalibrationResults,
) -> _DispenseCalibrationResults:
    """③ max_fill_speed を速度スイープの目視選択で確定する.

    速度列の各点で線を引き（実塗布同等の総量を固定。:func:`plan_speed_sweep`）、
    番号→速度の対応を全 log する。各線は ``fill_speed=v, rate_cap=inf`` で引き、実効塗布速度を
    v 自体に固定する。``max_fill_speed`` を per-line に上書きするのは、③ が config の
    現行値を超える速度域まで測る必要があるため（既定値で頭打ちさせない）。``rate_cap``
    を無効化するのは、q 非依存の移動速度限界を測る目的上、吐出レート上限で move 速度を
    律速させないため。連続して綺麗に引けた最大の番号を choice prompt で選び、
    その点の速度で ``max_fill_speed`` を確定する。

    Raises:
        CalibrationCancelled: 速度列が空、または線が銅板に収まらない場合
    """
    dispenser = ctx.machine.paste_dispenser
    params = _params(ctx)

    sweep, message = plan_speed_sweep(
        params,
        ul_per_mm2=dispenser.ul_per_mm2,
        bead_width=dispenser.nozzle_diameter * dispenser.bead_width_factor,
    )
    if sweep is None:
        ctx.log(message or "")
        raise CalibrationCancelled()
    points = sweep.points

    def checkpoint(index: int) -> None:
        ctx.progress("③ max_fill_speed: 線引き", 100.0 * index / len(points))
        ctx.checkpoint()

    executions = procedure.draw_lines(
        [(p.start, p.end) for p in points],
        amount_ul=sweep.total_amount_ul,
        fill_speed=[p.fill_speed for p in points],
        rate_cap=math.inf,
        checkpoint=checkpoint,
    )
    max_velocity = procedure.session.stage.max_velocity
    for point, execution in zip(points, executions, strict=True):
        actual = execution.fill_speed
        actual_text = (
            f" / 実効 {actual.resolve(max_velocity):.2f} mm/s"
            if actual is not None
            else ""
        )
        ctx.log(f"番号 {point.index}: 速度 {point.fill_speed:.2f} mm/s{actual_text}")

    selection = ctx.prompt(
        PromptSpec(
            kind="choice",
            message="連続して綺麗に引けた最大の番号を選んでください",
            choices=tuple(f"{p.index}: {p.fill_speed:.2f} mm/s" for p in points),
            default=f"{points[0].index}: {points[0].fill_speed:.2f} mm/s",
        )
    )
    chosen = points[int(str(selection).split(":", 1)[0])].fill_speed
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
