"""`pcbasm.posctrl.checkerboard_scan` の仕様テスト.

計画書「カメラキャリブレーションのレンズ歪み補正対応」§5 §7 が契約:

- 移動は**絶対座標**（開始位置 + `grid.positions[i]`）。`CheckerboardView.stage_position`
  および `ScanProgress.stage_position` にはコマンドした絶対 XY が入る
- 移動作法は `OffsetTransformMeasurer._move_to` と同形。`stage.move` の G-code に
  settle の待機と `M400` を連結して `klipper.send_gcode` に 1 回で送る
- `on_view` は成功・失敗の**両方で毎点**呼ばれる（`annotated` は成功時のみ非 None）
- 検出失敗はスキップして継続し `failures` に記録する
- `on_view` の例外（実運用では `JobAborted`）は伝播し、`finally` で開始位置へ復帰する
- パターンサイズは呼び出し側が固定する。総当たり探索は 1 視点 5.45 秒かかるため
  設計上禁止（速度要件）で、視点間で size がぶれると objectPoints の対応が壊れる

`Klipper` / `XYZStage` は自前 HAL なので `mocker.Mock` を使い（先例
`tests/pcbasm/posctrl/test_offset.py`）、`stage.move` の side effect でテスト側の
位置モデルを更新する。カメラは `SyntheticCheckerboardCamera` にその位置モデルを
参照させることで「ステージ位置に応じて見え方が変わるカメラ」を作る。3rd-party 表面
（OpenCV）はモックせず実物を通す。

歪み復元の精度検証は `tests/pcbasm/vision/test_calibration.py::TestDistortionRecovery`
の担当なので、ここではスキャンの制御フローだけを見る（合成フレームも軽い設定にする）。
"""

from typing import override

import numpy as np
import pytest
from pytest_mock import MockerFixture

from pcbasm import gcode
from pcbasm.geometry import Point2d, Point3d
from pcbasm.hal import Camera, CameraInfo, Resolution
from pcbasm.posctrl.checkerboard_scan import CheckerboardScanner
from pcbasm.vision import Image, ScanGrid
from tests.helpers import SyntheticCheckerboardCamera

# ステージの開始位置。原点から離しておくことで「絶対座標でコマンドしている」ことを
# 相対移動と区別できる
START = Point3d(12.0, -34.0, -5.5)

# 軽量な合成フレーム設定（制御フローの検証に精度は要らない）
IMAGE_SIZE = (480, 360)
PATTERN_SIZE = (7, 5)
SQUARE_SIZE_MM = 1.5
PIXEL_PER_MM = 20.0
SUPERSAMPLE = 4

SETTLE_TIME = 0.5


class _StageModel:
    """`stage.move` でコマンドされた位置を保持する fake stage の位置モデル.

    合成カメラはチェッカーボードが**開始位置に置かれている**ものとして描画するため、
    カメラへ渡すのは開始位置からの変位（`view_offset`）にする。
    """

    def __init__(self, start: Point3d = START) -> None:
        self.position = start

    def move(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        *,
        speed: object = None,
        relative: bool = False,
    ) -> gcode.GCode:
        """XYZStage.move と同じ引数を受け、位置モデルを更新して G-code を返す."""
        current = self.position
        if relative:
            self.position = Point3d(
                current.x + (x or 0.0), current.y + (y or 0.0), current.z + (z or 0.0)
            )
        else:
            self.position = Point3d(
                current.x if x is None else x,
                current.y if y is None else y,
                current.z if z is None else z,
            )
        return gcode.GCode("G1 MOVE")

    def view_offset(self) -> Point2d:
        """開始位置からの XY 変位（合成カメラの視点）."""
        return Point2d(self.position.x - START.x, self.position.y - START.y)


