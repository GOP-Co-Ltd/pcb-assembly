"""塗布実行のための HAL 配線をまとめたセッション.

「マシン設定読み込み → Board 計測 → probe / height_measurer の構築」をまとめ、 pad
ごとの座標変換（board → 補正 → toolhead → 高さ面）と applicator の構築点を 1
箇所に置く。コンテキストマネージャとして終了時のクリーンアップ（駐機）まで面倒を見る。

ユーザー対話・進捗・中断は持たない（web ジョブ側の責務）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Self

import attrs

from pcbasm.config import Machine, get_machine_config
from pcbasm.geometry import Compose, HeightPlane, Point2d, Transform
from pcbasm.hal import Camera, Klipper, XYZStage
from pcbasm.parking import park_or_present
from pcbasm.pasting.alignment import PasteCorrection
from pcbasm.pasting.applicator import PasteApplicator, build_applicator
from pcbasm.pasting.height import HeightPlaneMeasurer
from pcbasm.pasting.probe import ProbeExecutor
from pcbasm.pcb import Copper, Layer, Pad, PcbFile
from pcbasm.posctrl import (
    BoardCalibrationResult,
    RegionAlignmentSession,
    setup_board_calibration,
)
from pcbasm.vision import CalibrationResult, FrameSink


@attrs.frozen
class PasteSession:
    """マシン初期化〜Board 計測〜塗布用 HAL の配線をまとめた実行セッション.

    ``PasteSession.setup(...)`` または ``from_calibration(...)`` で構築し、
    コンテキストマネージャとして使う（終了時にノズルキャップへ駐機し、
    できなければ PRESENT / M84 に退避する）。
    """

    machine: Machine
    klipper: Klipper
    stage: XYZStage
    camera: Camera
    calibration: CalibrationResult
    calibration_result: BoardCalibrationResult
    board_transform: Transform
    offset_transform: Transform
    toolhead_offset: Transform
    pcb: PcbFile
    probe_executor: ProbeExecutor
    height_measurer: HeightPlaneMeasurer

    @classmethod
    def setup(
        cls,
        pcb_file_path: Path,
        tolerance: float = 0.1,
        *,
        camera: Camera | None = None,
        frame_sink: FrameSink | None = None,
    ) -> Self:
        """マシン設定読み込み〜Board 計測〜塗布用 HAL 構築をまとめて実行する."""
        machine = get_machine_config()
        result = setup_board_calibration(
            machine=machine,
            pcb_file_path=pcb_file_path,
            tolerance=tolerance,
            camera=camera,
            frame_sink=frame_sink,
        )
        return cls.from_calibration(result)

    @classmethod
    def from_calibration(cls, result: BoardCalibrationResult) -> Self:
        """既存の BoardCalibrationResult から塗布用 HAL を組み立てる."""
        machine = result.machine
        probe_config = machine.probe
        probe_executor = ProbeExecutor(
            klipper=result.klipper,
            stage=result.stage,
            lift_height=probe_config.lift_height,
        )
        height_measurer = HeightPlaneMeasurer(
            probe_executor=probe_executor,
            klipper=result.klipper,
            stage=result.stage,
            min_radius=probe_config.min_radius,
            board_edge_margin=probe_config.board_edge_margin,
            min_samples=probe_config.min_samples,
            max_samples=probe_config.max_samples,
        )
        return cls(
            machine=machine,
            klipper=result.klipper,
            stage=result.stage,
            camera=result.camera,
            calibration=result.calibration,
            calibration_result=result,
            board_transform=result.board_transform,
            offset_transform=result.offset_transform,
            toolhead_offset=machine.paste_dispenser.toolhead.to_transform(),
            pcb=result.pcb,
            probe_executor=probe_executor,
            height_measurer=height_measurer,
        )

    @property
    def board_to_machine(self) -> Transform:
        """Board 座標 → machine 座標の変換（board_transform + toolhead_offset）."""
        return Compose([self.board_transform, self.toolhead_offset])

    @property
    def top_coppers(self) -> tuple[Copper, ...]:
        return tuple(c for c in self.pcb.copper if c.layer == Layer.TOP)

    @property
    def component_positions(self) -> Mapping[str, Point2d]:
        """Designator → 部品位置（線塗布の走行方向の基準）."""
        return {c.designator: c.position for c in self.pcb.components}

    def measure_height_plane(
        self, coppers: Sequence[Copper] | None = None
    ) -> HeightPlane:
        """銅箔島上をプローブして高さ面を計測する（``None`` は TOP 層の全銅箔）."""
        return self.height_measurer.measure(
            coppers=self.top_coppers if coppers is None else coppers,
            board_to_machine=self.board_to_machine,
            outline=self.pcb.outline.polygon,
        )

    def alignment_session(
        self, *, frame_sink: FrameSink | None = None
    ) -> RegionAlignmentSession:
        """銅箔照合セッション（領域計画・照合・pad 精密照合）を作る."""
        return RegionAlignmentSession(self.calibration_result, frame_sink=frame_sink)

    def pad_transform(self, pad: Pad, correction: PasteCorrection) -> Transform:
        """1 pad 用の board → 補正 → toolhead → 高さ変換を返す."""
        return Compose(
            [
                self.board_transform,
                correction.alignment.correction_for(
                    pad.center, designator=pad.designator
                ),
                self.toolhead_offset,
                correction.height_plane,
            ]
        )

    def camera_target(
        self,
        pad: Pad,
        correction: PasteCorrection,
        *,
        offset: Point2d = Point2d(0.0, 0.0),
    ) -> Point2d:
        """補正済み pad 中心をカメラ中心へ置くステージ XY（+ 任意オフセット）."""
        shift = correction.alignment.correction_for(
            pad.center, designator=pad.designator
        )
        target = shift.apply(self.board_transform.apply(pad.center))
        return Point2d(target.x + offset.x, target.y + offset.y)

    def make_applicator(
        self,
        *,
        rotations_per_ul: float | None = None,
        lift_height: float | None = None,
    ) -> PasteApplicator:
        """Machine 設定のパラメータで :class:`PasteApplicator` を構築する."""
        return build_applicator(
            self.klipper,
            self.stage,
            self.machine.paste_dispenser,
            rotations_per_ul=rotations_per_ul,
            lift_height=lift_height,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        park_or_present(self.klipper, self.machine)
