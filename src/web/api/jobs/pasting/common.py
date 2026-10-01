"""Pasting タブのジョブが共有する WS コマンド解釈・prompt・ワークフロー駆動.

ドメイン手順（高さ計測・位置合わせ・pad 別変換・塗布）は ``pcbasm.pasting`` に置き、
ここは ``JobContext`` を使う対話（prompt / progress / log / checkpoint）とループ制御だけを担う。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import attrs

from pcbasm.hal import Klipper, XYZStage
from pcbasm.pasting.alignment import (
    PadRefinement,
    PasteCorrection,
    refine_pad,
    refinement_targets,
)
from pcbasm.pasting.applicator import PasteApplicator
from pcbasm.pasting.session import PasteSession
from pcbasm.pasting.settings import PasteSettingsModel
from pcbasm.pcb import Pad, PadHierarchy
from pcbasm.posctrl import (
    BoardAlignment,
    BoardCalibrationResult,
    RegionAlignmentSession,
)
from pcbasm.utils import is_finite_number
from web.api.jobs.board_ops import align_regions
from web.api.jobs.context import JobContext, PromptSpec
from web.api.jobs.machine_commands import handle_machine_command

# ローディングフェーズの progress stage 名
# （loading_controls.html の data 属性・テストでピンする契約値）
LOADING_STAGE = "ローディング"
LOADING_DEFAULT_AMOUNT = 0.1
LOADING_DEFAULT_ROTATIONS = 5.0
LOADING_DEFAULT_ROTATION_RATE = 0.5
LOADING_DEFAULT_ROTATION_ACCEL = 0.5
LOADING_DEFAULT_RETRACT_ROTATIONS = 0.0
APPLY_DIGITS = 6


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


def run_loading_loop(
    ctx: JobContext,
    klipper: Klipper,
    stage: XYZStage,
    applicator: PasteApplicator,
    *,
    focus_z: float | None = None,
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
                if not handle_machine_command(
                    ctx, klipper, stage, command, focus_z=focus_z
                ):
                    ctx.log(f"未知のコマンドです: {command.get('type')!r}")


def prompt_positive_number(
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


class CalibrationCancelled(Exception):
    """サブキャリブの中止要求。メニューループが捕捉してメニューへ戻す.

    ジョブ全体を終了する ``JobAborted`` とは異なり、こちらはメニューへ戻るだけ。
    多段ループ越しに None/False を手で伝播させる代わりに、既知ハンドラ（メニュー
    ループ）への制御フローとして例外を使う。
    """


def prompt_confirm(
    ctx: JobContext,
    message: str,
    *,
    true_label: str = "続行",
    cancel_label: str = "中止",
    default: bool = True,
) -> None:
    """続行 / 中止の confirm を出す。中止なら ``CalibrationCancelled`` を送出する."""
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
        raise CalibrationCancelled


def prompt_mass(
    ctx: JobContext, message: str, *, default: float | None = None
) -> float:
    """質量 (mg) を入力させる。中止なら ``CalibrationCancelled`` を送出する."""
    mass = prompt_positive_number(ctx, message, default=default, cancel_label="中止")
    if mass is None:
        raise CalibrationCancelled
    return mass


def apply_to_machine_toml(ctx: JobContext, values: Mapping[str, float]) -> None:
    """確定したキャリブ値を machine.toml へ即時反映し、内容を log する.

    採用のたびに書き込むことで、以降の中止・失敗でも計測結果を失わない。
    """
    rounded = {key: round(value, APPLY_DIGITS) for key, value in values.items()}
    ctx.apply_machine_settings(rounded)
    pairs = " / ".join(
        f"{key.rsplit('.', 1)[1]} = {value:.6f}" for key, value in rounded.items()
    )
    ctx.log(f"machine.toml へ反映しました: {pairs}")


def resolve_paste_model(ctx: JobContext, hierarchy: PadHierarchy) -> PasteSettingsModel:
    """基板設定ストア（あれば）から塗布設定モデルを取得する.

    ストア／PCB が未配線なら ``machine.toml`` の ``[paste_dispenser]`` を L0 デフォルトに
    据えた全 pad 有効のモデルを返す。
    """
    if ctx.board_store is not None and ctx.source_pcb is not None:
        return ctx.board_store.load_or_init(
            ctx.source_pcb,
            ctx.machine.paste_dispenser,
            board_signature=hierarchy.signature(),
        )
    return PasteSettingsModel.from_config(ctx.machine.paste_dispenser)


@attrs.frozen
class PreparedPasteWorkflow:
    """通常塗布と dataset 収集が共有する計測・位置合わせ結果とその要約."""

    session: PasteSession
    alignment_session: RegionAlignmentSession
    correction: PasteCorrection
    aligned_count: int
    region_count: int
    refinement_success_count: int
    refinement_target_count: int

    def summary(self) -> str:
        return (
            f"照合成功 {self.aligned_count}/{self.region_count} 領域 / "
            f"pad中心照合 {self.refinement_success_count}/"
            f"{self.refinement_target_count} pads"
        )


def prepare_paste_workflow(
    ctx: JobContext,
    result: BoardCalibrationResult,
    *,
    alignment_pads: Sequence[Pad],
) -> PreparedPasteWorkflow:
    """高さ計測 → 銅箔領域照合 → pad 中心精密照合を通し、pad 別変換の材料を揃える."""
    session = PasteSession.from_calibration(result)

    ctx.progress("高さ計測")
    height_plane = session.measure_height_plane()

    alignment_session = session.alignment_session(frame_sink=ctx.frame)
    regions = alignment_session.plan_regions([pad.center for pad in alignment_pads])
    ctx.log(f"照合対象の領域数: {len(regions)}")
    aligned = align_regions(ctx, alignment_session, regions)
    alignment = BoardAlignment(results=tuple(aligned))
    _check_all_pads_correctable(alignment, alignment_pads)
    ctx.log(f"位置合わせ成功: {len(aligned)}/{len(regions)} 領域")

    max_short_side = result.machine.paste_dispenser.pad_align.refine_max_short_side
    targets = refinement_targets(alignment_pads, max_short_side_mm=max_short_side)
    ctx.log(
        f"pad中心照合対象: {len(targets)}/{len(alignment_pads)} pads "
        f"(最大短辺 {max_short_side:g} mm)"
    )
    refinements: list[PadRefinement] = []
    for index, pad in enumerate(targets):
        ctx.progress("pad照合", 100.0 * index / len(targets))
        ctx.checkpoint()
        refinement = refine_pad(
            alignment_session,
            board_transform=session.board_transform,
            alignment=alignment,
            pad=pad,
        )
        if refinement.result is None or refinement.residual is None:
            ctx.log(
                f"警告: {pad.designator}.{pad.pad_number} のpad中心照合が"
                "収束しないため領域補正を使用"
            )
            continue
        ctx.log(
            f"{pad.designator}.{pad.pad_number}: "
            f"residual=({refinement.residual.x:+.4f}, {refinement.residual.y:+.4f}) mm, "
            f"passes={refinement.result.passes}"
        )
        refinements.append(refinement)
    alignment = BoardAlignment(
        results=tuple(r.result for r in refinements if r.result is not None),
        fallback_results=tuple(aligned),
    )
    ctx.log(f"pad中心照合成功: {len(refinements)}/{len(targets)} pads")
    _check_all_pads_correctable(alignment, alignment_pads)

    return PreparedPasteWorkflow(
        session=session,
        alignment_session=alignment_session,
        correction=PasteCorrection(alignment=alignment, height_plane=height_plane),
        aligned_count=len(aligned),
        region_count=len(regions),
        refinement_success_count=len(refinements),
        refinement_target_count=len(targets),
    )


def _check_all_pads_correctable(alignment: BoardAlignment, pads: Sequence[Pad]) -> None:
    """塗布前に全 pad の補正が求まることを確かめる（求まらなければ ValueError）."""
    for pad in pads:
        alignment.correction_for(pad.center, designator=pad.designator)