class _BlindCamera(Camera):
    """指定した撮像回だけ検出不能な無地フレームを返すラッパー."""

    def __init__(self, base: Camera, blind_indices: set[int]) -> None:
        self._base = base
        self._blind_indices = blind_indices
        self._count = 0

    @property
    @override
    def resolution(self) -> Resolution:
        return self._base.resolution

    @property
    @override
    def info(self) -> CameraInfo:
        return CameraInfo(name="BlindCamera", formats={"BGR": [self.resolution]})

    @override
    def capture(self) -> Image:
        index = self._count
        self._count += 1
        if index in self._blind_indices:
            width, height = IMAGE_SIZE
            return Image(np.full((height, width, 3), 128, dtype=np.uint8))
        return self._base.capture()


def _absolute(position: Point2d) -> Point2d:
    """相対格子点をコマンドされるべき絶対 XY へ写す."""
    return Point2d(START.x + position.x, START.y + position.y)


def _commanded_xy(call) -> Point2d:
    """`stage.move` の呼び出しから指定された XY を取り出す."""
    return Point2d(call.kwargs["x"], call.kwargs["y"])


class TestCheckerboardScanner:
    """ステージ格子スキャンの制御フロー."""

    @pytest.fixture
    def model(self) -> _StageModel:
        return _StageModel()

    @pytest.fixture
    def klipper(self, mocker: MockerFixture):
        return mocker.Mock()

    @pytest.fixture
    def stage(self, mocker: MockerFixture, model: _StageModel):
        stage = mocker.Mock()
        stage.max_velocity = 100.0
        stage.get_position.side_effect = lambda: model.position
        stage.move.side_effect = model.move
        return stage

    @pytest.fixture
    def camera(self, model: _StageModel) -> SyntheticCheckerboardCamera:
        return SyntheticCheckerboardCamera(
            position=model.view_offset,
            image_size=IMAGE_SIZE,
            pattern_size=PATTERN_SIZE,
            square_size_mm=SQUARE_SIZE_MM,
            pixel_per_mm=PIXEL_PER_MM,
            supersample=SUPERSAMPLE,
        )

    @pytest.fixture
    def grid(self, camera: SyntheticCheckerboardCamera) -> ScanGrid:
        planned = ScanGrid.plan(
            image_size=IMAGE_SIZE,
            corners=camera.project_corners(Point2d(0.0, 0.0)),
            pattern_size=PATTERN_SIZE,
            pixel_per_mm=PIXEL_PER_MM,
        )
        assert planned is not None
        return planned

    @pytest.fixture
    def scanner(self, klipper, stage, camera: Camera) -> CheckerboardScanner:
        return CheckerboardScanner(
            klipper,
            stage,
            camera,
            pattern_size=PATTERN_SIZE,
            settle_time=SETTLE_TIME,
        )

    def test_visits_every_grid_point_in_absolute_coordinates(
        self, scanner: CheckerboardScanner, grid: ScanGrid, stage
    ):
        """15 点すべてで視点が得られ、コマンド位置が開始位置 + 相対格子と一致する."""
        outcome = scanner.scan(grid)

        assert len(grid.positions) == 15
        assert len(outcome.views) == 15
        assert outcome.failures == ()
        expected = [_absolute(position) for position in grid.positions]
        commanded = [_commanded_xy(call) for call in stage.move.call_args_list[:15]]
        for actual, wanted in zip(commanded, expected, strict=True):
            assert actual.x == pytest.approx(wanted.x)
            assert actual.y == pytest.approx(wanted.y)
        for view, wanted in zip(outcome.views, expected, strict=True):
            assert view.stage_position.x == pytest.approx(wanted.x)
            assert view.stage_position.y == pytest.approx(wanted.y)
            assert view.pattern_size == PATTERN_SIZE
            assert view.image_size == IMAGE_SIZE

    def test_scan_moves_do_not_touch_z(
        self, scanner: CheckerboardScanner, grid: ScanGrid, stage
    ):
        """操作者が合わせたフォーカス Z を破壊しない（巡回中は Z を出さない）."""
        scanner.scan(grid)

        for call in stage.move.call_args_list[:15]:
            assert call.kwargs.get("z") is None

    def test_returns_to_the_start_position_after_the_scan(
        self, scanner: CheckerboardScanner, grid: ScanGrid, stage, model: _StageModel
    ):
        """巡回後に開始位置へ絶対移動で復帰する."""
        scanner.scan(grid)

        assert stage.move.call_count == len(grid.positions) + 1
        back = stage.move.call_args_list[-1].kwargs
        assert back["x"] == pytest.approx(START.x)
        assert back["y"] == pytest.approx(START.y)
        assert back["z"] == pytest.approx(START.z)
        assert back.get("relative") is not True
        assert model.position == START

    def test_each_move_is_sent_once_with_settle_and_wait_for_done(
        self, scanner: CheckerboardScanner, grid: ScanGrid, klipper
    ):
        """移動 + settle 待機 + M400 を 1 回の送信でまとめる（offset.py と同形）."""
        scanner.scan(grid)

        assert klipper.send_gcode.call_count == len(grid.positions) + 1
        for call in klipper.send_gcode.call_args_list:
            sent = str(call.args[0])
            assert "G4 P500" in sent
            assert "M400" in sent

    def test_on_view_is_called_for_every_point_with_progress(
        self, scanner: CheckerboardScanner, grid: ScanGrid
    ):
        """`on_view` が毎点呼ばれ index / total / annotated / raw が埋まる."""
        progress = []

        outcome = scanner.scan(grid, on_view=progress.append)

        assert [item.index for item in progress] == list(range(len(grid.positions)))
        assert {item.total for item in progress} == {len(grid.positions)}
        for item, view in zip(progress, outcome.views, strict=True):
            assert item.stage_position == view.stage_position
            assert item.raw.size == IMAGE_SIZE
            assert item.annotated is not None
            assert item.annotated.size == IMAGE_SIZE
            assert not np.array_equal(item.annotated.numpy(), item.raw.numpy())

    def test_undetectable_points_are_recorded_and_the_scan_continues(
        self, klipper, stage, camera: Camera, grid: ScanGrid
    ):
        """検出できない点だけ failures に入り、残りの点は巡回を続ける."""
        blind_indices = {2, 5}
        scanner = CheckerboardScanner(
            klipper,
            stage,
            _BlindCamera(camera, blind_indices),
            pattern_size=PATTERN_SIZE,
            settle_time=SETTLE_TIME,
        )
        progress = []

        outcome = scanner.scan(grid, on_view=progress.append)

        total = len(grid.positions)
        assert len(outcome.views) == total - len(blind_indices)
        assert [failure.index for failure in outcome.failures] == sorted(blind_indices)
        for failure in outcome.failures:
            assert failure.stage_position == _absolute(grid.positions[failure.index])
            assert failure.raw.size == IMAGE_SIZE
        assert len(progress) == total
        assert [item.index for item in progress if item.annotated is None] == sorted(
            blind_indices
        )
        assert stage.move.call_count == total + 1

    def test_on_view_exception_propagates_and_still_returns_to_start(
        self, scanner: CheckerboardScanner, grid: ScanGrid, stage, model: _StageModel
    ):
        """`on_view` の例外は伝播し、finally で開始位置へ復帰する（中止経路）."""

        def abort(progress) -> None:
            if progress.index == 1:
                raise RuntimeError("ジョブが中止されました")

        with pytest.raises(RuntimeError) as exc:
            scanner.scan(grid, on_view=abort)

        assert "中止" in str(exc.value)
        back = stage.move.call_args_list[-1].kwargs
        assert back["x"] == pytest.approx(START.x)
        assert back["y"] == pytest.approx(START.y)
        assert model.position == START

    def test_detection_uses_only_the_given_pattern_size(
        self, klipper, stage, camera: Camera, grid: ScanGrid
    ):
        """与えられた pattern_size でのみ検出する（総当たり探索をしない速度契約）.

        盤の内部コーナーは 7x5 なので、別サイズを指定したら 1 点も検出できない。
        フォールバックで他サイズを探索する実装ならここで views が埋まってしまう。
        """
        scanner = CheckerboardScanner(
            klipper,
            stage,
            camera,
            pattern_size=(9, 6),
            settle_time=SETTLE_TIME,
        )

        outcome = scanner.scan(grid)

        assert outcome.views == ()
        assert len(outcome.failures) == len(grid.positions)
