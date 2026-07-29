import math
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from functools import wraps
from pathlib import Path
from typing import ParamSpec, TypeVar, override

import cv2
import numpy as np
import picamera2
import pytest

from pcbasm.geometry import Point2d
from pcbasm.hal import Camera, CameraInfo, Resolution
from pcbasm.vision import CheckerboardView, Image
from pcbasm.vision.image import ImageArray

PROJECT_ROOT = Path(__file__).parent.parent

TESTING_DATA_DIR = PROJECT_ROOT / "data" / "testing"

# WebUI / E2E 用の config ディレクトリ fixture（Klipper port 7126 = 非リッスン）。
# コア層用の data/testing/machine.toml とは別物（用途差は data/config-templates/README.md 参照）
TESTING_CONFIG_DIR = TESTING_DATA_DIR / "config"

mark_hardware = pytest.mark.hardware


def copy_testing_config(tmp_path: Path) -> Path:
    """`data/testing/config` を `tmp_path/config` へ複製して返す（webui / e2e 共有）.

    テストが machine.toml を書き換えるため、追跡下の fixture を汚さないよう毎回コピーする。
    """
    config_dir = tmp_path / "config"
    shutil.copytree(TESTING_CONFIG_DIR, config_dir)
    return config_dir


_P = ParamSpec("_P")
_R = TypeVar("_R")


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 10.0,
    interval: float = 0.02,
) -> None:
    """条件が成立するまでポーリングする（タイミングのアサートはしない）.

    成立しないまま timeout を超えたら pytest.fail する。sleep 固定値依存のアサートを避けるための共有ポーラ。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    pytest.fail(f"{timeout}s 以内に条件が成立しませんでした")


def _usb_camera_available() -> bool:
    """USBカメラ（uvcvideoドライバー）が接続されているか確認する."""
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True,
            text=True,
        )
        return "uvcvideo" in result.stdout
    except Exception:
        return False


def _csi_camera_available() -> bool:
    """CSIカメラが接続されているか確認する."""
    return bool(picamera2.Picamera2.global_camera_info())


def _skip_if_camera_unavailable(
    is_available: Callable[[], bool],
    reason: str,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """実行時にカメラ接続を確認してテストをskipするdecoratorを返す."""

    def decorator(test: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(test)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            if not is_available():
                pytest.skip(reason)
            return test(*args, **kwargs)

        return wrapper

    return decorator


skip_if_no_usb_camera = _skip_if_camera_unavailable(
    _usb_camera_available,
    "USBカメラが接続されていません",
)

skip_if_no_csi_camera = _skip_if_camera_unavailable(
    _csi_camera_available,
    "CSIカメラが接続されていません",
)


class FakeCamera(Camera):
    """固定 Image 列を順に返すテスト用の Camera 実装.

    capture() のたびに与えられた画像を先頭から順に返し、 列を使い切った後は最後の画像を返し続ける。
    """

    def __init__(self, images: Sequence[Image], fps: float = 30.0) -> None:
        if not images:
            raise ValueError("imagesは1枚以上必要です")
        self._images = list(images)
        self._fps = fps
        self._index = 0

    @property
    @override
    def resolution(self) -> Resolution:
        first = self._images[0]
        return Resolution(width=first.width, height=first.height, fps=self._fps)

    @property
    @override
    def info(self) -> CameraInfo:
        return CameraInfo(name="FakeCamera", formats={"BGR": [self.resolution]})

    @override
    def capture(self) -> Image:
        image = self._images[min(self._index, len(self._images) - 1)]
        self._index += 1
        return image


# 1 マスの辺を何分割して多角形化するか。歪みで辺が曲がるため、直線 1 本で近似すると
# コーナー近傍の輪郭がずれて cornerSubPix の精度が落ちる。
_EDGE_SUBDIVISIONS = 4

# fillPoly の固定小数点シフト量（1/16 px 精度）。
_FILL_SHIFT = 4

# 既定のスーパーサンプリング倍率。fillPoly の塗り潰しは走査線境界で量子化するため、
# 等倍で描くと cornerSubPix の検出誤差が Y 方向で σ≈0.3px になる。8 倍 + INTER_AREA
# なら σ≈0.06px まで落ち、歪みマップ復元の検証がレンダリング雑音に埋もれない（実測）。
_SUPERSAMPLE = 8


class SyntheticCheckerboardCamera(Camera):
    """ステージ位置に応じた視点のチェッカーボードを既知の歪みで描画するテスト用カメラ.

    ボードは機械座標の原点を中心に静止しており、カメラがステージと一緒に動く
    （実機と同じ配置）。`cv2.projectPoints`（実 OpenCV）でコーナー画素座標を求め、
    各マスを歪んだ四辺形として `fillPoly` で塗る。`camera_matrix` / `dist_coeffs` を
    公開し ground truth に使う。

    焦点距離は既定で画像幅と同じ px 値にする。歪み係数は正規化座標に対して定義される
    ため、f を実機相当（OV9281 で概ね画像幅）に取らないと同じ `k1` でも歪み量が変わる。
    `pixel_per_mm` と f からボード距離 t_z = f / ppm が決まる。

    Attributes:
        camera_matrix: ground truth の 3x3 カメラ行列（float64）
        dist_coeffs: ground truth の歪み係数 (5,)（float64）
    """

    def __init__(
        self,
        *,
        position: Callable[[], Point2d],
        image_size: tuple[int, int] = (1280, 720),
        pattern_size: tuple[int, int] = (11, 8),
        square_size_mm: float = 1.5,
        pixel_per_mm: float = 30.31,
        dist_coeffs: tuple[float, float, float, float, float] = (
            -0.12,
            0.03,
            0.0,
            0.0,
            0.0,
        ),
        focal_px: float | None = None,
        mount_rotation_deg: float = 0.0,
        mirror_y: bool = False,
        supersample: int = _SUPERSAMPLE,
    ) -> None:
        """合成カメラを構成する.

        Args:
            position: 現在のステージ XY [mm] を返す callable（撮影時に評価される）
            image_size: フレームサイズ (width, height)
            pattern_size: 内部コーナー数 (cols, rows)。マス目は (cols+1, rows+1)
            square_size_mm: 1 マスの辺長 [mm]
            pixel_per_mm: 補正後フレームで成立させたい pixel/mm
            dist_coeffs: (k1, k2, p1, p2, k3)
            focal_px: 焦点距離 [px]。None なら画像幅
            mount_rotation_deg: ステージ軸に対するカメラの取付回転角 [deg]
            mirror_y: True なら画像 y 軸を機械 Y 軸と逆向きにする（実機の下向き
                カメラのジオメトリ）。px↔mm 写像の行列式が負になるので、回転と
                等方スケールだけのモデルでは表現できない配置になる
            supersample: 描画時のスーパーサンプリング倍率
        """
        width, height = image_size
        focal = float(width) if focal_px is None else focal_px

        self._position = position
        self._image_size = image_size
        self._pattern_size = pattern_size
        self._square_size_mm = square_size_mm
        self._pixel_per_mm = pixel_per_mm
        self._mount_rotation_deg = mount_rotation_deg
        self._supersample = supersample
        self._distance_mm = focal / pixel_per_mm

        self._camera_matrix = np.array(
            [[focal, 0.0, width / 2.0], [0.0, focal, height / 2.0], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )
        self._dist_coeffs = np.array(dist_coeffs, dtype=np.float64)

        theta = math.radians(mount_rotation_deg)
        rotation = np.array(
            [
                [math.cos(theta), -math.sin(theta)],
                [math.sin(theta), math.cos(theta)],
            ],
            dtype=np.float64,
        )
        # 鏡映は回転の前に掛ける（機械 Y を反転してからカメラ取付角で回す）
        self._rotation = rotation @ np.diag([1.0, -1.0]) if mirror_y else rotation

        cols, rows = pattern_size
        self._squares = (cols + 1, rows + 1)
        self._origin_mm = (
            -self._squares[0] * square_size_mm / 2.0,
            -self._squares[1] * square_size_mm / 2.0,
        )

    @property
    def camera_matrix(self) -> ImageArray:
        """Ground truth の 3x3 カメラ行列."""
        return self._camera_matrix.copy()

    @property
    def dist_coeffs(self) -> ImageArray:
        """Ground truth の歪み係数 (5,)."""
        return self._dist_coeffs.copy()

    @property
    def pattern_size(self) -> tuple[int, int]:
        """内部コーナー数 (cols, rows)."""
        return self._pattern_size

    @property
    def square_size_mm(self) -> float:
        """1 マスの辺長 [mm]."""
        return self._square_size_mm

    @property
    def pixel_per_mm(self) -> float:
        """補正後フレームで成立する pixel/mm（ground truth）."""
        return self._pixel_per_mm

    @property
    @override
    def resolution(self) -> Resolution:
        width, height = self._image_size
        return Resolution(width=width, height=height, fps=30.0)

    @property
    @override
    def info(self) -> CameraInfo:
        return CameraInfo(
            name="SyntheticCheckerboardCamera", formats={"BGR": [self.resolution]}
        )

    def corner_points_mm(self) -> ImageArray:
        """内部コーナーのボード座標 (N,2) [mm]（行優先、cv2 と同じ並び）."""
        cols, rows = self._pattern_size
        origin_x, origin_y = self._origin_mm
        square = self._square_size_mm
        points = [
            (origin_x + (c + 1) * square, origin_y + (r + 1) * square)
            for r in range(rows)
            for c in range(cols)
        ]
        return np.array(points, dtype=np.float64)

    def project_points(self, board_points_mm: ImageArray, stage: Point2d) -> ImageArray:
        """ボード座標 (N,2) [mm] を指定ステージ位置での歪んだ画素座標 (N,2) へ写す."""
        board = np.asarray(board_points_mm, dtype=np.float64).reshape(-1, 2)
        relative = board - np.array([stage.x, stage.y], dtype=np.float64)
        rotated = relative @ self._rotation.T
        objects = np.column_stack(
            [rotated, np.full(len(rotated), self._distance_mm, dtype=np.float64)]
        )
        projected, _ = cv2.projectPoints(
            objects,
            np.zeros(3, dtype=np.float64),
            np.zeros(3, dtype=np.float64),
            self._camera_matrix,
            self._dist_coeffs,
        )
        return projected.reshape(-1, 2)

    def project_corners(self, stage: Point2d) -> ImageArray:
        """指定ステージ位置での内部コーナー画素座標を (N,1,2) float32 で返す.

        `CheckerboardView.corners` と同じ形式なので、画像レンダリングを経由せずに
        解析的な視点を組み立てられる。
        """
        points = self.project_points(self.corner_points_mm(), stage)
        return points.reshape(-1, 1, 2).astype(np.float32)

    def project_view(self, stage: Point2d) -> CheckerboardView:
        """指定ステージ位置の視点を画像レンダリングを経由せずに組み立てる.

        検出誤差を混ぜずに残差の数学だけを検証したいケース（および描画テストの 素材づくり）で使う。
        """
        return CheckerboardView(
            stage_position=stage,
            corners=self.project_corners(stage),
            pattern_size=self._pattern_size,
            image_size=self._image_size,
        )

    @override
    def capture(self) -> Image:
        return self.render(self._position())

    def render(self, stage: Point2d) -> Image:
        """指定ステージ位置から見たチェッカーボードを描画する."""
        width, height = self._image_size
        ratio = self._supersample
        canvas = np.full((height * ratio, width * ratio), 255, dtype=np.uint8)

        square = self._square_size_mm
        origin_x, origin_y = self._origin_mm
        columns, rows = self._squares
        scale = 1 << _FILL_SHIFT

        for row in range(rows):
            for column in range(columns):
                if (row + column) % 2 != 0:
                    continue
                x0 = origin_x + column * square
                y0 = origin_y + row * square
                outline = self._square_outline_mm(x0, y0, square)
                pixels = self.project_points(outline, stage)
                # 画素中心を整数座標とする規約のまま拡大する（半画素ずれを避ける）
                polygon = np.round(((pixels + 0.5) * ratio - 0.5) * scale).astype(
                    np.int32
                )
                cv2.fillPoly(
                    canvas, [polygon], 0, lineType=cv2.LINE_AA, shift=_FILL_SHIFT
                )

        if ratio > 1:
            canvas = cv2.resize(canvas, (width, height), interpolation=cv2.INTER_AREA)
        return Image(canvas)

    @staticmethod
    def _square_outline_mm(x0: float, y0: float, square: float) -> ImageArray:
        """1 マスの輪郭を細分割した多角形（ボード座標 mm）として返す."""
        steps = np.linspace(0.0, square, _EDGE_SUBDIVISIONS + 1)[:-1]
        top = [(x0 + s, y0) for s in steps]
        right = [(x0 + square, y0 + s) for s in steps]
        bottom = [(x0 + square - s, y0 + square) for s in steps]
        left = [(x0, y0 + square - s) for s in steps]
        return np.array(top + right + bottom + left, dtype=np.float64)
