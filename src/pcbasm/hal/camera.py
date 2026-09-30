import abc
import os
import re
import stat
import subprocess
import warnings
from typing import override

import attrs
import cv2
import cv2.typing
import picamera2

from pcbasm.vision import Image


def _device_id_to_path(device_id: int) -> str:
    return f"/dev/video{device_id}"


@attrs.frozen
class Resolution:
    """解像度とFPSの組み合わせ."""

    width: int
    height: int
    fps: float

    @property
    def size(self) -> tuple[int, int]:
        """画像サイズを返します。"""
        return self.width, self.height


@attrs.frozen
class CameraInfo:
    """カメラのメタデータ."""

    name: str
    formats: dict[str, list[Resolution]]

    def has_format(self, format: str, resolution: Resolution) -> bool:
        """指定のフォーマットと解像度をサポートしているか."""
        if format not in self.formats:
            return False
        return resolution in self.formats[format]

    def format_text(self) -> str:
        """サポートされているフォーマットを見やすいテキストで返す."""
        lines = []
        for fmt, resolutions in self.formats.items():
            lines.append(f"  {fmt}:")
            for res in resolutions:
                lines.append(f"    - {res.width}x{res.height}@{res.fps}fps")
        return "\n".join(lines)


def get_camera_info(device_id: int = 0) -> CameraInfo:
    """v4l2-ctlを使用してカメラのメタデータを取得する."""
    device_path = _device_id_to_path(device_id)

    # カメラ名を取得
    result = subprocess.run(
        ["v4l2-ctl", "-d", device_path, "--all"],
        capture_output=True,
        text=True,
        check=True,
    )
    name_match = re.search(r"Card type\s*:\s*(.+)", result.stdout)
    name = name_match.group(1).strip() if name_match else "Unknown"

    # 解像度とFPSを取得
    result = subprocess.run(
        ["v4l2-ctl", "-d", device_path, "--list-formats-ext"],
        capture_output=True,
        text=True,
        check=True,
    )

    formats: dict[str, list[Resolution]] = {}
    current_format: str | None = None
    lines = iter(result.stdout.splitlines())

    for line in lines:
        # "[0]: 'MJPG' (Motion-JPEG, compressed)" のパターン
        if format_match := re.search(r"\[\d+\]:\s*'(\w+)'", line):
            current_format = str(format_match.group(1))
            formats[current_format] = []
            continue

        # フォーマットが未指定なら解像度・FPSはスキップ
        if current_format:
            # "Size: Discrete 1280x720" のパターン、次行にFPSがある
            if size_match := re.search(r"Size:\s*Discrete\s*(\d+)x(\d+)", line):
                width = int(size_match.group(1))
                height = int(size_match.group(2))
                next_line = next(lines, "")
                fps_match = re.search(r"\((\d+(?:\.\d+)?)\s*fps\)", next_line)
                fps = float(fps_match.group(1)) if fps_match else 0.0
                formats[current_format].append(Resolution(width, height, fps))

    return CameraInfo(name=name, formats=formats)


class Camera(abc.ABC):
    """カメラデバイスの抽象基底クラス."""

    @property
    @abc.abstractmethod
    def resolution(self) -> Resolution:
        """現在の解像度とFPSを返す."""

    @property
    @abc.abstractmethod
    def info(self) -> CameraInfo:
        """カメラのメタデータを返す."""

    @abc.abstractmethod
    def capture(self) -> Image:
        """1フレームをキャプチャして返す（BGR）。取得に失敗したら例外を送出する."""


