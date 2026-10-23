"""実機の代わりに使う Klipper・カメラ・音声出力の fake.

テストと開発用の起動（``make api-fake``）が同じ実装を使う。
機構の物理は模擬せず、送信した G-code から位置と押出量を追うところまでにとどめる。
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from typing import Any, override

import attrs

from pcbasm.config import Audio
from pcbasm.gcode import GCode, GCodeLike
from pcbasm.vision import Image

from .audio import AudioDevice, AudioPlayer, Sound
from .camera import Camera, CameraInfo, Resolution
from .klipper import Klipper, ReadonlyKlipper

# FakeKlipper が既定で返す printer.cfg。XYZStage / PasteDispenser / ProbeExecutor が読むセクションだけを持つ
FAKE_PRINTER_CONFIG: dict[str, dict[str, Any]] = {
    "printer": {"max_velocity": "100"},
    "stepper_x": {"position_min": "0", "position_max": "300"},
    "stepper_y": {"position_min": "0", "position_max": "300"},
    "stepper_z": {"position_min": "-5", "position_max": "50"},
    "manual_stepper paste_dispenser": {"rotation_distance": "1.0"},
    "output_pin air_pump": {},
    "load_cell_probe": {},
}

_AXES = "xyz"


@attrs.frozen
class SentGCode:
    """FakeKlipper が受けた 1 回の送信.

    Attributes:
        gcode: 送信した G-code
        timeout: 実効 timeout（送信ごとの指定が無ければコンストラクタの値）
    """

    gcode: GCode
    timeout: float | None


class FakeKlipper(Klipper):
    """送信を記録し、位置と押出量を追う Klipper の fake.

    Moonraker には接続しない。
    ``readonly`` も本物と同じ形で持つので、実 ``XYZStage`` / ``PasteDispenser`` をそのまま組み合わせられる。
    位置は G28、G90 / G91 下の G0 / G1 で、manual_stepper の位置と押出量は ``MANUAL_STEPPER`` の ``SET_POSITION`` / ``MOVE`` で更新する。
    緊急停止は ``M112`` の送信として記録する。
    """

    def __init__(
        self,
        config: Mapping[str, Mapping[str, Any]] = FAKE_PRINTER_CONFIG,
        *,
        timeout: float | None = None,
    ) -> None:
        """FakeKlipper を初期化する.

        Args:
            config: printer.cfg の内容（セクション名 → キーと値）
            timeout: 送信ごとの指定が無いときの実効 timeout
        """
        self._config = {section: dict(values) for section, values in config.items()}
        self._timeout = timeout
        self._status: dict[str, dict[str, Any]] = {
            "gcode_move": {"gcode_position": [0.0, 0.0, 0.0, 0.0]},
            "toolhead": {"homed_axes": ""},
            "probe": {"last_z_result": 0.0},
        }
        self._relative = False
        self._steppers: dict[str, float] = {}
        self._extruded: dict[str, float] = {}
        self._sent: list[SentGCode] = []
        self._fails: Callable[[GCode], bool] | None = None
        self._readonly = ReadonlyKlipper(self)

    @property
    def sent(self) -> tuple[SentGCode, ...]:
        """送信を呼び出し順に返す."""
        return tuple(self._sent)

    @property
    def sent_lines(self) -> list[str]:
        """送信した全 G-code を行に展開して返す."""
        return [line for sent in self._sent for line in str(sent.gcode).splitlines()]

    @property
    def extruded(self) -> dict[str, float]:
        """manual_stepper ごとの押出量 [mm]（``MOVE`` で進めた距離の合計）を返す."""
        return dict(self._extruded)

    def clear_sent(self) -> None:
        """送信の記録を消す（位置と押出量は保つ）."""
        self._sent.clear()

    def g1_moves(self) -> list[dict[str, float]]:
        """送信した ``G1`` の座標を {軸: 値} の列で返す（``f`` は feed）."""
        return [
            {word[0].lower(): float(word[1:]) for word in line.split()[1:]}
            for line in self.sent_lines
            if line.startswith("G1 ")
        ]

    def set_status(self, object: str, attribute: str, value: Any) -> None:
        """``get_status`` が返す値を置く（初期位置もここで与える）."""
        self._status.setdefault(object, {})[attribute] = value

    def fail_on(self, predicate: Callable[[GCode], bool]) -> None:
        """送信が predicate に当たったら、Moonraker の 400 と同じ RuntimeError を送出させる."""
        self._fails = predicate

    @override
    def send_gcode(
        self, gcode: GCodeLike, *, timeout: float | None = None
    ) -> dict[str, Any]:
        sent = GCode(gcode)
        self._record(sent, timeout)
        for line in str(sent).splitlines():
            self._execute(line.split())
        return {"result": "ok"}

    @override
    def get_status(self, object: str, attribute: str) -> Any:
        return self._status[object][attribute]

    @override
    @functools.cache
    def get_config(self) -> dict[str, dict[str, Any]]:
        return self._config

    @override
    def emergency_stop(self) -> None:
        self._record(GCode("M112"), None)

    def _record(self, gcode: GCode, timeout: float | None) -> None:
        self._sent.append(
            SentGCode(gcode, self._timeout if timeout is None else timeout)
        )
        if self._fails is not None and self._fails(gcode):
            raise RuntimeError("Bad Request")

    def _execute(self, words: list[str]) -> None:
        match words:
            case ["G90"]:
                self._relative = False
            case ["G91"]:
                self._relative = True
            case ["G28", *axes]:
                self._home([axis.lower() for axis in axes] or list(_AXES))
            case ["G0" | "G1", *params]:
                self._move(params)
            case ["MANUAL_STEPPER", *params]:
                self._move_stepper(dict(param.split("=", 1) for param in params))

    def _home(self, axes: list[str]) -> None:
        position = list(self._status["gcode_move"]["gcode_position"])
        for axis in axes:
            stepper = self._config.get(f"stepper_{axis}", {})
            position[_AXES.index(axis)] = float(stepper.get("position_endstop", 0.0))
        homed = set(self._status["toolhead"]["homed_axes"]) | set(axes)
        self._status["gcode_move"]["gcode_position"] = position
        self._status["toolhead"]["homed_axes"] = "".join(
            axis for axis in _AXES if axis in homed
        )

    def _move(self, params: list[str]) -> None:
        position = list(self._status["gcode_move"]["gcode_position"])
        for word in params:
            index = "XYZE".find(word[0])
            if index < 0:
                continue
            value = float(word[1:])
            position[index] = position[index] + value if self._relative else value
        self._status["gcode_move"]["gcode_position"] = position

    def _move_stepper(self, params: dict[str, str]) -> None:
        name = params["STEPPER"]
        if "SET_POSITION" in params:
            self._steppers[name] = float(params["SET_POSITION"])
        if "MOVE" in params:
            target = float(params["MOVE"])
            moved = target - self._steppers.get(name, 0.0)
            self._extruded[name] = self._extruded.get(name, 0.0) + moved
            self._steppers[name] = target


class FakeCamera(Camera):
    """与えた画像列を順に返す Camera の fake.

    列を使い切った後は最後の要素を返し続ける。
    列に例外を置くと、その ``capture()`` で送出する。
    ``resolution`` は列の最初の画像から決めるので、列には画像が 1 つ以上必要。
    fps を与えたときだけ ``capture()`` を 1/fps 間隔に合わせて待たせ、与えないときの ``resolution.fps`` は 0（上限なし）にする。
    """

    def __init__(
        self, frames: Sequence[Image | Exception], *, fps: float | None = None
    ) -> None:
        first = next((frame for frame in frames if isinstance(frame, Image)), None)
        if first is None:
            raise ValueError("frames には Image が 1 つ以上必要です")
        self._frames = list(frames)
        self._resolution = Resolution(first.width, first.height, fps or 0.0)
        self._fps = fps
        self._index = 0
        self._next_deadline = time.monotonic()

    @property
    @override
    def resolution(self) -> Resolution:
        return self._resolution

    @property
    @override
    def info(self) -> CameraInfo:
        return CameraInfo(name="FakeCamera", formats={"BGR": [self.resolution]})

    @override
    def capture(self) -> Image:
        if self._fps is not None:
            # ペーシングが無いと FrameHub の専有スレッドがビジーループになる
            now = time.monotonic()
            if now < self._next_deadline:
                time.sleep(self._next_deadline - now)
            self._next_deadline = max(
                self._next_deadline + 1.0 / self._fps, time.monotonic()
            )
        frame = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        if isinstance(frame, Exception):
            raise frame
        return frame


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
    """再生要求を記録し、ALSA に触れない AudioPlayer の fake."""

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
