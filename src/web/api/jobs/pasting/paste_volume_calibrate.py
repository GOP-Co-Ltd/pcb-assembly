"""収集済み session から直径ベース塗布量校正を作るジョブ（装置不要）.

判定・フィット・診断は :mod:`pcbasm.pasting.paste_volume.fit`、図は
:mod:`pcbasm.visualization.paste_volume_render` にあり、ここは session の選択 UI と
成果物への変換、保存の可否だけを担う。

装置を使わず数秒で終わるので、実行そのものが検出ハイパラのプレビューになる。
``save_name`` を空にすると診断だけを出して Git 管理の保存先を汚さない。
"""

from __future__ import annotations

from pathlib import Path

import cv2

from pcbasm.pasting.dataset.reader import DatasetSession, completed_sessions
from pcbasm.pasting.paste_volume.calibration import (
    CALIBRATION_SUFFIX,
    calibration_filename,
    write_calibration,
)
from pcbasm.pasting.paste_volume.detect import DotDetectionSpec, detection_mask
from pcbasm.pasting.paste_volume.fit import CalibrationFit, CellMeasurement, fit_session
from pcbasm.vision.image import ImageArray
from pcbasm.visualization.paste_volume_render import (
    render_calibration_scatter,
    render_detection_montage,
)
from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.context import Artifact, JobContext, JobResult, PromptSpec

_DEFAULTS = DotDetectionSpec()

# モンタージュに並べる代表セル（小さい順に 3 つと blank）
_MONTAGE_DISPENSED = 3


def register(catalog: JobCatalog) -> None:
    catalog.register(
        JobDefinition(
            name="paste_volume_calibrate",
            label="塗布量校正の生成（直径ベース）",
            tab="pasting",
            run=_run_paste_volume_calibrate,
            params=(
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
                    help=(
                        "2値化の閾値の下限を「最小コントラスト × この比」に置きます。"
                    ),
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
                ParamSpec(
                    "require_blank_zero",
                    "blankの直径0を必須にする",
                    "bool",
                    default=True,
                    help=(
                        "blank セルで直径が 0 にならなければ失敗させます。"
                        "外すとハイパラ探索の途中経過も見られます。"
                    ),
                ),
                ParamSpec(
                    "save_name",
                    "保存名",
                    "str",
                    default="",
                    optional=True,
                    help=(
                        "空なら保存せず診断だけを出します。"
                        "指定するとその名前で校正ファイルを保存します。"
                    ),
                ),
            ),
            requires_pcb=False,
            uses_machine=False,
        )
    )


def _run_paste_volume_calibrate(ctx: JobContext) -> JobResult:
    """選んだ session を計測してフィットし、校正ファイルと診断図を出す."""
    spec = _detection_spec(ctx)
    error = spec.validate()
    if error is not None:
        raise ValueError(error)

    session = _choose_session(ctx)
    ctx.log(f"session {session.label} / {len(session.cells())} セルを計測します")
    ctx.progress("計測とフィット", None)

    fit, error = fit_session(
        session,
        spec=spec,
        require_blank_zero=bool(ctx.params["require_blank_zero"]),
    )
    if fit is None:
        raise ValueError(error)

    for line in _diagnostic_lines(fit):
        ctx.log(line)

    ctx.progress("成果物の生成", None)
    artifacts = _render_artifacts(ctx, session, fit, spec)
    saved = _save(ctx, fit)
    if saved is not None:
        artifacts = (*artifacts, ctx.artifact("校正ファイル", saved.name, "file"))

    diagnostics = fit.calibration.diagnostics
    return JobResult(
        summary=(
            f"校正生成完了: {fit.calibration.label} / "
            f"塗布 {fit.calibration.source.sample_count} 点 / "
            f"総体積誤差 {diagnostics.total_relative_error * 100:+.2f}% / "
            f"点ごと残差std {diagnostics.residual_relative_std * 100:.1f}% / "
            + ("保存なし（診断のみ）" if saved is None else f"保存先 {saved.name}")
        ),
        artifacts=artifacts,
    )


def _detection_spec(ctx: JobContext) -> DotDetectionSpec:
    """ジョブパラメータを検出ハイパラへ写す."""
    return DotDetectionSpec(
        min_contrast=float(ctx.params["min_contrast"]),
        contrast_percentile=float(ctx.params["contrast_percentile"]),
        threshold_floor_ratio=float(ctx.params["threshold_floor_ratio"]),
        open_kernel_px=int(ctx.params["open_kernel_px"]),
        min_area_px=int(ctx.params["min_area_px"]),
    )


def _choose_session(ctx: JobContext) -> DatasetSession:
    """完成 session を運転者に選ばせて読み込む."""
    sessions = completed_sessions(ctx.paste_dataset_dir)
    if not sessions:
        raise ValueError(
            f"校正に使える完成datasetがありません: {ctx.paste_dataset_dir}"
        )
    answer = ctx.prompt(
        PromptSpec(
            kind="choice",
            message="校正の材料にするdatasetを選んでください。",
            default=sessions[-1].name,
            choices=tuple(session.name for session in sessions),
        )
    )
    session, error = DatasetSession.load(ctx.paste_dataset_dir / str(answer))
    if session is None:
        raise ValueError(error)
    return session


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
    ctx: JobContext,
    session: DatasetSession,
    fit: CalibrationFit,
    spec: DotDetectionSpec,
) -> tuple[Artifact, ...]:
    """散布図と検出モンタージュを artifacts_dir へ描く."""
    scatter = "paste_volume_calibration.png"
    render_calibration_scatter(
        fit.cells,
        fit.calibration.model,
        f"{session.label} / diameter to volume",
        ctx.artifacts_dir / scatter,
    )
    montage = "paste_volume_detection.png"
    render_detection_montage(
        _montage_panels(session, fit, spec),
        f"{session.label} / detection",
        ctx.artifacts_dir / montage,
    )
    return (
        ctx.artifact("直径と体積の散布図", scatter, "image"),
        ctx.artifact("検出モンタージュ", montage, "image"),
    )


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


def _save(ctx: JobContext, fit: CalibrationFit) -> Path | None:
    """``save_name`` があれば校正ファイルを保存する（空なら診断のみ）."""
    name = str(ctx.params.get("save_name", "")).strip()
    if not name:
        ctx.log("保存名が空なので校正ファイルは保存しません（診断のみ）")
        return None
    filename = name if name.endswith(CALIBRATION_SUFFIX) else calibration_filename(name)
    path = ctx.paste_volume_calibration_dir / filename
    write_calibration(path, fit.calibration)
    ctx.log(f"校正ファイルを保存しました: {path}")
    return path
