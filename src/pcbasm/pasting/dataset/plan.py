"""銅板上のセル格子・吐出量スイープ・撮影 view の計画（純ロジック）.

装置・カメラ・PCB を知らない純関数層。座標は銅板左上原点の board 座標 [mm]。
撮影が camera frame に、撮影・塗布が stage 可動域に収まるかの事前検証もここに置く（数値と
:class:`~pcbasm.geometry.Transform` だけに依存し、HAL には触れない）。
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

import attrs

from pcbasm.geometry import Point2d, Transform
from pcbasm.geometry.packing import Rect
from pcbasm.pasting.dataset.metadata import DatasetView
from pcbasm.pasting.sweep import sweep_schedule
from pcbasm.utils import is_finite_number

# 「ちょうど収まる」寸法が浮動小数点誤差で 1 行/1 列失われないための微小許容
_GRID_EPSILON = 1e-9

# セル格子に属さない収集設定の既定値（:class:`DotGridSpec` の既定と併せて、
# web ジョブの ``ParamSpec`` 既定値の唯一の出典）
#
# 周辺 view の既定は 0（中心のみ）。直径ベースの校正では周辺 view に実測上の利点が
# ほぼ無い（831 サンプルで検出失敗 0 件、view 間ばらつきは体積換算 1.2% で、5 view
# から 1 view へ落としても総残差は 8.0% → 8.1% 程度）のに対し、撮影枚数は view 数に
# 比例して収集時間を支配する。複数視点を前提にする CNN 方式の学習材料を採るときだけ
# 4 などへ上げる（docs/paste-volume-diameter-calibration.md の「周辺 view の既定」節）。
DEFAULT_VIEW_COUNT = 0
DEFAULT_VIEW_OFFSET_MM = 1.0
DEFAULT_PASTE_HEIGHT_MM = 0.2

# 指令として意味を持つ最小の吐出回転数 [rev]。ペーストスクリューのステッパーは
# 200 step/rev を 64 分割で駆動するので、1 マイクロステップぶんが指令の分解能。
# これを下回る指令は 1 マイクロステップも回らないため収集前に弾く。
MIN_COMMANDED_ROTATIONS = 1.0 / (200 * 64)


@attrs.frozen
class DotGridSpec:
    """銅板上のセル格子と吐出量スイープの設定.

    Attributes:
        plate_width_mm: 銅板の幅 [mm]（X 方向）
        plate_height_mm: 銅板の高さ [mm]（Y 方向）
        edge_margin_mm: 銅板端から有効領域までの余白 [mm]（四方）
        cell_size_mm: セル（塗布点の占有領域）の一辺 [mm]
        cell_gap_mm: 隣接セルの間隔 [mm]
        crop_size_mm: 撮影 crop の一辺 [mm]（``cell_size_mm + cell_gap_mm`` 以下）
        volume_min_ul: 吐出量スイープの下限 [μL]
        volume_max_ul: 吐出量スイープの上限 [μL]
        volume_divisions: 吐出量の分割数（1 以上）
        samples_per_volume: 1 量あたりのサンプル数（1 以上）
        blank_count: 塗布しない blank セル数（真値 0 のサンプル）
        shuffle_seed: 使用セルの選択と量・blank 割り当てのシード
    """

    plate_width_mm: float = 40.0
    plate_height_mm: float = 40.0
    edge_margin_mm: float = 2.0
    cell_size_mm: float = 2.0
    cell_gap_mm: float = 1.0
    crop_size_mm: float = 2.0
    volume_min_ul: float = 0.05
    volume_max_ul: float = 0.2
    volume_divisions: int = 5
    samples_per_volume: int = 3
    blank_count: int = 4
    shuffle_seed: int = 0

    def validate(self) -> str | None:
        """設定値の型・符号・大小関係を検証する（不正なら理由文）."""
        for name, value in (
            ("銅板の幅", self.plate_width_mm),
            ("銅板の高さ", self.plate_height_mm),
            ("セル寸法", self.cell_size_mm),
            ("撮影crop寸法", self.crop_size_mm),
            ("吐出量の下限", self.volume_min_ul),
        ):
            if not is_finite_number(value) or value <= 0:
                return f"{name}は正の有限値が必要です: {value!r}"
        for name, value in (
            ("外周余白", self.edge_margin_mm),
            ("セル間隔", self.cell_gap_mm),
        ):
            if not is_finite_number(value) or value < 0:
                return f"{name}は0以上の有限値が必要です: {value!r}"
        pitch = self.cell_size_mm + self.cell_gap_mm
        if self.crop_size_mm > pitch + _GRID_EPSILON:
            return (
                f"撮影crop寸法はセルピッチ（セル寸法 + セル間隔 = {pitch:g} mm）以下に"
                f"してください: {self.crop_size_mm!r}"
            )
        if not is_finite_number(self.volume_max_ul) or (
            self.volume_max_ul < self.volume_min_ul
        ):
            return (
                "吐出量の上限は下限以上の有限値が必要です: "
                f"{self.volume_max_ul!r} < {self.volume_min_ul!r}"
            )
        for name, count in (
            ("吐出量の分割数", self.volume_divisions),
            ("1量あたりのサンプル数", self.samples_per_volume),
        ):
            if type(count) is not int or count < 1:
                return f"{name}は1以上の整数が必要です: {count!r}"
        if type(self.blank_count) is not int or self.blank_count < 0:
            return f"blankセル数は0以上の整数が必要です: {self.blank_count!r}"
        if type(self.shuffle_seed) is not int:
            return f"配置シードは整数が必要です: {self.shuffle_seed!r}"
        return None

    @property
    def sample_count(self) -> int:
        """塗布するサンプル数（分割数 × 1 量あたりのサンプル数）."""
        return self.volume_divisions * self.samples_per_volume

    @property
    def target_count(self) -> int:
        """撮影対象セル数（塗布サンプル + blank）."""
        return self.sample_count + self.blank_count

    @property
    def volumes_ul(self) -> tuple[float, ...]:
        """``volume_min_ul``..``volume_max_ul`` を分割数で等分した昇順列 [μL]."""
        return sweep_schedule(
            self.volume_min_ul, self.volume_max_ul, self.volume_divisions
        )


@attrs.frozen
class DotCell:
    """塗布する 1 サンプルのセル矩形・中心・指令吐出量.

    Attributes:
        index: サンプル番号（1 起点。画像ファイル名の番号と一致する）
        rect: セル矩形（銅板左上原点 [mm]）
        center: セル中心（塗布点・撮影中心）
        commanded_volume_ul: 指令吐出量 [μL]
        volume_index: 吐出量列の index（0..``volume_divisions`` - 1）
        order: 塗布実行順（1 起点。セッション中の流量ドリフト検出に使う）
    """

    index: int
    rect: Rect
    center: Point2d
    commanded_volume_ul: float
    volume_index: int
    order: int


@attrs.frozen
class DotBlank:
    """塗布しない blank セル（真値 0 のサンプル）.

    Attributes:
        index: サンプル番号（``DotCell`` と同じ採番列を共有する）
        rect: セル矩形（銅板左上原点 [mm]）
        center: セル中心（撮影中心）
    """

    index: int
    rect: Rect
    center: Point2d


type DotTarget = DotCell | DotBlank


@attrs.frozen
class DotGridPlan:
    """点塗布データセット 1 回分のセル配置と量割り当て.

    Attributes:
        spec: 元の設定
        usable_area: 銅板から余白を除いた有効領域
        cells: 塗布するサンプルセル（index 昇順）
        blanks: 塗布しない blank セル（index 昇順）
        capacity: 格子へ入るセル総数（すべて計測可能点）
    """

    spec: DotGridSpec
    usable_area: Rect
    cells: tuple[DotCell, ...]
    blanks: tuple[DotBlank, ...]
    capacity: int

    @property
    def targets(self) -> tuple[DotTarget, ...]:
        """撮影対象セル（塗布サンプルと blank を index 昇順に並べたもの）."""
        merged: list[DotTarget] = [*self.cells, *self.blanks]
        return tuple(sorted(merged, key=lambda target: target.index))


@attrs.frozen
class _GridGeometry:
    """セル格子の幾何（量割り当ての前段）."""

    usable: Rect
    grid: tuple[Rect, ...]


def _grid_geometry(spec: DotGridSpec) -> tuple[_GridGeometry | None, str | None]:
    """有効領域と格子セルを求める（配置可能性は判定しない）."""
    error = spec.validate()
    if error is not None:
        return None, error

    usable = Rect(
        x=spec.edge_margin_mm,
        y=spec.edge_margin_mm,
        width=spec.plate_width_mm - 2.0 * spec.edge_margin_mm,
        height=spec.plate_height_mm - 2.0 * spec.edge_margin_mm,
    )
    if usable.width <= 0.0 or usable.height <= 0.0:
        return None, (
            f"外周余白 {spec.edge_margin_mm:g} mm では銅板 "
            f"{spec.plate_width_mm:g}x{spec.plate_height_mm:g} mm に有効領域が"
            "残りません"
        )
    return _GridGeometry(usable=usable, grid=_grid_rects(spec, usable)), None


def plan_dot_grid(spec: DotGridSpec) -> tuple[DotGridPlan | None, str | None]:
    """セル格子を敷き、シード付きシャッフルで吐出量と blank を割り当てる.

    除外領域は無く、格子セルはすべて計測可能点になる。

    使用セルは格子から ``shuffle_seed`` で無作為抽出して板全体へ散らし、行優先の
    昇順へ並べ直してから量と blank を割り当てる（先頭から詰めるとサンプルが板の上端
    数行に固まり、照明ムラや板の反りが帯単位で乗る）。同じ seed なら同じ配置になる。
    """
    geometry, error = _grid_geometry(spec)
    if geometry is None:
        return None, error

    usable = geometry.usable
    available = geometry.grid
    capacity = len(available)
    if capacity < spec.target_count:
        return None, (
            f"セル {capacity} 個に対しサンプル {spec.target_count} 個"
            f"（塗布 {spec.sample_count} + blank {spec.blank_count}）が必要です"
            "（銅板寸法・セル寸法・間隔・分割数・1 量あたりのサンプル数を"
            "調整してください）"
        )

    rng = random.Random(spec.shuffle_seed)
    used = [
        available[i] for i in sorted(rng.sample(range(capacity), spec.target_count))
    ]
    assignments: list[tuple[float, int] | None] = [
        (volume, volume_index)
        for _ in range(spec.samples_per_volume)
        for volume_index, volume in enumerate(spec.volumes_ul)
    ]
    assignments.extend([None] * spec.blank_count)
    rng.shuffle(assignments)

    cells: list[DotCell] = []
    blanks: list[DotBlank] = []
    for index, (rect, assignment) in enumerate(
        zip(used, assignments, strict=True), start=1
    ):
        center = Point2d(rect.x + rect.width / 2.0, rect.y + rect.height / 2.0)
        if assignment is None:
            blanks.append(DotBlank(index=index, rect=rect, center=center))
            continue
        volume, volume_index = assignment
        cells.append(
            DotCell(
                index=index,
                rect=rect,
                center=center,
                commanded_volume_ul=volume,
                volume_index=volume_index,
                order=len(cells) + 1,
            )
        )
    return (
        DotGridPlan(
            spec=spec,
            usable_area=usable,
            cells=tuple(cells),
            blanks=tuple(blanks),
            capacity=capacity,
        ),
        None,
    )


@attrs.frozen
class DotGridPreview:
    """WebUI へ返す診断用レイアウトと派生カウント.

    配置不能な設定でも、判明している範囲の幾何と理由を返して図を消さない。

    Attributes:
        spec: 元の設定
        plate: 銅板外形（左上原点）
        usable_area: 外周余白を除いた有効領域（設定が不正なら ``None``）
        grid: 格子セル全部（未使用セルを含む）
        cells: 塗布するサンプルセル（配置できなければ空）
        blanks: blank セル（配置できなければ空）
        capacity: 格子へ入るセル総数（すべて計測可能点）
        sample_count: 塗布するサンプル数
        target_count: 撮影対象セル数（塗布 + blank）
        volumes_ul: 吐出量の昇順列 [μL]
        views_per_cell: 中心を含む 1 セルあたりの view 数
        image_count: 保存される画像枚数（対象 × view × 塗布前後）
        error: 配置不能・設定不正の理由（無ければ ``None``）
    """

    spec: DotGridSpec
    plate: Rect
    usable_area: Rect | None
    grid: tuple[Rect, ...]
    cells: tuple[DotCell, ...]
    blanks: tuple[DotBlank, ...]
    capacity: int
    sample_count: int
    target_count: int
    volumes_ul: tuple[float, ...]
    views_per_cell: int
    image_count: int
    error: str | None


def preview_dot_grid(
    spec: DotGridSpec, *, view_count: int, view_offset_mm: float
) -> DotGridPreview:
    """WebUI 表示用に、配置結果と派生カウントをまとめて返す.

    撮影枚数や総点数を WebUI 側で再導出させないため、派生値はここで確定させる。
    設定が不正・配置不能でも例外を投げず、判明した幾何と理由を返す。
    """
    valid_spec = spec.validate() is None
    geometry, geometry_error = _grid_geometry(spec)
    plan, plan_error = plan_dot_grid(spec)
    views, view_error = plan_views(view_count, view_offset_mm)
    resolved_views = 0 if views is None else len(views)
    target_count = spec.target_count if valid_spec else 0
    return DotGridPreview(
        spec=spec,
        plate=_plate_rect(spec),
        usable_area=None if geometry is None else geometry.usable,
        grid=() if geometry is None else geometry.grid,
        cells=() if plan is None else plan.cells,
        blanks=() if plan is None else plan.blanks,
        capacity=0 if geometry is None else len(geometry.grid),
        sample_count=spec.sample_count if valid_spec else 0,
        target_count=target_count,
        volumes_ul=spec.volumes_ul if valid_spec else (),
        views_per_cell=resolved_views,
        image_count=target_count * resolved_views * 2,
        error=geometry_error or plan_error or view_error,
    )


def _plate_rect(spec: DotGridSpec) -> Rect:
    """銅板外形（寸法が有限な正値でなければ原点の点）."""
    if not is_finite_number(spec.plate_width_mm) or spec.plate_width_mm <= 0:
        return Rect(0.0, 0.0, 0.0, 0.0)
    if not is_finite_number(spec.plate_height_mm) or spec.plate_height_mm <= 0:
        return Rect(0.0, 0.0, 0.0, 0.0)
    return Rect(0.0, 0.0, float(spec.plate_width_mm), float(spec.plate_height_mm))


def plan_views(
    count: int, radius_mm: float
) -> tuple[tuple[DatasetView, ...] | None, str | None]:
    """中心 view 0 と、360/count 度ずつ回した count 個の周辺 view を返す.

    角度の起点は +X（0 度）で反時計回り。``count = 0`` は中心 view のみ。
    """
    if type(count) is not int or count < 0:
        return None, f"周辺view数は0以上の整数が必要です: {count!r}"
    if not is_finite_number(radius_mm):
        return None, f"viewの移動距離は有限値が必要です: {radius_mm!r}"
    if count > 0 and radius_mm <= 0:
        return None, f"周辺viewには正の移動距離が必要です: {radius_mm!r}"
    radius = float(radius_mm)
    views = [DatasetView(number=0, offset_x_mm=0.0, offset_y_mm=0.0)]
    for index in range(1, count + 1):
        angle = 2.0 * math.pi * (index - 1) / count
        views.append(
            DatasetView(
                number=index,
                offset_x_mm=radius * math.cos(angle),
                offset_y_mm=radius * math.sin(angle),
            )
        )
    return tuple(views), None


def validate_min_rotations(
    spec: DotGridSpec,
    *,
    rotations_per_ul: float,
    min_rotations: float = MIN_COMMANDED_ROTATIONS,
) -> str | None:
    """最小の指令吐出量が意味のある回転数になるかを検証する."""
    if not is_finite_number(rotations_per_ul) or rotations_per_ul <= 0:
        return f"rotations_per_ulは正の有限値が必要です: {rotations_per_ul!r}"
    rotations = spec.volume_min_ul * rotations_per_ul
    if rotations < min_rotations:
        return (
            f"吐出量の下限 {spec.volume_min_ul:g} uL は指令回転数 "
            f"{rotations:.4f} rev で、最小 {min_rotations:g} rev を下回ります"
            "（下限を上げるか吐出量キャリブレーションを見直してください）"
        )
    return None


def validate_crop_in_frame(
    views: Sequence[DatasetView],
    *,
    crop_size_px: int,
    pixel_per_mm: float,
    resolution: tuple[int, int],
) -> str | None:
    """View offset を足しても crop が camera frame に収まるかを検証する.

    撮影はセル中心をカメラ中心へ置いたうえで view offset ぶんステージをずらすので、
    crop 中心は画像中心から ``offset * pixel_per_mm`` だけ離れる。
    """
    if not is_finite_number(pixel_per_mm) or pixel_per_mm <= 0:
        return f"pixel_per_mmは正の有限値が必要です: {pixel_per_mm!r}"
    reach_mm = max(
        (max(abs(view.offset_x_mm), abs(view.offset_y_mm)) for view in views),
        default=0.0,
    )
    required = reach_mm * pixel_per_mm + crop_size_px / 2.0 + 1.0
    available = min(resolution) / 2.0
    if required > available:
        return (
            f"view offset {reach_mm:g} mm と crop {crop_size_px} px では撮影窓が "
            f"camera frame {resolution} に収まりません"
            f"（必要 {required:.1f} px > 使用可 {available:.1f} px）"
        )
    return None


def validate_capture_reach(
    plan: DotGridPlan,
    views: Sequence[DatasetView],
    *,
    board_to_stage: Transform,
    x_limits: tuple[float, float],
    y_limits: tuple[float, float],
) -> str | None:
    """全（セル × view）の撮影目標がステージ可動域に入るかを検証する."""
    for target in plan.targets:
        machine = board_to_stage.apply(target.center)
        for view in views:
            error = _reach_error(
                machine.x + view.offset_x_mm,
                machine.y + view.offset_y_mm,
                x_limits,
                y_limits,
                label=f"sample {target.index} view {view.number} の撮影位置",
            )
            if error is not None:
                return error
    return None


def validate_dispense_reach(
    plan: DotGridPlan,
    *,
    board_to_machine: Transform,
    x_limits: tuple[float, float],
    y_limits: tuple[float, float],
) -> str | None:
    """全塗布セルのノズル目標がステージ可動域に入るかを検証する.

    塗布目標は撮影目標から toolhead offset ぶんずれるので、撮影の可動域検証
    （:func:`validate_capture_reach`）とは別に見る必要がある。
    """
    for cell in plan.cells:
        label = f"sample {cell.index} の塗布位置"
        machine = board_to_machine.apply(cell.center)
        error = _reach_error(machine.x, machine.y, x_limits, y_limits, label=label)
        if error is not None:
            return error
    return None


def _reach_error(
    x: float,
    y: float,
    x_limits: tuple[float, float],
    y_limits: tuple[float, float],
    *,
    label: str,
) -> str | None:
    """可動域内なら ``None``、外れていれば理由文."""
    if x_limits[0] <= x <= x_limits[1] and y_limits[0] <= y <= y_limits[1]:
        return None
    return (
        f"{label} ({x:.3f}, {y:.3f}) がステージ可動域 "
        f"X{x_limits} / Y{y_limits} を外れます"
    )


def _grid_rects(spec: DotGridSpec, usable: Rect) -> tuple[Rect, ...]:
    """有効領域へ行優先（左上から）でセル矩形を敷く."""
    pitch = spec.cell_size_mm + spec.cell_gap_mm
    columns = _grid_count(usable.width, spec.cell_size_mm, pitch)
    rows = _grid_count(usable.height, spec.cell_size_mm, pitch)
    return tuple(
        Rect(
            x=usable.x + column * pitch,
            y=usable.y + row * pitch,
            width=spec.cell_size_mm,
            height=spec.cell_size_mm,
        )
        for row in range(rows)
        for column in range(columns)
    )


def _grid_count(available_mm: float, cell_size_mm: float, pitch_mm: float) -> int:
    """1 軸へ並ぶセル数（1 個も入らなければ 0）."""
    if available_mm + _GRID_EPSILON < cell_size_mm:
        return 0
    return int((available_mm - cell_size_mm) / pitch_mm + _GRID_EPSILON) + 1
