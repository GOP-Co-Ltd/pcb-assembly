"""領域単位の銅箔照合による位置ずれの反復計測."""

import logging

import attrs

from pcbasm import gcode
from pcbasm.geometry import Point2d, Shift, Transform
from pcbasm.hal import Camera, Klipper, Speed, XYZStage
from pcbasm.posctrl.copper import CopperEdgeMatcher, CopperProjector, EdgeMatch
from pcbasm.posctrl.correction import to_machine_transform
from pcbasm.posctrl.region import AlignmentRegion
from pcbasm.posctrl.render import render_edge_match
from pcbasm.utils import get_class_module_path
from pcbasm.vision import CopperEdgeDetector, FrameSink


@attrs.frozen
class RegionAlignment:
    """1領域の照合結果（最大max_passes回の反復計測の累積）.

    Attributes:
        region: 対象領域
        match: 最終パスの照合結果
        displacement: 累積変位（観測 − 設計）[mm]、機械座標。
            各パスの補正は純並進なので単純和になる
        increment: 最終パスの増分 [mm]（収束の読み取り用）
        passes: 実施したパス数（1以上max_passes以下）
        converged: 最終パスの増分がconverge_tolerance以下だった
    """

    region: AlignmentRegion
    match: EdgeMatch
    displacement: Point2d
    increment: Point2d
    passes: int
    converged: bool


class RegionAligner:
    """領域単位で銅箔照合による位置ずれを反復計測するクラス.

    領域のアンカー（＋それまでの累積変位）へ移動し、その指令位置に固定した
    アンカーで想定銅箔を投影、領域ROI限定の並進照合を行う。2パス目以降は
    **累積変位をboard変換の後段へ挿した投影器**で投影する。ステージを動かすと
    想定投影と観測が画像内で同じだけ動くので、投影を補正せずに再計測すると 同じ変位を二重に足してしまう。

    増分が converge_tolerance_mm 以下になったら打ち切る。補正の適用自体は
    機械座標へ出る瞬間に1回だけ行う（このクラスは計測に徹する）。
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
        offset_transform: Transform,
        max_correction_mm: float | None = 1.0,
        max_passes: int = 5,
        converge_tolerance_mm: float = 0.005,
        settle_time: float = 0.5,
        frame_sink: FrameSink | None = None,
    ) -> None:
        """RegionAlignerを初期化する.

        Args:
            camera: カメラ
            klipper: Klipperクライアント
            stage: XYZステージ
            projector: 設計銅箔の投影器
            matcher: エッジ照合器
            edge_detector: 銅箔エッジ検出器
            offset_transform: 観測オフセット系から機械座標系への変換
            max_correction_mm: 1領域で許容する累積ずれ（mm）。
                超過は誤マッチとみなして照合失敗にする。Noneは無制限
            max_passes: 1領域あたりのパス数の上限（1以上）
            converge_tolerance_mm: このパス増分以下で収束とみなす（mm）
            settle_time: 移動後の安定待機時間（秒）
            frame_sink: 観測ごとに照合状況フレームを送る sink。
                Noneの場合は送らない
        """
        self._camera = camera
        self._klipper = klipper
        self._stage = stage
        self._projector = projector
        self._matcher = matcher
        self._edge_detector = edge_detector
        self._offset_transform = offset_transform
        self._max_correction_mm = max_correction_mm
        self._max_passes = max_passes
        self._converge_tolerance_mm = converge_tolerance_mm
        self._settle_time = settle_time
        self._frame_sink = frame_sink

        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def measure(self, region: AlignmentRegion) -> RegionAlignment:
        """累積変位を当てたアンカーで最大max_passes回まで計測する.

        Args:
            region: 対象領域

        Returns:
            領域の照合結果（累積変位・パス数・収束フラグ）

        Raises:
            RuntimeError: いずれかのパスで照合に失敗した場合、または累積変位が
                max_correction_mmを超えた場合
        """
        cumulative = Point2d(0.0, 0.0)
        passes = 0
        while True:
            passes += 1
            target = region.anchor + cumulative
            self._logger.info(
                "領域 %d の照合を開始 (%d パス目): anchor (%.4f, %.4f) mm",
                region.index,
                passes,
                target.x,
                target.y,
            )
            self._klipper.send_gcode(
                self._stage.move(x=target.x, y=target.y, speed=Speed.rate(0.5))
                + gcode.wait(self._settle_time)
                + gcode.wait_for_done()
            )

            # 累積変位をboard変換の後段へ挿す。これをやらないと同じ変位を二重に測る
            projector = (
                self._projector
                if passes == 1
                else self._projector.with_correction(Shift.from_point(cumulative))
            )
            projection = projector.project(target)
            image = self._camera.capture()
            edges = self._edge_detector.detect_edges(image)
            if self._frame_sink is not None:
                self._frame_sink(
                    render_edge_match(image, edges, projection.edge_mask, region.roi)
                )

            match = self._matcher.match(edges, projection.edge_mask, region.roi)
            if match is None:
                raise RuntimeError(
                    f"領域 {region.index} の銅箔エッジ照合に失敗しました"
                    f"（{passes} パス目・拘束不足または観測エッジなし）"
                )

            machine_transform = to_machine_transform(
                match.camera_transform,
                self._offset_transform,
                projection_anchor=target,
                observed_at=self._stage.get_position().to2d(),
            )
            # machine_transform は純並進（アンカーからの距離に依存しない）なので、
            # パスごとの増分は単純和で累積できる
            increment = machine_transform.apply(target) - target
            cumulative = cumulative + increment
            if (
                self._max_correction_mm is not None
                and cumulative.norm > self._max_correction_mm
            ):
                raise RuntimeError(
                    f"照合ずれ {cumulative.norm:.3f} mm が上限 "
                    f"{self._max_correction_mm} mm を超過しました（誤マッチの疑い）"
                )

            converged = increment.norm <= self._converge_tolerance_mm
            if converged or passes >= self._max_passes:
                return RegionAlignment(
                    region=region,
                    match=match,
                    displacement=cumulative,
                    increment=increment,
                    passes=passes,
                    converged=converged,
                )
