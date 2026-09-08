import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from functools import cache, wraps
from pathlib import Path
from secrets import token_hex
from typing import Any, ParamSpec, TypeVar, override

import pcbnew
import picamera2
import pytest

from pcbasm.config import Audio
from pcbasm.gcode import GCode, GCodeLike
from pcbasm.hal import Camera, CameraInfo, Klipper, Resolution
from pcbasm.hal.audio import AudioDevice, AudioPlayer, Sound
from pcbasm.hal.klipper import GCodeMacro, ReadonlyKlipper
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


def make_paste_test_board_footprint_root(root: Path) -> Path:
    """テスト塗布基板テスト用の実KiCad footprint rootを作る.

    system KiCad libraryの有無や収録数に依存させず、productionと同じ
    ``pcbnew.FootprintLoad`` 経路を通すため、実 ``FOOTPRINT`` / ``PAD`` を
    ``*.pretty/*.kicad_mod`` として保存する。
    """
    libraries = {
        "Resistor_SMD.pretty": (
            _two_pad_footprint("R_0402_1005Metric", 0.54, 0.64),
            _two_pad_footprint("R_0603_1608Metric", 0.90, 0.95),
            _two_pad_footprint("R_0805_2012Metric", 1.00, 1.40),
            _two_pad_footprint("R_1206_3216Metric", 1.15, 1.80),
        ),
        "Package_TO_SOT_SMD.pretty": (
            _uniform_pad_footprint("SOT-23", ("1", "2", "3"), 0.80, 0.90),
            _uniform_pad_footprint("SOT-23-5", ("1", "2", "3", "4", "5"), 0.60, 0.90),
            _sot223_footprint(),
        ),
        "Package_DFN_QFN.pretty": (_qfn_footprint(),),
    }
    root.mkdir(parents=True, exist_ok=True)
    for library_name, footprints in libraries.items():
        _save_footprint_library(root / library_name, footprints)
    return root


def make_paste_test_board_offset_pad_root(
    root: Path, *, shape_offset_x_mm: float, pad_size_mm: float = 1.0
) -> Path:
    """形状offset付き正方形SMD padを持つ独立KiCad footprint rootを作る."""
    footprint = _footprint("OffsetPad")
    _add_smd_pad(
        footprint,
        number="1",
        width_mm=pad_size_mm,
        height_mm=pad_size_mm,
        x_mm=0.0,
        shape_offset_x_mm=shape_offset_x_mm,
    )
    root.mkdir(parents=True, exist_ok=True)
    _save_footprint_library(root / "Test.pretty", (footprint,))
    return root


def _two_pad_footprint(
    name: str, width_mm: float, height_mm: float
) -> pcbnew.FOOTPRINT:
    return _uniform_pad_footprint(name, ("1", "2"), width_mm, height_mm)


def _uniform_pad_footprint(
    name: str,
    pad_numbers: Sequence[str],
    width_mm: float,
    height_mm: float,
) -> pcbnew.FOOTPRINT:
    footprint = _footprint(name)
    for index, number in enumerate(pad_numbers):
        _add_smd_pad(
            footprint,
            number=number,
            width_mm=width_mm,
            height_mm=height_mm,
            x_mm=float(index) * (width_mm + 0.5),
        )
    return footprint


def _qfn_footprint() -> pcbnew.FOOTPRINT:
    """Paste aperture、lead、exposed padの3群を持つQFN相当fixture."""
    footprint = _footprint("QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm")
    paste_only_layers = pcbnew.LSET()
    paste_only_layers.AddLayer(pcbnew.F_Paste)
    copper_mask_layers = pcbnew.LSET()
    copper_mask_layers.AddLayer(pcbnew.F_Cu)
    copper_mask_layers.AddLayer(pcbnew.F_Mask)
    for index in range(4):
        _add_smd_pad(
            footprint,
            number="",
            width_mm=0.50,
            height_mm=0.50,
            x_mm=float(index),
            layer_set=paste_only_layers,
        )
    for index in range(16):
        _add_smd_pad(
            footprint,
            number=str(index + 1),
            width_mm=0.25,
            height_mm=0.80,
            x_mm=float(index),
        )
    _add_smd_pad(
        footprint,
        number="17",
        width_mm=1.75,
        height_mm=1.75,
        x_mm=20.0,
        layer_set=copper_mask_layers,
    )
    return footprint


