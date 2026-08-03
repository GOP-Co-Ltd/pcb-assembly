import shutil
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from functools import wraps
from pathlib import Path
from secrets import token_hex
from typing import ParamSpec, TypeVar, override

import picamera2
import pytest

from pcbasm.hal import Camera, CameraInfo, Resolution
from pcbasm.vision import Image

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


def before_deadline[T](
    call: Callable[[], T], *, what: str = "応答", deadline: float = 15.0
) -> T:
    """`deadline` 以内に返らなければ失敗させる（ハングをテスト失敗に変える）.

    ブロッキングする待ち（`TestClient` の WS receive / `httpx` のストリーム読み）は
    `pytest.mark.timeout` では中断できず、テストが「落ちる」のではなく「終わらない」。
    締め切りをテスト側に持たせるための共有ヘルパ（daemon スレッドで走らせて join する）。
    """
    outcome: list[T] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            outcome.append(call())
        except BaseException as exc:  # noqa: BLE001 - 呼び出し元へそのまま送り直す
            failures.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(deadline)
    if failures:
        raise failures[0]
    if not outcome:
        pytest.fail(f"{deadline}s 以内に{what}が返りませんでした")
    return outcome[0]


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


def _skip_unless_available(
    is_available: Callable[[], bool],
    reason: str,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """実行時に能力（カメラ接続・mDNS 可否）を確認してテストをskipするdecoratorを返す."""

    def decorator(test: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(test)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            if not is_available():
                pytest.skip(reason)
            return test(*args, **kwargs)

        return wrapper

    return decorator


skip_if_no_usb_camera = _skip_unless_available(
    _usb_camera_available,
    "USBカメラが接続されていません",
)

skip_if_no_csi_camera = _skip_unless_available(
    _csi_camera_available,
    "CSIカメラが接続されていません",
)

# mDNS の能力プローブ結果（bind し直さないようモジュールレベルでキャッシュする）
_MDNS_AVAILABLE: bool | None = None

_MDNS_GROUP = "224.0.0.251"
_MDNS_PORT = 5353


def _mdns_available() -> bool:
    """5353 を共有 bind してループバックでマルチキャストに join できるか確認する.

    実機では avahi が 5353 を持っているので `SO_REUSEADDR` での共存を確かめる
    （共存できない環境ではテストが zeroconf を起動できない）。結果はキャッシュする。
    """
    global _MDNS_AVAILABLE
    if _MDNS_AVAILABLE is None:
        _MDNS_AVAILABLE = _probe_mdns()
    return _MDNS_AVAILABLE


def _probe_mdns() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("", _MDNS_PORT))
            probe.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                struct.pack(
                    "=4s4s",
                    socket.inet_aton(_MDNS_GROUP),
                    socket.inet_aton("127.0.0.1"),
                ),
            )
        return True
    except OSError:
        return False


skip_if_no_mdns = _skip_unless_available(
    _mdns_available,
    "mDNS（5353 の共有 bind / マルチキャスト join）が使えません",
)


def random_service_type() -> str:
    """テスト専用の DNS-SD サービス型（実 LAN / CI の並列ジョブと混ざらない）.

    実 zeroconf を触るテストは運用のサービス型（``_pcbasm._tcp``）を使わない。
    bind が失敗する前提のテスト（存在しない IF を指定するもの）でも、万一 bind が
    成功したときに実 LAN へ広告・探索を漏らさないための構造的な予防。
    """
    return f"_pcbasmt{token_hex(4)}._tcp.local."


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
