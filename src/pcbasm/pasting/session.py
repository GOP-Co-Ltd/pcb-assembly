"""塗布実行のための HAL 配線をまとめたセッション.

Board 計測の結果から probe / height_measurer を組み、 pad ごとの座標変換（board → 補正 →
toolhead → 高さ面）と applicator の構築点を 1 箇所に置く。

ユーザー対話・進捗・中断は持たない（web ジョブ側の責務）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Self

import attrs

from pcbasm.align import BoardFrame, RegionAlignmentSession
from pcbasm.board import Board, Copper, Layer, Pad
from pcbasm.config import MachineConfig
from pcbasm.config.dispenser import ToolheadConfig
from pcbasm.geometry import Compose, HeightPlane, Identity, Point2d, Shift, Transform
from pcbasm.hal import Camera, KlipperClient, XYZStage
from pcbasm.pasting.alignment import PasteCorrection
from pcbasm.pasting.applicator import PasteApplicator, build_applicator
from pcbasm.pasting.height import HeightPlaneMeasurer
from pcbasm.pasting.probe import ProbeExecutor
from pcbasm.vision import FrameSink


def toolhead_transform(toolhead: ToolheadConfig) -> Transform:
    """Toolhead の位置に xy 平行移動する Transform を返す."""
    return Shift(x=toolhead.x, y=toolhead.y)


@attrs.frozen
class PasteSession:
    """Board 計測の結果から塗布用 HAL を配線した実行セッション.

    ``from_board_frame(...)`` で構築する。
    終了時の駐機はジョブ管理（web）が行う。

    塗布に渡す board → machine 変換は、塗る対象で選ぶ。

    - pad: :meth:`pad_transform`（pad ごとに組む）
    - pad ではない board 上の点（パージ・測定点など）: :meth:`point_transform`（点ごとに組む）
    - 位置合わせしない銅板: :meth:`plate_transform`（1 回組めば板上の全点に使える）

    3 つとも最後に高さ面を通すので、塗布高さは高さ面からの相対になる。
    :attr:`board_to_machine` は補正も高さ面も含まないので、塗布には使わない。
    """

    machine: MachineConfig
    klipper: KlipperClient
    stage: XYZStage
    camera: Camera
    board_frame: BoardFrame
    toolhead_offset: Transform
    probe_executor: ProbeExecutor
    height_measurer: HeightPlaneMeasurer

    @classmethod
    def from_board_frame(
        cls,
        board_frame: BoardFrame,
        machine: MachineConfig,
        *,
        klipper: KlipperClient,
        stage: XYZStage,
        camera: Camera,
    ) -> Self:
        """計測済みの基板の位置関係と装置から塗布用 HAL を組み立てる."""
        probe_config = machine.probe
        settle = machine.settle
        probe_executor = ProbeExecutor(
            klipper=klipper,
            stage=stage,
            lift_height=probe_config.lift_height,
            settle_sec=settle.probe_sec,
        )
        height_measurer = HeightPlaneMeasurer(
            probe_executor=probe_executor,
            klipper=klipper,
            stage=stage,
            min_radius=probe_config.min_radius,
            board_edge_margin=probe_config.board_edge_margin,
            min_samples=probe_config.min_samples,
            max_samples=probe_config.max_samples,
            settle_sec=settle.move_sec,
        )
        return cls(
            machine=machine,
            klipper=klipper,
            stage=stage,
            camera=camera,
            board_frame=board_frame,
            toolhead_offset=toolhead_transform(machine.paste_dispenser.toolhead),
            probe_executor=probe_executor,
            height_measurer=height_measurer,
        )

    @property
    def board_to_machine(self) -> Transform:
        """Board 座標 → machine 座標の変換（board_transform + toolhead_offset）.

        位置合わせ補正と高さ面を含まないので、高さ面を測るプローブや可動域の検証など XY の行き先だけが要る用途に使う。
        """
        return Compose([self.board_frame.board_transform, self.toolhead_offset])

    @property
    def top_coppers(self) -> tuple[Copper, ...]:
        return tuple(c for c in self.board_frame.board.copper if c.layer == Layer.TOP)

    @property
    def component_positions(self) -> Mapping[str, Point2d]:
        """Designator → 部品位置（線塗布の走行方向の基準）."""
        return {c.designator: c.position for c in self.board_frame.board.components}

    def measure_height_plane(
        self, coppers: Sequence[Copper] | None = None
    ) -> HeightPlane:
        """銅箔島上をプローブして高さ面を計測する（``None`` は TOP 層の全銅箔）."""
        return self.height_measurer.measure(
            coppers=self.top_coppers if coppers is None else coppers,
            board_to_machine=self.board_to_machine,
            outline=self.board_frame.board.outline.polygon,
        )

    def alignment_session(
        self, *, frame_sink: FrameSink | None = None
    ) -> RegionAlignmentSession:
        """銅箔照合セッション（領域計画・照合・pad 精密照合）を作る."""
        return RegionAlignmentSession(
            self.board_frame,
            self.machine,
            klipper=self.klipper,
            stage=self.stage,
            camera=self.camera,
            frame_sink=frame_sink,
        )

    def plate_transform(self, *, height_plane: HeightPlane) -> Transform:
        """補正を挟まない board → toolhead → 高さ変換を返す.

        銅板の点塗布のように照合対象の銅箔パターンが無い場合の入口。pad ベースの
        :meth:`pad_transform` と違い領域照合の補正を挟まないので、対象点に依らず
        1 回組めば板上のどの点にも使える。
        """
        return self._point_chain(Identity(), height_plane)

    def pad_transform(self, pad: Pad, correction: PasteCorrection) -> Transform:
        """1 pad 用の board → 補正 → toolhead → 高さ変換を返す."""
        return self._point_chain(
            correction.alignment.correction_for(pad.center, designator=pad.designator),
            correction.height_plane,
        )

    def point_transform(self, point: Point2d, correction: PasteCorrection) -> Transform:
        """任意の board 点用の board → 補正 → toolhead → 高さ変換を返す.

        pad を持たない対象（任意位置のパージなど）の入口。補正はその点を覆う
        位置合わせ成功領域から内挿するので、点ごとに組み直す必要がある。
        """
        return self._point_chain(
            correction.alignment.correction_for(point), correction.height_plane
        )

    def camera_point_target(
        self,
        point: Point2d,
        *,
        offset: Point2d = Point2d(0.0, 0.0),
        correction: Transform | None = None,
    ) -> Point2d:
        """Board 座標の点をカメラ中心へ置くステージ XY（+ 任意オフセット）.

        ``correction`` にその点へ適用する機械座標の位置合わせ補正を渡すと、塗布と同じ
        位置を撮る。
        銅板のように位置合わせしない対象では ``None`` のままでよい。
        """
        machine_point = self.board_frame.board_transform.apply(point)
        if correction is not None:
            machine_point = correction.apply(machine_point)
        return Point2d(machine_point.x + offset.x, machine_point.y + offset.y)

    def _point_chain(self, correction: Transform, height_plane: Transform) -> Transform:
        """Board → 補正 → toolhead → 高さ の変換列を組む."""
        return Compose(
            [
                self.board_frame.board_transform,
                correction,
                self.toolhead_offset,
                height_plane,
            ]
        )

    def make_applicator(
        self,
        *,
        rotations_per_ul: float | None = None,
        lift_height: float | None = None,
    ) -> PasteApplicator:
        """Machine 設定のパラメータで :class:`PasteApplicator` を構築する.

        返す applicator はまだディスペンサーを有効化していない。
        ``with`` で有効化してから使う。

        Args:
            rotations_per_ul: μL → 回転数の係数 [rev/μL] の上書き（``None`` で machine 設定の値）
            lift_height: 塗布後の上昇高さ [mm] の上書き（``None`` で machine 設定の値）
        """
        return build_applicator(
            self.klipper,
            self.stage,
            self.machine.paste_dispenser,
            rotations_per_ul=rotations_per_ul,
            lift_height=lift_height,
        )