def _sot223_footprint() -> pcbnew.FOOTPRINT:
    """3本のleadと異寸法tabの2群を持つSOT-223相当fixture."""
    footprint = _footprint("SOT-223-3_TabPin2")
    for index, number in enumerate(("1", "2", "3")):
        _add_smd_pad(
            footprint,
            number=number,
            width_mm=0.70,
            height_mm=1.50,
            x_mm=float(index),
        )
    _add_smd_pad(
        footprint,
        number="2",
        width_mm=3.00,
        height_mm=2.00,
        x_mm=4.0,
    )
    return footprint


def _footprint(name: str) -> pcbnew.FOOTPRINT:
    footprint = pcbnew.FOOTPRINT(None)
    footprint.SetFPID(pcbnew.LIB_ID("", name))
    footprint.SetReference("REF**")
    footprint.SetValue(name)
    return footprint


def _add_smd_pad(
    footprint: pcbnew.FOOTPRINT,
    *,
    number: str,
    width_mm: float,
    height_mm: float,
    x_mm: float,
    layer_set: pcbnew.LSET | None = None,
    shape_offset_x_mm: float = 0.0,
) -> None:
    pad = pcbnew.PAD(footprint)
    pad.SetNumber(number)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetShape(pcbnew.PAD_SHAPE_RECTANGLE)
    pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(width_mm), pcbnew.FromMM(height_mm)))
    pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), 0))
    pad.SetOffset(pcbnew.VECTOR2I(pcbnew.FromMM(shape_offset_x_mm), 0))
    pad.SetLayerSet(pad.SMDMask() if layer_set is None else layer_set)
    footprint.Add(pad)


def _save_footprint_library(path: Path, footprints: Sequence[pcbnew.FOOTPRINT]) -> None:
    plugin = pcbnew.PCB_IO_KICAD_SEXPR()
    plugin.FootprintLibCreate(str(path))
    first, *remaining = footprints
    # pcbnew.FootprintSaveは空libraryのformatを判別できないため、最初の1個だけ
    # concrete pluginでbootstrapする。以後は公開helperを通して実ファイルへ保存する。
    plugin.FootprintSave(str(path), first)
    for footprint in remaining:
        pcbnew.FootprintSave(str(path), footprint)


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


