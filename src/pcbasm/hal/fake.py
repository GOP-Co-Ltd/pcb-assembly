"""実機の代わりに使う Klipper・カメラ・音声出力の fake.

テストと開発用のシミュレーション起動（``make dev``）が同じ実装を使う。
機構の物理は模擬せず、送信した G-code から位置と押出量を追うところまでにとどめる。
"""

from __future__ import annotations

import copy
import math
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from typing import Any, override

import attrs
import cv2
import numpy as np
from shapely import Polygon

from pcbasm.gcode import GCode, GCodeLike
from pcbasm.geometry import Point2d
from pcbasm.vision.image import Image, ImageArray

from .audio import AudioDevice, AudioPlayer, Sound
from .camera import Camera, CameraInfo, Resolution
from .klipper import GCodeJournal, KlipperError

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
# 実機と同じく、駐機の PRESENT マクロまでそろえた printer.cfg（make dev の fake 機体）
FAKE_MACHINE_PRINTER_CONFIG: dict[str, dict[str, Any]] = {
    **FAKE_PRINTER_CONFIG,
    "gcode_macro PRESENT": {"gcode": "G1 Y300"},
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


@attrs.frozen
class StepperMove:
    """FakeKlipper が受けた manual_stepper の 1 回の ``MOVE``.

    Attributes:
        stepper: manual_stepper の名前
        at: 送った時点のツールヘッドの XY（機械座標）
        distance: 進めた距離 [mm]（戻すときは負）
    """

    stepper: str
    at: Point2d
    distance: float


class FakeKlipper:
    """送信を記録し、位置と押出量を追う ``KlipperClient``.

    Moonraker には接続しない。
    実 ``XYZStage`` / ``PasteDispenser`` をそのまま組み合わせられる。
    位置は G28、G90 / G91 下の G0 / G1 で、manual_stepper の位置と押出量は ``MANUAL_STEPPER`` の ``SET_POSITION`` / ``MOVE`` で更新する。
    緊急停止は ``M112`` の送信として記録する。
    ``connection()`` で開いた接続は、この機体の状態と記録を共有する。
    ジャーナルを与えた接続は、``MoonrakerKlipper`` と同じく送信をジャーナルにも書く（緊急停止は書かない）。
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
        self._journal: GCodeJournal | None = None
        # 以下は connection() と共有するので、属性を差し替えずに中身を更新する
        self._status: dict[str, dict[str, Any]] = {
            "gcode_move": {
                "gcode_position": [0.0, 0.0, 0.0, 0.0],
                "absolute_coordinates": True,
            },
            "toolhead": {"homed_axes": ""},
            "probe": {"last_z_result": 0.0},
            "webhooks": {"state": "ready", "state_message": "Printer is ready"},
        }
        self._steppers: dict[str, float] = {}
        self._extruded: dict[str, float] = {}
        self._stepper_moves: list[StepperMove] = []
        self._sent: list[SentGCode] = []
        self._fails: list[Callable[[GCode], bool]] = []
        self._settles: list[Callable[[], None]] = []

    def connection(
        self, *, timeout: float | None = None, journal: GCodeJournal | None = None
    ) -> FakeKlipper:
        """同じ機体への別の接続を返す.

        位置・押出量・送信の記録・失敗の注入・静定待ちを共有し、送信ごとの指定が無いときの実効 timeout とジャーナルだけが違う。
        ``Devices`` が用途ごとに timeout を替えた接続（塗布の本体、jog、駐機など）を、1 台の機体として記録するのに使う。
        """
        connection = copy.copy(self)
        connection._timeout = timeout
        connection._journal = journal
        return connection

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

    @property
    def stepper_moves(self) -> tuple[StepperMove, ...]:
        """manual_stepper の ``MOVE`` を送った順に返す."""
        return tuple(self._stepper_moves)

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
        """送信が predicate に当たったら、Moonraker の 400 と同じ KlipperError を送出させる."""
        self._fails[:] = [predicate]

    def settle_with(self, wait: Callable[[], None]) -> None:
        """送信を実行し終えてから返るまでに wait を呼ぶ（実機の静定待ちの代わり）."""
        self._settles.append(wait)

    def send_gcode(
        self, gcode: GCodeLike, *, timeout: float | None = None
    ) -> dict[str, Any]:
        sent = GCode(gcode)
        timeout = self._timeout if timeout is None else timeout
        if self._journal is not None:
            self._journal.write(sent, timeout)
        self._record(sent, timeout)
        for line in str(sent).splitlines():
            self._execute(line.split())
        for wait in self._settles:
            wait()
        return {"result": "ok"}

    def get_status(self, object: str, attribute: str) -> Any:
        return self._status[object][attribute]

    def get_config(self) -> dict[str, dict[str, Any]]:
        return self._config

    def emergency_stop(self) -> None:
        self._record(GCode("M112"), self._timeout)

    def close(self) -> None:
        """何もしない（``open_klipper`` の差し替えで返せるよう口をそろえる）."""

    def _record(self, gcode: GCode, timeout: float | None) -> None:
        self._sent.append(SentGCode(gcode, timeout))
        if any(fails(gcode) for fails in self._fails):
            raise KlipperError("Bad Request")

    def _execute(self, words: list[str]) -> None:
        match words:
            case ["G90"]:
                self._status["gcode_move"]["absolute_coordinates"] = True
            case ["G91"]:
                self._status["gcode_move"]["absolute_coordinates"] = False
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
        relative = not self._status["gcode_move"]["absolute_coordinates"]
        for word in params:
            index = "XYZE".find(word[0])
            if index < 0:
                continue
            value = float(word[1:])
            position[index] = position[index] + value if relative else value
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
            x, y = self._status["gcode_move"]["gcode_position"][:2]
            self._stepper_moves.append(StepperMove(name, Point2d(x, y), moved))


class _Pacer:
    """``capture()`` を 1/fps 間隔にそろえる（無いと FrameHub の専有スレッドがビジーループになる）."""

    def __init__(self, fps: float) -> None:
        self._interval = 1.0 / fps
        self._next_deadline = time.monotonic()

    def wait(self) -> None:
        now = time.monotonic()
        if now < self._next_deadline:
            time.sleep(self._next_deadline - now)
        self._next_deadline = max(
            self._next_deadline + self._interval, time.monotonic()
        )


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
        self._pacer = _Pacer(fps) if fps is not None else None
        self._index = 0

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
        if self._pacer is not None:
            self._pacer.wait()
        frame = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        if isinstance(frame, Exception):
            raise frame
        return frame


@attrs.frozen
class Checkerboard:
    """機械座標に置いたチェッカーボード（白い縁 1 マス分の内側に squares x squares マス）.

    Attributes:
        center: 盤の中心（機械座標 [mm]）
        square: 1 マスの辺 [mm]
        squares: 1 辺のマスの数。内側コーナーは 1 辺に squares - 1 個
    """

    center: Point2d
    square: float
    squares: int = 6


# SceneCamera の濃淡（グレースケール）。銅箔と基準円は基板より明るく、塗布痕は暗い
_SCENE_BOARD = 90
_SCENE_SHAPE = 220
_SCENE_PASTE = 30
_SCENE_LIGHT = 240
_SCENE_DARK = 20
# 塗布量 [μL] から痕の径を決めるときの半球の係数（V = 2/3 π r^3）
_HEMISPHERE = 2.0 * math.pi / 3.0
# cv2 の描画に渡す座標の小数部のビット数（1/16 px 単位で描く）
_SUBPIXEL_BITS = 4


class SceneCamera(Camera):
    """FakeKlipper の位置から見える機械座標の図形と塗布痕を描く Camera の fake.

    ステージが s にあるとき、機械座標の点 p は画像中心から ``pixel_per_mm * Rot(rotation)(s - p)`` [px] の位置に写る（+x 右、+y 下）。
    この式は現行の position.py / copper.py の向きに合わせて凍結したもので、align の関数を使わずにここへ直書きする。
    描く側と読む側が同じだけ変わって golden をすり抜けるのを防ぐため。
    図形（銅箔・基準円）は shapely の多角形で受け取り、円は ``buffer`` で多角形にして渡す。
    塗布痕は FakeKlipper が記録した manual_stepper の ``MOVE`` を XY ごとに送った順に足し、正なら体積の半球の径で描く（線も始点の 1 点に描く）。
    引き戻しは、その XY に出ている分までしか減らさない（基板に出ていないペーストは吸い戻せない）。
    痕はノズルの位置から toolhead だけ戻した点、つまりカメラで見る機械座標に描く。

    FrameHub の専有スレッドから撮られている間、FakeKlipper の送信は新しい位置で描いたフレームが配られるまで返らない。
    実機では静定待ちの間に古いフレームが流れ去るのを、待ち時間ではなく描いたフレームで代える。
    """

    def __init__(
        self,
        klipper: FakeKlipper,
        *,
        pixel_per_mm: float,
        toolhead: Point2d,
        rotations_per_ul: float,
        rotation: float = 0.0,
        size: tuple[int, int] = (1280, 800),
        shapes: Sequence[Polygon] = (),
        checkerboard: Checkerboard | None = None,
        fps: float = 100.0,
    ) -> None:
        """SceneCamera を初期化し、klipper の送信に静定待ちを差し込む.

        Args:
            klipper: ステージ位置と塗布の記録を読む機体
            pixel_per_mm: 1 mm あたりの画素数
            toolhead: カメラからノズルへの相対位置 [mm]（machine の paste_dispenser.toolhead）
            rotations_per_ul: 1 μL あたりの manual_stepper の移動量 [mm]（machine の paste_dispenser.rotations_per_ul）
            rotation: 機械の軸に対するカメラの回転 [度]
            size: 画像サイズ (width, height)
            shapes: 明るく描く図形（機械座標 [mm]）
            checkerboard: 描くチェッカーボード
            fps: ``capture()`` の間隔と ``resolution.fps``
        """
        self._klipper = klipper
        self._pixel_per_mm = pixel_per_mm
        radians = math.radians(rotation)
        self._rotation = np.array(
            [
                [math.cos(radians), -math.sin(radians)],
                [math.sin(radians), math.cos(radians)],
            ]
        )
        self._size = size
        self._center = np.array([size[0] / 2, size[1] / 2])
        self._marks = [(shape, _SCENE_SHAPE) for shape in shapes]
        if checkerboard is not None:
            self._marks += _checkerboard_marks(checkerboard)
        self._toolhead = toolhead
        self._rotations_per_ul = rotations_per_ul
        self._resolution = Resolution(size[0], size[1], fps)
        self._pacer = _Pacer(fps)
        self._cond = threading.Condition()
        self._capturer: threading.Thread | None = None
        self._moves = 0  # 静定待ちに入った送信の数
        self._returned = -1  # 直前に返したフレームを描き始めたときの _moves
        self._delivered = -1  # FrameHub が配り終えたフレームの _moves
        klipper.settle_with(self._settle)

    @property
    @override
    def resolution(self) -> Resolution:
        return self._resolution

    @property
    @override
    def info(self) -> CameraInfo:
        return CameraInfo(name="SceneCamera", formats={"BGR": [self.resolution]})

    @override
    def capture(self) -> Image:
        with self._cond:
            # FrameHub は配り終えてから次を撮るので、ここに来たら直前のフレームは配られている
            self._delivered = self._returned
            self._capturer = threading.current_thread()
            self._cond.notify_all()
        self._pacer.wait()
        with self._cond:
            moves = self._moves
        x, y = self._klipper.get_status("gcode_move", "gcode_position")[:2]
        frame = self._render(np.array([x, y]))
        with self._cond:
            self._returned = moves
        return frame

    def _settle(self) -> None:
        """FrameHub が撮っている間は、送信の後に描いたフレームが配られるまで待つ."""
        with self._cond:
            self._moves += 1
            moves = self._moves
            while self._delivered < moves and self._streaming():
                self._cond.wait(0.1)

    def _streaming(self) -> bool:
        capturer = self._capturer
        return (
            capturer is not None
            and capturer is not threading.current_thread()
            and capturer.is_alive()
        )

    def _render(self, stage: ImageArray) -> Image:
        width, height = self._size
        image = np.full((height, width), _SCENE_BOARD, dtype=np.uint8)
        for shape, value in self._marks:
            cv2.fillPoly(
                image,
                [self._pixels(shape.exterior.coords, stage)],
                value,
                cv2.LINE_AA,
                _SUBPIXEL_BITS,
            )
            for ring in shape.interiors:
                cv2.fillPoly(
                    image,
                    [self._pixels(ring.coords, stage)],
                    _SCENE_BOARD,
                    cv2.LINE_AA,
                    _SUBPIXEL_BITS,
                )
        for at, volume_ul in self._deposits():
            radius = (volume_ul / _HEMISPHERE) ** (1.0 / 3.0)
            x, y = self._pixels([(at.x, at.y)], stage)[0, 0]
            cv2.circle(
                image,
                (int(x), int(y)),
                round(radius * self._pixel_per_mm * (1 << _SUBPIXEL_BITS)),
                _SCENE_PASTE,
                -1,
                cv2.LINE_AA,
                _SUBPIXEL_BITS,
            )
        return Image(cv2.cvtColor(image, cv2.COLOR_GRAY2BGR))

    def _pixels(self, points: Any, stage: ImageArray) -> ImageArray:
        """機械座標の点列を、cv2 の描画に渡す固定小数点の画素座標にする."""
        camera_mm = (stage - np.asarray(points, dtype=np.float64)) @ self._rotation.T
        pixels = self._center + self._pixel_per_mm * camera_mm
        return (
            np.round(pixels * (1 << _SUBPIXEL_BITS)).astype(np.int32).reshape(-1, 1, 2)
        )

    def _deposits(self) -> list[tuple[Point2d, float]]:
        """塗布痕を（カメラで見る機械座標, 体積 [μL]）で返す."""
        distances: defaultdict[Point2d, float] = defaultdict(float)
        for move in self._klipper.stepper_moves:
            distances[move.at] = max(0.0, distances[move.at] + move.distance)
        return [
            (at - self._toolhead, distance / self._rotations_per_ul)
            for at, distance in distances.items()
            if distance > 0
        ]


def _checkerboard_marks(board: Checkerboard) -> list[tuple[Polygon, int]]:
    """白い縁の上に黒マスを並べた多角形の列を返す."""
    half = board.square * (board.squares / 2 + 1)
    marks = [
        (
            _box(board.center.x - half, board.center.y - half, 2 * half),
            _SCENE_LIGHT,
        )
    ]
    origin_x = board.center.x - board.square * board.squares / 2
    origin_y = board.center.y - board.square * board.squares / 2
    for row in range(board.squares):
        for column in range(board.squares):
            if (row + column) % 2 == 0:
                marks.append(
                    (
                        _box(
                            origin_x + column * board.square,
                            origin_y + row * board.square,
                            board.square,
                        ),
                        _SCENE_DARK,
                    )
                )
    return marks


def _box(x: float, y: float, side: float) -> Polygon:
    return Polygon([(x, y), (x + side, y), (x + side, y + side), (x, y + side)])


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
        self._played: list[tuple[Sound, str, float]] = []
        self._close_calls = 0

    @property
    def played(self) -> tuple[tuple[Sound, str, float], ...]:
        """再生を要求された (音種, デバイス, 音量) を呼び出し順に返す."""
        return tuple(self._played)

    @property
    def close_calls(self) -> int:
        """終了処理が呼ばれた回数を返す."""
        return self._close_calls

    @override
    def list_devices(self) -> tuple[AudioDevice, ...]:
        return self._devices

    @override
    def play(self, sound: Sound, device: str, volume: float) -> Future[None]:
        self._played.append((sound, device, volume))
        future: Future[None] = Future()
        if self._playback_error is None:
            future.set_result(None)
        else:
            future.set_exception(self._playback_error)
        return future

    @override
    def close(self) -> None:
        self._close_calls += 1
