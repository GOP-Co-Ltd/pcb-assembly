import warnings

import cv2
import cv2.typing
import numpy as np

type HWC = tuple[int, int, int]  # Height, Width, Channels
type Image = np.ndarray[HWC, np.dtype[np.uint8]]


class Camera:
    """カメラデバイスの抽象化クラス."""

    def __init__(
        self,
        device_id: int = 0,
        width: int = 640,
        height: int = 480,
        fps: float = 30.0,
    ) -> None:
        """カメラを初期化して接続する."""

        self._device_id = device_id
        self._width = width
        self._height = height
        self._fps = fps

        self._cam = self._open_camera()

    def _open_camera(self) -> cv2.VideoCapture:
        cam = cv2.VideoCapture(self._device_id)
        if not cam.isOpened():
            raise RuntimeError(f"カメラ {self._device_id} を開けません")

        if not cam.set(cv2.CAP_PROP_FRAME_WIDTH, self._width):
            warnings.warn(f"幅 {self._width} を設定できません", RuntimeWarning)
        if not cam.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height):
            warnings.warn(f"高さ {self._height} を設定できません", RuntimeWarning)
        if not cam.set(cv2.CAP_PROP_FPS, self._fps):
            warnings.warn(f"FPS {self._fps} を設定できません", RuntimeWarning)

        return cam

    def capture(self) -> Image:
        """1フレームをキャプチャして返す."""
        ret, img = self._cam.read()
        if ret:
            return self._fix_captured_image(img)
        raise RuntimeError("フレームの取得に失敗しました")

    def _fix_captured_image(self, image: cv2.typing.MatLike) -> Image:
        if image.shape[:2] != (self._height, self._width):
            warnings.warn(
                f"取得した画像のサイズ ({image.shape[1]}x{image.shape[0]})が"
                f"想定 ({self._width}x{self._height})と異なります。"
                "リサイズします。",
                RuntimeWarning,
            )
            image = cv2.resize(image, (self._width, self._height))
        match len(image.shape):
            case 3:
                pass
            case 2:
                image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
            case _:
                raise ValueError(f"不正な画像形状: {image.shape}")
        return image.astype(np.uint8)
