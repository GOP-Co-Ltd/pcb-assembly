"""塗布量校正のジョブ 2 つで共有する ParamSpec と、フィット〜成果物の組み立て.

校正の生成（`paste_volume_calibration`。収集の最後に作る）と再フィット
（`paste_volume_refit`。既存 session からハイパラを変えて作り直す）は、材料の
入手経路だけが違って、計測 → フィット → 診断 → 保存はまったく同じ。

既定値の出典を :class:`DotDetectionSpec` ひとつにして、片方だけ既定が動くことを
防ぐ。判定とフィットそのものは :mod:`pcbasm.pasting.paste_volume.fit`、図は
:mod:`pcbasm.visualization.paste_volume_render` にあり、ここは成果物への変換と
保存の可否だけを担う。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

import cv2

from pcbasm.pasting.dataset.reader import DatasetSession
from pcbasm.pasting.paste_volume.calibration import (
    calibration_path,
    write_calibration,
)
from pcbasm.pasting.paste_volume.detect import DotDetectionSpec, detection_mask
from pcbasm.pasting.paste_volume.fit import CalibrationFit, CellMeasurement, fit_session
from pcbasm.vision.image import ImageArray
from pcbasm.visualization.paste_volume_render import (
    render_calibration_scatter,
    render_detection_montage,
)
from web.api.jobs.catalog import ParamSpec
from web.api.jobs.context import Artifact, JobContext, JobResult, ParamValue

logger = logging.getLogger(__name__)

# モンタージュに並べる塗布セルの数（直径順の端と中間を等間隔に選ぶ）。blank を 1 つ足す
_MONTAGE_DISPENSED = 3

_DEFAULTS = DotDetectionSpec()

DETECTION_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec(
        "min_contrast",
        "最小コントラスト",
        "float",
        default=_DEFAULTS.min_contrast,
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
        default=_DEFAULTS.contrast_percentile,
        minimum=0.0,
        unit="%",
        help="差分のどの分位点を最小コントラストと比べるかです。",
    ),
    ParamSpec(
        "threshold_floor_ratio",
        "Otsu閾値の下限比",
        "float",
        default=_DEFAULTS.threshold_floor_ratio,
        minimum=0.0,
        help="2値化の閾値の下限を「最小コントラスト × この比」に置きます。",
    ),
    ParamSpec(
        "open_kernel_px",
        "openカーネル",
        "int",
        default=_DEFAULTS.open_kernel_px,
        minimum=0,
        unit="px",
        help="孤立点を落とすモルフォロジー openのサイズ（0か正の奇数）です。",
    ),
    ParamSpec(
        "min_area_px",
        "最小面積",
        "int",
        default=_DEFAULTS.min_area_px,
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

# 再フィット用。ハイパラ探索の途中経過を見るために「保存しない」を選べる
SAVE_NAME_PARAM = ParamSpec(
    "save_name",
    "校正の保存名",
    "str",
    default="",
    optional=True,
    help=(
        "空なら保存せず診断だけを出します。"
        "指定するとその名前で校正ファイルを保存します。"
    ),
)

# 収集用。1 時間かけた実行の主成果物なので「保存しない」は選ばせず、名前の上書き
# だけを任意にする（空ならペースト・ノズル径・塗布高さから自動命名する）
COLLECTED_SAVE_NAME_PARAM = ParamSpec(
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

    収集の前と session 選択の前に呼ぶ。1 時間の収集や session の選択を終えてから
    「openカーネルが偶数です」と言われても遅い。
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
    always_save: bool = False,
) -> tuple[str, tuple[Artifact, ...]]:
    """診断をログへ出し、校正を保存し、図を描く.

    Args:
        ctx: 実行中のジョブ
        session: 材料にした session
        fit: フィット結果
        always_save: ``save_name`` が空でも自動命名して保存するか

    Returns:
        ``(まとめの断片, artifact)``
    """
    for line in _diagnostic_lines(fit):
        ctx.log(line)

    # 校正ファイルを図より先に書く。図は診断の補助で、落ちても校正は成果として
    # 残したい（順序が逆だと matplotlib の失敗で校正が 1 件も残らない）
    saved = _save(ctx, fit, always=always_save)
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


def calibration_result(
    ctx: JobContext, session: DatasetSession, fit: CalibrationFit
) -> JobResult:
    """校正だけを成果とするジョブ（再フィット）の JobResult を組む."""
    summary, artifacts = report_calibration(ctx, session, fit)
    return JobResult(
        summary=(
            f"校正生成完了: {fit.calibration.label} / "
            f"塗布 {fit.calibration.source.sample_count} 点 / {summary}"
        ),
        artifacts=artifacts,
    )


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

    図は診断の補助にすぎないので、描けなかったことで校正やジョブを落とさない。

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


def _save(ctx: JobContext, fit: CalibrationFit, *, always: bool) -> Path | None:
    """校正ファイルを保存する.

    Args:
        ctx: 実行中のジョブ
        fit: 保存する校正
        always: ``save_name`` が空でも自動命名して保存するか（収集ジョブは True）

    Returns:
        保存先（保存しなかったときは ``None``）
    """
    name = str(ctx.params.get("save_name", "")).strip() or (
        fit.calibration.label if always else ""
    )
    if not name:
        ctx.log("保存名が空なので校正ファイルは保存しません（診断のみ）")
        return None
    path = calibration_path(ctx.paste_volume_calibration_dir, name)
    if path.exists():
        ctx.log(f"同名の校正ファイルを上書きします: {path.name}")
    write_calibration(path, fit.calibration)
    ctx.log(f"校正ファイルを保存しました: {path}")
    return path