class _UsbCamera(Camera):
    """USBカメラ（V4L2）実装."""

    def __init__(
        self,
        device_id: int = 0,
        width: int = 640,
        height: int = 480,
        fps: float = 30.0,
        format: str | None = None,
    ) -> None:
        self._validate_device_id(device_id)

        self._device_id = device_id
        self._width = width
        self._height = height
        self._fps = fps

        self._info = get_camera_info(device_id)

        if len(self._info.formats) == 0:
            raise RuntimeError("カメラがサポートするフォーマットがありません")

        if format is None:
            format = self._get_default_format(self._info)

        if len(format) != 4:
            raise ValueError(f"フォーマットは4文字である必要があります: {format}")

        if not self._info.has_format(format, self.resolution):
            raise RuntimeError(
                f"カメラは {format} {width}x{height}@{fps}fps をサポートしていません。\n"
                f"サポートされているフォーマット:\n{self._info.format_text()}"
            )

        self._format = format
        self._cam = self._open_camera()

    @property
    @override
    def resolution(self) -> Resolution:
        return Resolution(self._width, self._height, self._fps)

    @property
    @override
    def info(self) -> CameraInfo:
        return self._info

    def _get_default_format(self, info: CameraInfo) -> str:
        return list(info.formats.keys())[0]

    def _open_camera(self) -> cv2.VideoCapture:
        cam = cv2.VideoCapture(self._device_id)
        if not cam.isOpened():
            raise RuntimeError(f"カメラ {self._device_id} を開けません")

        if not cam.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*self._format)):
            warnings.warn(
                f"フォーマット {self._format} を設定できません", RuntimeWarning
            )

        if not cam.set(cv2.CAP_PROP_FRAME_WIDTH, self._width):
            warnings.warn(f"幅 {self._width} を設定できません", RuntimeWarning)
        if not cam.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height):
            warnings.warn(f"高さ {self._height} を設定できません", RuntimeWarning)
        if not cam.set(cv2.CAP_PROP_FPS, self._fps):
            warnings.warn(f"FPS {self._fps} を設定できません", RuntimeWarning)

        return cam

    def _validate_device_id(self, device_id: int) -> None:
        """デバイスIDがカメラデバイスとして有効か検証する."""
        device_path = _device_id_to_path(device_id)

        # デバイスファイルが存在するか
        if not os.path.exists(device_path):
            raise OSError(f"デバイス {device_path} が存在しません")

        # キャラクタデバイスか
        if not stat.S_ISCHR(os.stat(device_path).st_mode):
            raise OSError(f"{device_path} はキャラクタデバイスではありません")

        # Video Capture機能を持つか
        result = subprocess.run(
            ["v4l2-ctl", "-d", device_path, "--all"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise OSError(f"{device_path} はV4L2デバイスではありません")

        if "Video Capture" not in result.stdout:
            raise OSError(f"{device_path} はビデオキャプチャデバイスではありません")

    @override
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
        return Image(image)


class _CsiCamera(Camera):
    """CSIカメラ（picamera2）実装."""

    def __init__(
        self,
        camera_num: int = 0,
        width: int = 640,
        height: int = 480,
        fps: float = 30.0,
    ) -> None:
        cameras = picamera2.Picamera2.global_camera_info()
        if camera_num >= len(cameras):
            raise OSError(f"CSIカメラ {camera_num} が見つかりません")

        self._width = width
        self._height = height
        self._fps = fps

        name = cameras[camera_num].get("Model", "CSI Camera")
        self._info = CameraInfo(
            name=name,
            formats={"BGR": [Resolution(width, height, fps)]},
        )

        self._picam2 = picamera2.Picamera2(camera_num)
        config = self._picam2.create_video_configuration(
            main={"size": (width, height), "format": "BGR888"},
            controls={"FrameRate": fps},
        )
        self._picam2.configure(config)
        self._picam2.start()

    @property
    @override
    def resolution(self) -> Resolution:
        return Resolution(self._width, self._height, self._fps)

    @property
    @override
    def info(self) -> CameraInfo:
        return self._info

    @override
    def capture(self) -> Image:
        """1フレームをキャプチャして返す."""
        frame = self._picam2.capture_array("main")
        return Image(frame)

    def __del__(self) -> None:
        if hasattr(self, "_picam2"):
            self._picam2.stop()
            self._picam2.close()


def create_camera(
    device_id: int = 0,
    width: int = 1280,
    height: int = 720,
    fps: float = 30.0,
    format: str | None = None,
    backend: str = "csi",
) -> Camera:
    """バックエンドを指定してカメラを生成するファクトリ関数.

    Args:
        device_id: デバイスID。USBカメラでは /dev/video{device_id}、
                   CSIカメラではカメラポート番号（0 または 1）。
        width: 解像度の幅。
        height: 解像度の高さ。
        fps: フレームレート。
        format: フォーマットコード（4文字）。USB バックエンドのみ有効。
        backend: カメラバックエンド。"csi"（デフォルト）または "usb"。

    Raises:
        OSError: デバイスが見つからない場合
        RuntimeError: USB カメラが指定のフォーマット・解像度に対応しない場合
        ValueError: 未知のバックエンドの場合
    """
    match backend:
        case "usb":
            return _UsbCamera(
                device_id=device_id,
                width=width,
                height=height,
                fps=fps,
                format=format,
            )
        case "csi":
            return _CsiCamera(
                camera_num=device_id,
                width=width,
                height=height,
                fps=fps,
            )
        case _:
            raise ValueError(
                f"未知のバックエンド: {backend}。'usb' または 'csi' を指定してください"
            )