def _alsa_audio_available() -> bool:
    """aplayが利用でき、ALSAに1台以上の再生デバイスが見えているか確認する."""
    try:
        result = subprocess.run(
            ["aplay", "-l"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        return result.returncode == 0 and "card " in result.stdout.lower()
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def _skip_unless_available(
    is_available: Callable[[], bool],
    reason: str,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """実行時に能力（カメラ / ALSA 接続・mDNS）を確認してテストをskipするdecoratorを返す."""

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

skip_if_no_alsa_audio = _skip_unless_available(
    _alsa_audio_available,
    "ALSA再生デバイスが接続されていません",
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


# FakeAudioPlayer が既定で返すデバイス一覧（実 ALSA 構成に依存しないための固定値）
FAKE_AUDIO_DEVICES = (
    AudioDevice("default", "システム既定"),
    AudioDevice(
        "plughw:CARD=sndrpihifiberry,DEV=0",
        "snd_rpi_hifiberry_dac, HifiBerry DAC HiFi pcm5102a-hifi-0",
    ),
    AudioDevice("plughw:CARD=vc4hdmi0,DEV=0", "vc4-hdmi-0, MAI PCM i2s-hifi-0"),
)


class FakeAudioPlayer(AudioPlayer):
    """AudioPlayer 利用側を実ALSAなしで結合検証するテスト用実装."""

    def __init__(
        self,
        devices: Sequence[AudioDevice] | None = None,
        playback_error: Exception | None = None,
    ) -> None:
        self._devices = FAKE_AUDIO_DEVICES if devices is None else tuple(devices)
        self._playback_error = playback_error
        self._played: list[tuple[Sound, Audio]] = []
        self._close_calls = 0

    @property
    def played(self) -> tuple[tuple[Sound, Audio], ...]:
        """再生を要求された (音種, 設定) を呼び出し順に返す."""
        return tuple(self._played)

    @property
    def close_calls(self) -> int:
        """終了処理が呼ばれた回数を返す."""
        return self._close_calls

    @override
    def list_devices(self) -> tuple[AudioDevice, ...]:
        return self._devices

    @override
    def play(self, sound: Sound, config: Audio) -> Future[None]:
        self._played.append((sound, config))
        future: Future[None] = Future()
        if self._playback_error is None:
            future.set_result(None)
        else:
            future.set_exception(self._playback_error)
        return future

    @override
    def close(self) -> None:
        self._close_calls += 1


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


# FakeKlipper が返す缶詰 printer config。実 XYZStage / PasteDispenser（ManualStepper +
# AirPump）/ ProbeExecutor が要求するセクションだけを持つ。
FAKE_PRINTER_CONFIG: dict[str, dict[str, Any]] = {
    "printer": {"max_velocity": "100"},
    "stepper_x": {"position_min": "0", "position_max": "300"},
    "stepper_y": {"position_min": "0", "position_max": "300"},
    "stepper_z": {"position_min": "-5", "position_max": "50"},
    "manual_stepper paste_dispenser": {"rotation_distance": "1.0"},
    "output_pin air_pump": {},
    "load_cell_probe": {},
}
FAKE_KLIPPER_STATUS: dict[tuple[str, str], Any] = {
    ("gcode_move", "gcode_position"): [0.0, 0.0, 5.0, 0.0],
    ("probe", "last_z_result"): 0.0,
}


_G1_RE = re.compile(r"G1 (.*)")


class FakeKlipper(Klipper):
    """送信 G-code を記録し、缶詰 config / status を返す自前 HAL の fake.

    Moonraker には接続しない（``__init__`` を上書きしてソケットを開かない）。
    ``ReadonlyKlipper`` は束縛メソッドを保持するだけなので、実 ``XYZStage`` /
    ``PasteDispenser`` / ``ProbeExecutor`` をそのまま組み合わせて結合検証できる。
    """

    def __init__(
        self,
        *,
        config: Mapping[str, Mapping[str, Any]] = FAKE_PRINTER_CONFIG,
        status: Mapping[tuple[str, str], Any] = FAKE_KLIPPER_STATUS,
    ) -> None:
        self._config = {section: dict(values) for section, values in config.items()}
        self._status = dict(status)
        self._sent: list[GCode] = []
        self._readonly = ReadonlyKlipper(self)

    @property
    def sent(self) -> tuple[GCode, ...]:
        """``send_gcode`` に渡された G-code を呼び出し順に返す."""
        return tuple(self._sent)

    @property
    def sent_lines(self) -> list[str]:
        """送信した全 G-code を行に展開して返す."""
        return [line for gc in self._sent for line in str(gc).splitlines()]

    def clear_sent(self) -> None:
        self._sent.clear()

    def g1_moves(self) -> list[dict[str, float]]:
        """送信した ``G1`` の座標を {軸: 値} の列で返す（``f`` は feed）."""
        moves: list[dict[str, float]] = []
        for line in self.sent_lines:
            match = _G1_RE.match(line)
            if match:
                moves.append(
                    {
                        part[0].lower(): float(part[1:])
                        for part in match.group(1).split()
                    }
                )
        return moves

    def set_status(self, object: str, attribute: str, value: Any) -> None:
        self._status[(object, attribute)] = value

    @override
    def send_gcode(
        self, gcode: GCodeLike, *, timeout: float | None = None
    ) -> dict[str, Any]:
        self._sent.append(GCode(gcode))
        return {"result": "ok"}

    @override
    def get_status(self, object: str, attribute: str) -> Any:
        return self._status[(object, attribute)]

    @override
    @cache
    def get_config(self) -> dict[str, dict[str, Any]]:
        return self._config

    @override
    @cache
    def get_macros(self) -> dict[str, GCodeMacro]:
        return {}

    @override
    def __del__(self) -> None:
        pass
