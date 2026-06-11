"""pad単位の銅箔照合による自動位置合わせ."""

import logging
import math
from collections.abc import Sequence

import attrs
import cv2
import numpy as np
import shapely

from pcbasm import gcode
from pcbasm.geometry import Point2d, Rotation, Transform
from pcbasm.hal import Camera, Klipper, Speed, XYZStage
from pcbasm.pcb import Pad
from pcbasm.posctrl.copper import (
    CopperEdgeMatcher,
    CopperProjection,
    CopperProjector,
    PixelRect,
    RigidEdgeMatch,
)
from pcbasm.posctrl.correction import to_machine_transform
from pcbasm.posctrl.position import XYPositionAdjustor
from pcbasm.utils import get_class_module_path
from pcbasm.vision import CopperEdgeDetector, Image, ImageArray

_EXPECTED_COLOR = (0, 0, 255)  # 想定エッジの表示色 (BGR: 赤)
_DETECTED_COLOR = (0, 255, 0)  # 検出エッジの表示色 (BGR: 緑)
_ROI_COLOR = (255, 255, 255)  # ROI枠の表示色 (BGR: 白)


@attrs.frozen
class PadGroup:
    """補正Transformを共有するpadのグループ.

    グループの代表padで計測した補正Transformを、グループ内の全padで
    共有するための単位。padsはグループ内padの重心に近い順に並び、
    先頭が代表。代表で照合に失敗した場合は後続のpadを順に試せる。

    Attributes:
        pads: グループ内のpad（重心に近い順）
    """

    pads: tuple[Pad, ...]

    @property
    def representative(self) -> Pad:
        """代表pad（グループ内padの重心に最も近いpad）."""
        return self.pads[0]


def _centroid(pads: Sequence[Pad]) -> Point2d:
    """pad中心の重心を返す."""
    return Point2d(
        x=sum(p.center.x for p in pads) / len(pads),
        y=sum(p.center.y for p in pads) / len(pads),
    )


def _ordered_group(pads: Sequence[Pad]) -> PadGroup:
    """padを重心に近い順に並べたPadGroupを作る."""
    centroid = _centroid(pads)
    return PadGroup(pads=tuple(sorted(pads, key=lambda p: (p.center - centroid).norm)))


def _split_cluster(members: Sequence[Pad], count: int) -> list[PadGroup]:
    """クラスタをcount個のグループへ空間的に分割する.

    pad中心を主成分軸（広がりが最大の方向）へ射影した順に並べ、 要素数が均等（差は高々1）な連続チャンクに切る。代表がクラスタの
    長手方向へ等間隔に分散する。
    """
    if count <= 1:
        return [_ordered_group(members)]

    centers = np.array([[p.center.x, p.center.y] for p in members])
    deviations = centers - centers.mean(axis=0)
    # 共分散の第1固有ベクトル = 広がりが最大の方向
    _, eigenvectors = np.linalg.eigh(deviations.T @ deviations)
    axis = eigenvectors[:, -1]
    order = np.argsort(deviations @ axis, kind="stable")
    return [
        _ordered_group([members[int(i)] for i in chunk])
        for chunk in np.array_split(order, count)
        if len(chunk) > 0
    ]


