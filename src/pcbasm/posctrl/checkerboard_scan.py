"""チェッカーボードを固定したままステージを蛇行させ、多視点のコーナーを集める.

``cv2.calibrateCamera`` に必要な多視点は、ボードを手で傾けるのではなく XY ステージの
自動移動で稼ぐ。ステージを動かす層なので ``vision`` ではなく ``posctrl`` に置く
（``vision`` はハードウェア制御を行わない）。
"""

import logging
from collections.abc import Callable

import attrs

from pcbasm import gcode
from pcbasm.geometry import Point2d
from pcbasm.hal import Camera, Klipper, Speed, XYZStage
from pcbasm.utils import get_class_module_path
from pcbasm.vision import CheckerboardDetector, CheckerboardView, Image, ScanGrid


@attrs.frozen(eq=False)
class ScanProgress:
    """1 視点ぶんの進捗（成功・失敗の両方で通知される。Image を持つので永続化しない）.

    Attributes:
        index: ``ScanGrid.positions`` 内の位置（0 始まり）
        total: 訪問する総点数
        stage_position: コマンドしたステージ XY [mm]（絶対）
        annotated: コーナーを描画した画像。検出できなかった視点では None
        raw: 撮像したフレーム
    """

    index: int
    total: int
    stage_position: Point2d
    annotated: Image | None
    raw: Image


@attrs.frozen(eq=False)
class ScanFailure:
    """コーナーを検出できなかった視点（Image を持つので永続化しない）.

    Attributes:
        index: ``ScanGrid.positions`` 内の位置（0 始まり）
        stage_position: コマンドしたステージ XY [mm]（絶対）
        raw: 撮像したフレーム（診断用に保存する）
    """

    index: int
    stage_position: Point2d
    raw: Image


@attrs.frozen(eq=False)
class ScanOutcome:
    """スキャンの結果.

    Attributes:
        views: 検出できた視点
        failures: 検出できなかった視点
    """

    views: tuple[CheckerboardView, ...]
    failures: tuple[ScanFailure, ...]


class CheckerboardScanner:
    """ステージを格子状に巡回してチェッカーボードを多視点撮像する.

    Z は一切動かさない（操作者が合わせたフォーカス Z を壊さない）。移動は開始位置
    からの絶対座標で行い、誤差の累積を避ける。終了時は ``finally`` で必ず開始位置へ
    戻る。

    Example:
        scanner = CheckerboardScanner(
            klipper, stage, camera, pattern_size=detector.pattern_size
        )
        outcome = scanner.scan(grid, on_view=report_progress)
    """

    def __init__(
        self,
        klipper: Klipper,
        stage: XYZStage,
        camera: Camera,
        *,
        pattern_size: tuple[int, int],
        settle_time: float = 0.5,
        speed_ratio: float = 0.5,
    ) -> None:
        """スキャナを初期化する.

        Args:
            klipper: Klipperクライアント
            stage: XYZステージ
            camera: 撮像するカメラ（歪み補正前のフレームを返すもの）
            pattern_size: 計画用ショットで確定した内部コーナー数 (cols, rows)。
                総当たり探索は 1 視点 5.45 秒かかるため、検出器はこのサイズだけを試す
            settle_time: 移動後の安定待機時間（秒）
            speed_ratio: 最大速度に対する移動速度の割合 (0.0-1.0)
        """
        self._klipper = klipper
        self._stage = stage
        self._camera = camera
        self._settle_time = settle_time
        self._speed_ratio = speed_ratio

        columns, rows = pattern_size
        self._detector = CheckerboardDetector(
            pattern_rows_range=(rows, rows + 1),
            pattern_cols_range=(columns, columns + 1),
        )

        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def scan(
        self,
        grid: ScanGrid,
        on_view: Callable[[ScanProgress], None] | None = None,
    ) -> ScanOutcome:
        """格子の各点へ移動して撮像・検出する.

        検出できなかった点はスキップして継続し ``failures`` に記録する。``on_view``
        は成功・失敗にかかわらず毎点呼ばれ、そこで送出された例外（ジョブの中止など）
        は伝播させる。いずれの経路でも開始位置へ復帰する。

        Args:
            grid: 開始位置を基準とする相対 XY 格子（蛇行順）
            on_view: 1 点ごとの進捗コールバック

        Returns:
            検出できた視点と失敗した視点
        """
        start = self._stage.get_position()
        self._logger.info(
            f"チェッカーボードスキャン開始: {len(grid.positions)}点"
            f" ({grid.columns}x{grid.rows}), 移動幅"
            f" X={grid.span_mm[0]:.2f}mm Y={grid.span_mm[1]:.2f}mm, 開始位置 {start}"
        )

        views: list[CheckerboardView] = []
        failures: list[ScanFailure] = []
        try:
            for index, position in enumerate(grid.positions):
                target = Point2d(start.x + position.x, start.y + position.y)
                self._move_to(x=target.x, y=target.y)

                raw = self._camera.capture()
                view = self._detector.detect(raw, target)
                if view is None:
                    self._logger.warning(
                        f"視点{index}でコーナーを検出できません: {target}"
                    )
                    failures.append(
                        ScanFailure(index=index, stage_position=target, raw=raw)
                    )
                else:
                    views.append(view)

                if on_view is not None:
                    on_view(
                        ScanProgress(
                            index=index,
                            total=len(grid.positions),
                            stage_position=target,
                            annotated=self._detector.draw(raw, view)
                            if view is not None
                            else None,
                            raw=raw,
                        )
                    )
        finally:
            self._logger.info("開始位置に戻る")
            self._move_to(x=start.x, y=start.y, z=start.z)

        self._logger.info(
            f"スキャン完了: 有効視点 {len(views)}/{len(grid.positions)}"
            f"（失敗 {len(failures)}）"
        )
        return ScanOutcome(views=tuple(views), failures=tuple(failures))

    def _move_to(self, *, x: float, y: float, z: float | None = None) -> None:
        """指定座標へ絶対移動し、安定を待つ（``z=None`` なら Z 軸は出力しない）."""
        move = self._stage.move(
            x=x,
            y=y,
            z=z,
            speed=Speed.absolute(self._stage.max_velocity * self._speed_ratio),
        )
        self._klipper.send_gcode(
            move + gcode.wait(self._settle_time) + gcode.wait_for_done()
        )
