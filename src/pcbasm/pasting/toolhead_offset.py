"""ツールヘッドオフセット計測（計測点配置・probe → deposit → measure 手順・結果集計）.

ループ・進捗・成果物保存はユーザー対話を持つ呼び出し側（web ジョブ）が担い、ここは 1 点分の機械手順と結果の算出・永続化だけを提供する。
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Self

import attrs
from cattrs.preconf.json import make_converter
from shapely import Point as ShapelyPoint, Polygon

from pcbasm.gcode import GCode
from pcbasm.geometry import Identity, Point2d
from pcbasm.pasting.applicator import PasteApplicator, build_applicator
from pcbasm.pasting.params import PasteParamsPatch
from pcbasm.pasting.probe import ProbeExecutor
from pcbasm.posctrl import (
    DETECTION_MAX_ATTEMPTS,
    DETECTION_RETRY_SEC,
    BoardCalibrationResult,
    CircleDetectionError,
    OffsetObserver,
    XYPositionAdjustor,
)
from pcbasm.vision import FrameSink, Image, PasteDotDetector

_converter = make_converter()
_converter.register_unstructure_hook(Point2d, lambda p: {"x": p.x, "y": p.y})
_converter.register_structure_hook(Point2d, lambda d, _: Point2d(x=d["x"], y=d["y"]))

MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT = 5


@attrs.frozen
class ToolheadOffsetSample:
    """1 計測点のツールヘッドオフセット.

    Attributes:
        board_position: 基板上の計測位置
        dispense_position: ペースト吐出時のステージ位置
        camera_position: カメラが検出した位置
        offset: ツールヘッド XY オフセット (mm)
    """

    board_position: Point2d
    dispense_position: Point2d
    camera_position: Point2d
    offset: Point2d

    @classmethod
    def from_positions(
        cls,
        *,
        board_position: Point2d,
        dispense_position: Point2d,
        camera_position: Point2d,
    ) -> Self:
        """吐出位置と検出位置から 1 点分のオフセットを算出する."""
        return cls(
            board_position=board_position,
            dispense_position=dispense_position,
            camera_position=camera_position,
            offset=dispense_position - camera_position,
        )


@attrs.frozen
class ToolheadOffsetResult:
    """複数点から計測したツールヘッドオフセット結果.

    Attributes:
        offset: 全計測点の平均ツールヘッド XY オフセット (mm)
        standard_deviation: 各軸の母標準偏差 (mm)
        samples: 各計測点の結果
        tolerance: 収束許容誤差 (mm)
        point_spacing: 計測点同士の最小間隔設定 (mm)
        edge_margin: ペースト外縁から基板外周までの margin (mm)
        calibrated_at: 計測日時
    """

    offset: Point2d
    standard_deviation: Point2d
    samples: tuple[ToolheadOffsetSample, ...]
    tolerance: float
    point_spacing: float
    edge_margin: float
    calibrated_at: datetime

    @classmethod
    def measure(
        cls,
        samples: Sequence[ToolheadOffsetSample],
        *,
        tolerance: float,
        point_spacing: float,
        edge_margin: float,
        calibrated_at: datetime,
    ) -> Self | None:
        """各計測点のオフセットから平均と母標準偏差を算出する.

        有効点が :data:`MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT` 未満なら ``None``。
        """
        measured_samples = tuple(samples)
        if len(measured_samples) < MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT:
            return None

        count = len(measured_samples)
        mean = Point2d(
            x=sum(sample.offset.x for sample in measured_samples) / count,
            y=sum(sample.offset.y for sample in measured_samples) / count,
        )
        standard_deviation = Point2d(
            x=math.sqrt(
                sum((sample.offset.x - mean.x) ** 2 for sample in measured_samples)
                / count
            ),
            y=math.sqrt(
                sum((sample.offset.y - mean.y) ** 2 for sample in measured_samples)
                / count
            ),
        )
        return cls(
            offset=mean,
            standard_deviation=standard_deviation,
            samples=measured_samples,
            tolerance=tolerance,
            point_spacing=point_spacing,
            edge_margin=edge_margin,
            calibrated_at=calibrated_at,
        )

    @property
    def is_within_tolerance(self) -> bool:
        """各軸の標準偏差が位置合わせ許容誤差以内か返す."""
        return (
            self.standard_deviation.x <= self.tolerance
            and self.standard_deviation.y <= self.tolerance
        )

    def to_dict(self) -> dict[str, Any]:
        """辞書に変換."""
        return _converter.unstructure(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """辞書から生成."""
        return _converter.structure(data, cls)

    def save(self, path: Path) -> None:
        """JSON ファイルに保存."""
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        """JSON ファイルから読み込み."""
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def plan_toolhead_offset_points(
    outline: Polygon,
    *,
    point_count: int,
    point_spacing: float,
    edge_margin: float,
    paste_diameter_max: float,
) -> tuple[tuple[Point2d, ...] | None, str | None]:
    """基板の安全領域内を左上から走査してオフセット計測点を配置する.

    安全領域の bbox 左上から細かく走査し、外形や穴によって領域外になる候補を
    飛ばしながら、採用済み点との距離が ``point_spacing`` 以上の点を必要数採る。
    そのため格子配置を優先しつつ、外形に合わせて半間隔ずれた点も利用できる。
    配置できないときは ``(None, 理由)`` を返す。
    """
    if (
        isinstance(point_count, bool)
        or not isinstance(point_count, int)
        or point_count < 1
    ):
        return None, "point_countは1以上の整数である必要があります"
    if not math.isfinite(point_spacing) or point_spacing <= 0:
        return None, "point_spacingは正の有限値である必要があります"
    if not math.isfinite(edge_margin) or edge_margin < 0:
        return None, "edge_marginは0以上の有限値である必要があります"
    if not math.isfinite(paste_diameter_max) or paste_diameter_max <= 0:
        return None, "paste_diameter_maxは正の有限値である必要があります"
    if outline.is_empty:
        return None, "基板外形が空のため計測点を配置できません"
    if not outline.is_valid:
        return None, "基板外形が不正なため計測点を配置できません"

    clearance = edge_margin + paste_diameter_max / 2
    safe_area = outline.buffer(-clearance)
    if safe_area.is_empty:
        return None, (
            "基板外形に安全領域を確保できません"
            f"（margin={edge_margin:g} mm, 最大直径={paste_diameter_max:g} mm）"
        )

    min_x, min_y, max_x, max_y = safe_area.bounds
    scan_step = point_spacing / 2
    column_count = math.floor((max_x - min_x) / scan_step + 1e-9) + 1
    row_count = math.floor((max_y - min_y) / scan_step + 1e-9) + 1
    minimum_distance_squared = point_spacing**2 * (1.0 - 1e-9)

    selected: list[Point2d] = []
    for row in range(row_count):
        y = min_y + row * scan_step
        for column in range(column_count):
            candidate = Point2d(x=min_x + column * scan_step, y=y)
            if not safe_area.covers(ShapelyPoint(candidate.x, candidate.y)):
                continue
            if any(
                (candidate.x - point.x) ** 2 + (candidate.y - point.y) ** 2
                < minimum_distance_squared
                for point in selected
            ):
                continue
            selected.append(candidate)
            if len(selected) == point_count:
                return tuple(selected), None

    return None, (
        "基板の安全領域に指定数の計測点を配置できません"
        f"（必要 {point_count} 点 / 配置可能 {len(selected)} 点、"
        f" 最小間隔={point_spacing:g} mm）"
    )


@attrs.frozen
class ToolheadOffsetPoint:
    """1 計測点の座標組.

    Attributes:
        board: 基板座標の計測点
        camera: カメラ中心をその点へ置くステージ位置（board 変換後）
        dispense: ノズルをその点へ置くステージ位置（camera + toolhead offset 設定値）
    """

    board: Point2d
    camera: Point2d
    dispense: Point2d


@attrs.frozen
class ProbedPoint:
    """高さ計測済みの計測点.

    Attributes:
        point: 計測点の座標組
        surface_z: プローブで得た基板表面の絶対 Z (mm)
    """

    point: ToolheadOffsetPoint
    surface_z: float


@attrs.frozen
class ToolheadOffsetFailure:
    """1 計測点の円検出失敗.

    Attributes:
        index: 1 始まりの計測点番号
        board_position: 基板座標の計測点
        reason: 検出失敗の理由
        image: 失敗時に撮影した ROI 画像（撮れなければ ``None``）
    """

    index: int
    board_position: Point2d
    reason: str
    image: Image | None = attrs.field(eq=False)

    @property
    def image_filename(self) -> str:
        """診断 JSON と成果物 PNG が共有する失敗画像のファイル名."""
        return f"toolhead_offset_failure_{self.index:02d}.png"


@attrs.frozen
class ToolheadOffsetDiagnostics:
    """円検出の診断（要求点数・最低有効点数・失敗一覧・成功点数）."""

    requested_point_count: int
    minimum_valid_point_count: int
    failures: tuple[ToolheadOffsetFailure, ...]
    successful_point_count: int

    @classmethod
    def from_outcomes(
        cls,
        requested_point_count: int,
        failures: Sequence[ToolheadOffsetFailure],
        samples: Sequence[ToolheadOffsetSample],
    ) -> Self:
        """計測ループの失敗一覧と成功 sample から診断を組む."""
        return cls(
            requested_point_count=requested_point_count,
            minimum_valid_point_count=MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT,
            failures=tuple(failures),
            successful_point_count=len(samples),
        )

    def to_dict(self) -> dict[str, Any]:
        """診断 JSON の dict（失敗画像はファイル名で参照する）."""
        return {
            "requested_point_count": self.requested_point_count,
            "minimum_valid_point_count": self.minimum_valid_point_count,
            "successful_point_count": self.successful_point_count,
            "failures": [
                {
                    "index": failure.index,
                    "board_position": {
                        "x": failure.board_position.x,
                        "y": failure.board_position.y,
                    },
                    "reason": failure.reason,
                    "image": (
                        failure.image_filename if failure.image is not None else None
                    ),
                }
                for failure in self.failures
            ],
        }

    def save(self, path: Path) -> Path:
        """診断 JSON を書き、書いた path を返す."""
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path


class ToolheadOffsetProcedure:
    """ツールヘッドオフセット計測の 1 点分の機械手順（probe → deposit → measure）.

    全点の高さ計測 → 全点の塗布 → 全点の円検出、というフェーズ順のループと進捗・ログ・成果物保存は呼び出し側が持つ。
    """

    def __init__(
        self,
        result: BoardCalibrationResult,
        *,
        tolerance: float,
        lift_height: float,
        diameter_min: float,
        diameter_max: float,
        point_spacing: float,
        frame_sink: FrameSink | None = None,
    ) -> None:
        """Board 計測結果と計測パラメータから HAL と検出器を配線する.

        Args:
            result: ボードキャリブレーション結果
            tolerance: 位置合わせ許容誤差 [mm]（円検出の標準偏差上限にも使う）
            lift_height: 吐出後の上昇高さ [mm]
            diameter_min: 検出円の最小直径 [mm]
            diameter_max: 検出円の最大直径 [mm]
            point_spacing: 計測点の最小間隔 [mm]（円検出 ROI の一辺に使う）
            frame_sink: 検出注釈画像を送る sink
        """
        machine = result.machine
        self._klipper = result.klipper
        self._stage = result.stage
        self._camera = result.camera
        self._calibration = result.calibration
        self._board_transform = result.board_transform
        self._dispenser_config = machine.paste_dispenser
        self._toolhead_transform = machine.paste_dispenser.toolhead.to_transform()
        self._lift_height = lift_height
        # 静定・サンプリングは機体設定の値を使う（他の計測と同じ現象・同じ検出器）
        self._settle_sec = machine.settle.move_sec
        self._probe_executor = ProbeExecutor(
            klipper=result.klipper,
            stage=result.stage,
            lift_height=machine.probe.lift_height,
            settle_sec=machine.settle.probe_sec,
        )
        roi_side = max(1, round(point_spacing * result.calibration.pixel_per_mm))
        self._roi_size = (roi_side, roi_side)
        detector = PasteDotDetector(
            pixel_per_mm=result.calibration.pixel_per_mm,
            diameter_min_mm=diameter_min,
            diameter_max_mm=diameter_max,
            crop_size=self._roi_size,
        )
        observer = OffsetObserver(
            detector=detector,
            camera=result.camera,
            crop_size=self._roi_size,
            frame_sink=frame_sink,
            sample_count=machine.detection.sample_count,
            minimum_sample_count=machine.detection.minimum_sample_count,
            max_attempts=DETECTION_MAX_ATTEMPTS,
            retry_sec=DETECTION_RETRY_SEC,
            max_standard_deviation_mm=tolerance,
        )
        self._adjustor = XYPositionAdjustor(
            settle_sec=machine.settle.move_sec,
            observe=observer.observe,
            klipper=result.klipper,
            stage=result.stage,
            offset_transform=result.offset_transform,
            tolerance=tolerance,
        )

    @property
    def roi_size(self) -> tuple[int, int]:
        """円検出 ROI のサイズ (width, height) [px]."""
        return self._roi_size

    def probe(self, point: Point2d) -> ProbedPoint:
        """吐出位置へ移動してプローブし、基板表面の絶対 Z を得る."""
        camera_position = self._board_transform.apply(point)
        dispense_position = self._toolhead_transform.apply(camera_position)
        self._klipper.send_gcode(
            self._stage.move(x=dispense_position.x, y=dispense_position.y)
            + GCode.wait_for_done()
        )
        surface_z = self._probe_executor.probe()
        return ProbedPoint(
            point=ToolheadOffsetPoint(
                board=point, camera=camera_position, dispense=dispense_position
            ),
            surface_z=surface_z,
        )

    def applicator(self) -> PasteApplicator:
        """塗布フェーズ用の :class:`PasteApplicator`（``lift_height`` 上書き）を作る."""
        return build_applicator(
            self._klipper,
            self._stage,
            self._dispenser_config,
            lift_height=self._lift_height,
        )

    def deposit(
        self, applicator: PasteApplicator, probed: ProbedPoint, *, amount_ul: float
    ) -> None:
        """プローブ済み点の吐出位置へ、表面 Z + 塗布高さで点塗布する."""
        default_params = applicator.default_params
        # transform は Identity なので machine XY と絶対 Z（表面 + 塗布高さ）を渡す。
        applicator.deposit_at(
            probed.point.dispense,
            amount_ul=amount_ul,
            transform=Identity(),
            params=default_params.patched(
                PasteParamsPatch(
                    paste_height=probed.surface_z + default_params.paste_height_mm
                )
            ),
        )

    def measure(
        self, index: int, probed: ProbedPoint
    ) -> ToolheadOffsetSample | ToolheadOffsetFailure:
        """カメラ位置へ移動して塗布痕を円検出し、1 点分のオフセットを求める.

        規定回数の再取得でも検出できなければ ROI 画像付きの失敗を返す。
        """
        point = probed.point
        self._klipper.send_gcode(
            self._stage.move(
                x=point.camera.x, y=point.camera.y, z=self._calibration.z_position
            )
            + GCode.wait(self._settle_sec)
            + GCode.wait_for_done()
        )
        try:
            camera_final_position = self._adjustor.adjust()
        except CircleDetectionError as exc:
            return ToolheadOffsetFailure(
                index=index,
                board_position=point.board,
                reason=str(exc),
                image=self._camera.capture().crop_center(self._roi_size),
            )
        return ToolheadOffsetSample.from_positions(
            board_position=point.board,
            dispense_position=point.dispense,
            camera_position=camera_final_position,
        )