def cluster_pads(
    pads: Sequence[Pad],
    cluster_distance_mm: float,
    pads_per_representative: int,
) -> list[PadGroup]:
    """近傍padを単一連結のクラスタにまとめ、代表を持つグループへ分割する.

    padポリゴン同士の隙間距離がcluster_distance_mm以下なら同じ
    クラスタに連結する（推移的）。クラスタの要素数が
    pads_per_representativeを超える場合は ceil(要素数 / 同値) 個の
    グループへ空間分割し、それぞれが代表を持つ。

    Args:
        pads: 対象pad列
        cluster_distance_mm: 同一クラスタとみなすpadポリゴン間の距離（mm）
        pads_per_representative: 代表1つあたりのpad数

    Returns:
        グループのリスト。各グループ内のpadは重心に近い順

    Raises:
        ValueError: cluster_distance_mmが正でない、または
            pads_per_representativeが1未満の場合
    """
    if cluster_distance_mm <= 0:
        raise ValueError(
            f"cluster_distance_mmは正の値である必要があります: {cluster_distance_mm}"
        )
    if pads_per_representative < 1:
        raise ValueError(
            "pads_per_representativeは1以上である必要があります: "
            f"{pads_per_representative}"
        )
    if not pads:
        return []

    # ポリゴン間距離が閾値以下のペアを列挙し、union-findで連結成分を作る
    polygons = [p.polygon for p in pads]
    tree = shapely.STRtree(polygons)
    left, right = tree.query(
        polygons, predicate="dwithin", distance=cluster_distance_mm
    )

    parent = list(range(len(pads)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in zip(left, right):
        root_a, root_b = find(int(a)), find(int(b))
        if root_a != root_b:
            parent[root_b] = root_a

    clusters: dict[int, list[Pad]] = {}
    for i, pad in enumerate(pads):
        clusters.setdefault(find(i), []).append(pad)

    groups: list[PadGroup] = []
    for members in clusters.values():
        count = math.ceil(len(members) / pads_per_representative)
        groups.extend(_split_cluster(members, count))
    return groups


class CopperPadObserver:
    """固定アンカー投影に対するpad ROI照合のobserver.

    observe() -> Transform 契約（カメラmm空間、原点=画像中心、想定→観測）。
    """

    def __init__(
        self,
        camera: Camera,
        edge_detector: CopperEdgeDetector,
        matcher: CopperEdgeMatcher,
        projection: CopperProjection,
        roi: PixelRect,
        window_name: str | None = None,
        max_offset_mm: float | None = None,
    ) -> None:
        """CopperPadObserverを初期化する.

        Args:
            camera: カメラ
            edge_detector: 銅箔エッジ検出器
            matcher: エッジ照合器
            projection: アンカー位置で固定した想定銅箔の投影
            roi: 照合に使うROI矩形（全画面px）
            window_name: 観測ごとに照合状況を表示するウィンドウ名。
                Noneの場合は表示しない
            max_offset_mm: 照合ずれの許容上限（mm）。boardキャリブレーション
                済みでpadはほぼ合っている前提のもと、これを超えるずれは
                誤マッチとみなして照合失敗にする。Noneの場合は無制限
        """
        self._camera = camera
        self._edge_detector = edge_detector
        self._matcher = matcher
        self._projection = projection
        self._roi = roi
        self._window_name = window_name
        self._max_offset_mm = max_offset_mm
        self._last_match: RigidEdgeMatch | None = None

    def observe(self) -> Transform:
        """撮像→エッジ検出→剛体照合し、想定→観測のTransformを返す.

        Returns:
            想定→観測のTransform（カメラmm空間）

        Raises:
            RuntimeError: 照合に失敗した場合、または照合ずれが
                max_offset_mmを超えた場合
        """
        image = self._camera.capture()
        edges = self._edge_detector.detect_edges(image)
        if self._window_name is not None:
            self._show(self._window_name, image, edges)
        match = self._matcher.match_rigid(
            edges, self._projection.edge_mask, roi=self._roi
        )
        if match is None:
            raise RuntimeError("銅箔エッジの照合に失敗しました")
        offset_norm = match.offset.mm.norm
        if self._max_offset_mm is not None and offset_norm > self._max_offset_mm:
            raise RuntimeError(
                f"照合ずれ {offset_norm:.3f} mm が上限 "
                f"{self._max_offset_mm} mm を超過しました（誤マッチの疑い）"
            )
        self._last_match = match
        return match.camera_transform

    def _show(self, window_name: str, image: Image, edges: ImageArray) -> None:
        """ROI枠と想定（赤）・検出（緑）エッジを重ねて表示する."""
        x0, y0, x1, y1 = self._roi
        display = image.numpy().copy()
        roi_view = display[y0:y1, x0:x1]
        roi_view[self._projection.edge_mask[y0:y1, x0:x1] > 0] = _EXPECTED_COLOR
        roi_view[edges[y0:y1, x0:x1] > 0] = _DETECTED_COLOR
        cv2.rectangle(display, (x0, y0), (x1 - 1, y1 - 1), _ROI_COLOR, 1)
        cv2.imshow(window_name, display)
        cv2.waitKey(1)

    @property
    def last_match(self) -> RigidEdgeMatch | None:
        """直近の照合結果."""
        return self._last_match


@attrs.frozen
class PadAlignmentResult:
    """pad位置合わせの結果.

    Attributes:
        machine_transform: 設計machine点→観測machine点の変換（fill path合成用）
        match: 最終照合結果（表示用）
        anchor: 投影アンカー s0 = board_transform.apply(pad.center)（機械座標、mm）
        adjusted_position: 収束後のXY位置（機械座標、mm）
        roi: 照合に使ったROI矩形（全画面px）
    """

    machine_transform: Transform
    match: RigidEdgeMatch
    anchor: Point2d
    adjusted_position: Point2d
    roi: PixelRect

    @property
    def translation(self) -> Point2d:
        """アンカー点での並進補正量（machine mm）."""
        return self.machine_transform.apply(self.anchor) - self.anchor

    @property
    def rotation(self) -> Rotation:
        """machine空間での回転成分（鏡映も自動処理）."""
        ex = Point2d(1.0, 0.0)
        origin = self.machine_transform.apply(self.anchor)
        moved = self.machine_transform.apply(self.anchor + ex)
        return Rotation.from_points(ex, moved - origin)


class PadAligner:
    """pad単位で銅箔照合による自動位置合わせを行うクラス.

    pad中心へ移動し、指令位置に固定したアンカーで想定銅箔を投影、 pad
    ROI限定の剛体照合とXYPositionAdjustorで収束させ、 machine空間の補正Transformを構築する。
    """

    def __init__(
        self,
        *,
        camera: Camera,
        klipper: Klipper,
        stage: XYZStage,
        projector: CopperProjector,
        matcher: CopperEdgeMatcher,
        edge_detector: CopperEdgeDetector,
        board_transform: Transform,
        offset_transform: Transform,
        roi_margin_mm: float = 1.0,
        min_roi_mm: float = 3.0,
        tolerance: float = 0.05,
        max_correction_mm: float | None = 1.0,
        max_iterations: int = 10,
        settle_time: float = 0.5,
        window_name: str | None = None,
    ) -> None:
        """PadAlignerを初期化する.

        Args:
            camera: カメラ
            klipper: Klipperクライアント
            stage: XYZステージ
            projector: 設計銅箔の投影器
            matcher: エッジ照合器
            edge_detector: 銅箔エッジ検出器
            board_transform: board座標→機械座標の変換
            offset_transform: 観測オフセット系から機械座標系への変換
            roi_margin_mm: pad投影bboxへ加えるROIマージン（mm）
            min_roi_mm: ROIの最小辺長（mm）
            tolerance: 収束の許容誤差（mm）
            max_correction_mm: 1回の照合で許容する最大ずれ（mm）。
                超過は誤マッチとみなして照合失敗にする。Noneは無制限
            max_iterations: 収束ループの最大反復回数
            settle_time: 移動後の安定待機時間（秒）
            window_name: 観測ごとに照合状況を表示するウィンドウ名。
                Noneの場合は表示しない
        """
        self._camera = camera
        self._klipper = klipper
        self._stage = stage
        self._projector = projector
        self._matcher = matcher
        self._edge_detector = edge_detector
        self._board_transform = board_transform
        self._offset_transform = offset_transform
        self._roi_margin_mm = roi_margin_mm
        self._min_roi_mm = min_roi_mm
        self._tolerance = tolerance
        self._max_correction_mm = max_correction_mm
        self._max_iterations = max_iterations
        self._settle_time = settle_time
        self._window_name = window_name

        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def align(self, pad: Pad) -> PadAlignmentResult:
        """padの中心へ移動し、銅箔照合で収束するまで位置補正する.

        投影アンカーはpadの指令位置 s0 に固定し、収束ループ中は
        再投影しない（毎反復同位置で再投影すると補正が収束しない）。

        Args:
            pad: 対象pad

        Returns:
            位置合わせ結果

        Raises:
            RuntimeError: 照合に失敗、または収束しなかった場合
        """
        anchor = self._board_transform.apply(pad.center)
        self._logger.info(
            "pad %s.%s の位置合わせを開始: anchor (%.4f, %.4f) mm",
            pad.designator,
            pad.pad_number,
            anchor.x,
            anchor.y,
        )

        # pad中心へ移動
        self._klipper.send_gcode(
            self._stage.move(x=anchor.x, y=anchor.y, speed=Speed.rate(0.5))
            + gcode.wait(self._settle_time)
            + gcode.wait_for_done()
        )

        # 投影とROIをアンカー s0 で固定する（ループ中は再投影しない）
        projection = self._projector.project(anchor)
        roi = self._projector.roi_of(
            pad.polygon,
            anchor,
            margin_mm=self._roi_margin_mm,
            min_size_mm=self._min_roi_mm,
        )

        observer = CopperPadObserver(
            camera=self._camera,
            edge_detector=self._edge_detector,
            matcher=self._matcher,
            projection=projection,
            roi=roi,
            window_name=self._window_name,
            max_offset_mm=self._max_correction_mm,
        )
        adjustor = XYPositionAdjustor(
            observe=observer.observe,
            klipper=self._klipper,
            stage=self._stage,
            offset_transform=self._offset_transform,
            tolerance=self._tolerance,
            max_iterations=self._max_iterations,
            settle_time=self._settle_time,
        )
        adjusted_position = adjustor.adjust()

        # adjust() が成功した時点で observe() は少なくとも1回成功している
        match = observer.last_match
        assert match is not None

        machine_transform = to_machine_transform(
            match.camera_transform,
            self._offset_transform,
            projection_anchor=anchor,
            observed_at=self._stage.get_position().to2d(),
        )
        return PadAlignmentResult(
            machine_transform=machine_transform,
            match=match,
            anchor=anchor,
            adjusted_position=adjusted_position,
            roi=roi,
        )
