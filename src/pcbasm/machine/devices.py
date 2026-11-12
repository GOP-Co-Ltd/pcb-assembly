"""機体 1 台の装置（Klipper か新ファームの XYZ ステージ、カメラ、通知音、ピンセットのサーボ）.

装置を組み立てる場所はここだけで、fake への切替もここで行う。

XYZ は machine.toml に ``[pnp.motion]`` があれば新ファームのステージ（:class:`~pcbasm.hal.motion.MotionStage`）で、
無ければ ``[klipper]`` の Klipper で動かす。
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from shapely import Polygon

from pcbasm import hal
from pcbasm.config import ConfigError, load_klipper, load_machine, load_motion
from pcbasm.config.klipper import KlipperConfig
from pcbasm.config.pnp import (
    LoadCellConfig,
    MotionAxisConfig,
    MotionConfig,
    ServoConfig,
)
from pcbasm.errors import DeviceError
from pcbasm.geometry import Point2d
from pcbasm.hal import (
    AlsaAudioPlayer,
    AudioPlayer,
    Camera,
    ContactSettings,
    FrameHub,
    Gamepad,
    KlipperClient,
    Limits,
    MoonrakerKlipper,
    MotionStage,
    ScalarLimits,
    ServoBus,
    ServoMode,
    Tweezer,
    Velocity,
    mcu,
)
from pcbasm.hal.fake import (
    FAKE_MACHINE_PRINTER_CONFIG,
    FakeCamera,
    FakeGamepad,
    FakeKlipper,
    FakeMotionStage,
    FakeServoBus,
    SceneCamera,
)
from pcbasm.hal.mcu import (
    AxisSettings,
    LoadCellSettings,
    McuMotionSettings,
    TmcSettings,
)
from pcbasm.machine.journal import Journal
from pcbasm.vision import CameraModel, Image

EMERGENCY_STOP_TIMEOUT = 10.0

# [klipper] が無く、XYZ を新ファームで動かす機体で Klipper を求められたときの文言
NO_KLIPPER = (
    "この機体は Klipper を使いません（XYZ は [pnp.motion] の新ファームで動かします）"
)


class Devices:
    """機体 1 台の装置.

    作っただけでは外部装置へ接続しない。
    Moonraker へは最初の送信か問い合わせで、カメラへは最初に :meth:`frame_hub` を呼んだときに、
    サーボへは最初に :meth:`servo_bus` を呼んだときに、新ファームのステージへは最初に :meth:`motion_stage` を
    呼んだときに接続する。
    Moonraker が起動していなくても作れ、起動した後は作り直さずにそのまま使える。
    """

    def __init__(
        self,
        config_path: Path,
        journal_dir: Path,
        *,
        klipper: FakeKlipper | None = None,
        camera: Callable[[], Camera] | None = None,
        audio: AudioPlayer | None = None,
        servo_bus: FakeServoBus | None = None,
        gamepad: FakeGamepad | None = None,
        motion: FakeMotionStage | None = None,
    ) -> None:
        """Devices を初期化する.

        Args:
            config_path: machine.toml（[klipper] は接続先を渡されないときに、[camera] はカメラを開くときに読む）
            journal_dir: G-code ジャーナルの置き場所
            klipper: Moonraker の代わりに使う fake 機体（None は [klipper] の Moonraker）
            camera: カメラを開く関数（None は [camera] のカメラを開く）
            audio: 通知音のプレイヤー（None は ALSA で鳴らす）
            servo_bus: 実機のサーボの代わりに使う fake のバス（None は [pnp.servo] のポートを開く）
            gamepad: 実機のコントローラの代わりに使う fake（None はつながっているコントローラを探して開く）
            motion: 新ファームのステージの代わりに使う fake（None は [pnp.motion] の MCU を開く）
        """
        self._config_path = config_path
        self._journal_dir = journal_dir
        self._fake_klipper = klipper
        self._open_camera = camera
        self._klipper_lock = threading.Lock()
        self._moonraker: MoonrakerKlipper | None = None
        self._endpoint: KlipperConfig | None = None
        self._camera_lock = threading.Lock()
        self._frame_hub: FrameHub | None = None
        self._audio = audio if audio is not None else AlsaAudioPlayer()
        self._fake_servo_bus = servo_bus
        self._servo_lock = threading.Lock()
        self._servo_bus: ServoBus | None = None
        self._servo_port: tuple[str, int] | None = None
        self._fake_gamepad = gamepad
        self._fake_motion = motion
        self._motion_lock = threading.Lock()
        self._motion_stage: MotionStage | None = None
        self._motion_settings: McuMotionSettings | None = None

    @property
    def audio(self) -> AudioPlayer:
        """通知音のプレイヤー."""
        return self._audio

    def klipper(
        self,
        timeout: float | None = None,
        *,
        journal: Journal | None = None,
        endpoint: KlipperConfig | None = None,
    ) -> KlipperClient:
        """Klipper への接続を返す.

        HTTP クライアントは 1 つを使い回し、呼び出しごとに替えるのは既定の timeout とジャーナルだけにする。
        接続先が前と変わっていれば HTTP クライアントを作り直す。
        printer.cfg は接続ごとに最初の 1 回だけ読むので、ジョブは 1 つの接続を使い回せば同じ版で走る。

        Args:
            timeout: 送信ごとの指定が無いときと、問い合わせの timeout 秒数（None は無制限。塗布の本体はこれ）
            journal: 送信を書くジャーナル（None は当日の REST のジャーナル）
            endpoint: 接続先（None はそのときの machine.toml の [klipper]。ジョブは開始時に読んだ設定を渡す）

        Raises:
            ConfigError: endpoint が None で [klipper] が読めない場合
            DeviceError: endpoint が None で、[klipper] が無く [pnp.motion] で XYZ を動かす機体の場合
        """
        if journal is None:
            journal = Journal.for_today(self._journal_dir)
        return self._klipper(endpoint).connection(timeout=timeout, journal=journal)

    def emergency_stop(self) -> None:
        """緊急停止を送る（使い回す HTTP クライアントとは別に送り、実行中の長い送信の後ろで待たない）.

        新ファームの機体（[pnp.motion]）では MCU へ送る。開いているステージがあればそれで送り
        （原点出しや移動の待ちの後ろで待たない）、無ければ緊急停止だけのために開いて送る（設定も reset もしない）。
        ステージを閉じている最中（:meth:`restart_motion_stage` で設定が変わったときと :meth:`close`）は、
        実行中の操作の終わりを待つ間も、閉じ終えるまで古いステージで送る。
        ステージを開いている最中（初回の :meth:`motion_stage` と、設定が変わった :meth:`restart_motion_stage` で
        古いステージを閉じ終えた後）は、ポートを開けずに ``DeviceError`` になる（その間 MCU は流していないか、
        速度指令の期限で止まる）。
        どちらの機体かは :meth:`uses_motion_stage` と同じく決める（[pnp.motion] が読めなくても [klipper] が
        読めれば Klipper へ送る）。

        Raises:
            ConfigError: どちらの機体かを決められないか、[klipper] が読めない場合
            KlipperError: Klipper が要求を断った場合
            DeviceError: MCU へ送れない場合（``MotionError``・``McuError``）
        """
        stage = self.opened_motion_stage()
        if stage is not None:
            stage.emergency_stop()
            return
        if self._fake_klipper is not None:
            self._fake_klipper.connection(
                timeout=EMERGENCY_STOP_TIMEOUT
            ).emergency_stop()
            return
        motion = self._motion_config()
        if motion is not None:
            mcu.send_emergency_stop(motion.serial)
            return
        endpoint = load_klipper(self._config_path)
        klipper = hal.open_klipper(
            endpoint.host, endpoint.port, timeout=EMERGENCY_STOP_TIMEOUT
        )
        try:
            klipper.emergency_stop()
        finally:
            klipper.close()

    def uses_motion_stage(self) -> bool:
        """XYZ を新ファームのステージで動かす機体か（fake を渡されていなければ、呼ぶたびに [pnp.motion] を読む）.

        [pnp.motion] が読めなくても [klipper] が読めれば、Klipper の機体とする（移行の途中で [pnp.motion] を
        書き損じても、緊急停止と操作パネルを Klipper へ届けるため）。

        Raises:
            ConfigError: machine.toml が TOML として読めないか、[pnp.motion] が不正で [klipper] も読めない場合
        """
        return self._fake_motion is not None or self._motion_config() is not None

    def motion_limits(self) -> Limits:
        """新ファームのステージの可動域と最高速度（[pnp.motion] から読み、MCU は開かない）.

        Raises:
            DeviceError: [pnp.motion] が無い場合
            ConfigError: [pnp.motion] が読めない場合
        """
        if self._fake_motion is not None:
            return self._fake_motion.limits
        return _motion_limits(self._required_motion())

    def motion_stage(self) -> MotionStage:
        """新ファームのステージを返す（初回に [pnp.motion] の MCU を開いて設定する）.

        machine.toml を書き換えても開き直さない。読み直すのは :meth:`restart_motion_stage`。

        Raises:
            DeviceError: [pnp.motion] が無い場合（``MotionError`` は MCU を開けない・設定できない場合）
            ConfigError: [pnp.motion] が読めないか、ピンの書き方が不正な場合
        """
        if self._fake_motion is not None:
            return self._fake_motion
        with self._motion_lock:
            stage = self._motion_stage
            if stage is None:
                stage = self._open_motion(self._read_motion_settings())
            return stage

    def opened_motion_stage(self) -> MotionStage | None:
        """開いている新ファームのステージ（開いていなければ None。ここでは接続しない）.

        状態の表示やジョブ終わりの停止のように、そのために MCU を開きたくない呼び出し側が使う。
        """
        if self._fake_motion is not None:
            return self._fake_motion
        return self._motion_stage

    def restart_motion_stage(self) -> MotionStage:
        """ファームウェア再起動。[pnp.motion] を読み直してステージを立て直す。原点は「未」になる.

        設定が開いたときと同じなら、開いているステージを restart する（MCU を reset して設定し直す）。
        変わっていれば閉じて新しい設定で開き直し、開いていなければ開く。開くときは、MCU が shutdown しているか
        MCU の設定が違えば reset する。

        Raises:
            DeviceError: [pnp.motion] が無い場合（``MotionError`` は MCU と話せない場合）
            ConfigError: [pnp.motion] が読めないか、ピンの書き方が不正な場合
        """
        if self._fake_motion is not None:
            self._fake_motion.restart()
            return self._fake_motion
        settings = self._read_motion_settings()
        with self._motion_lock:
            stage = self._motion_stage
            if stage is not None and settings == self._motion_settings:
                stage.restart()
                return stage
            if stage is not None:
                # 閉じ終えるまで参照を外さない（実行中の操作の終わりを待つ間も、緊急停止は古い接続から送る）
                stage.close()
                self._motion_stage = None
                self._motion_settings = None
            return self._open_motion(settings)

    def frame_hub(self) -> FrameHub:
        """FrameHub を返す（初回の呼び出しでカメラを開く。start はしない）.

        Raises:
            OSError: カメラデバイスが見つからない・開けない場合
            RuntimeError: カメラがフォーマット等をサポートしない場合
        """
        with self._camera_lock:
            if self._frame_hub is None:
                self._frame_hub = FrameHub(self._camera())
            return self._frame_hub

    def rebuild_camera(self) -> None:
        """FrameHub を止めて捨てる（次の :meth:`frame_hub` で [camera] を読み直して開く）."""
        with self._camera_lock:
            hub = self._frame_hub
            self._frame_hub = None
        if hub is not None:
            hub.stop()

    def connect_gamepad(self) -> Gamepad:
        """コントローラを開く（呼ぶたびに開き直す。閉じるのは呼んだ側）.

        Raises:
            GamepadError: コントローラが見つからない・開けない場合
        """
        if self._fake_gamepad is not None:
            return self._fake_gamepad
        return hal.open_gamepad()

    def servo_bus(self) -> ServoBus:
        """ピンセットのサーボのバスを返す（初回に [pnp.servo] のポートを開く）.

        port か baudrate が前に開いたときと変わっていれば、古いバスを閉じて開き直す。

        Raises:
            ConfigError: machine.toml が読めないか、[pnp] / [pnp.servo] が無い場合
            ServoError: ポートを開けない場合
        """
        servo = self._servo_config()
        with self._servo_lock:
            if self._fake_servo_bus is not None:
                return self._fake_servo_bus
            port = (servo.port, servo.baudrate)
            if self._servo_bus is None or port != self._servo_port:
                if self._servo_bus is not None:
                    self._servo_bus.close()
                    self._servo_bus = None
                self._servo_bus = hal.open_servo_bus(servo.port, servo.baudrate)
                self._servo_port = port
            return self._servo_bus

    def tweezer(self) -> Tweezer:
        """ピンセットを返す（呼ぶたびに machine.toml の [pnp.servo] を読む）.

        Raises:
            ConfigError: machine.toml が読めない、[pnp.servo] が無い、または役割と開閉の角度が未設定の場合
            ServoError: サーボが応答しないか、モードが役割と合わない場合
        """
        servo = self._servo_config()
        match (
            servo.rotation_id,
            servo.open_close_id,
            servo.closed_angle,
            servo.open_angle,
        ):
            case (
                int() as rotation,
                int() as open_close,
                float()
                | int() as closed,
                float()
                | int() as opened,
            ):
                closed_position = hal.degrees_to_position(closed)
                open_position = hal.degrees_to_position(opened)
                if closed_position == open_position:
                    raise ConfigError(
                        f"machine.toml の [pnp.servo] の closed_angle（{closed:g}°）と open_angle（{opened:g}°）が"
                        "サーボの同じ位置になり、開閉できません（サーボ設定ジョブをやり直す）",
                        frozenset({"pnp"}),
                    )
                return Tweezer(
                    self.servo_bus(),
                    rotation_id=rotation,
                    open_close_id=open_close,
                    closed_position=closed_position,
                    open_position=open_position,
                    rotation_speed=servo.rotation_speed,
                )
            case values:
                names = (
                    "rotation_id",
                    "open_close_id",
                    "closed_angle",
                    "open_angle",
                )
                missing = [name for name, value in zip(names, values) if value is None]
                raise ConfigError(
                    f"machine.toml の [pnp.servo] に {', '.join(missing)} がありません"
                    "（サーボの役割を判別して書く）",
                    frozenset({"pnp"}),
                )

    def close(self) -> None:
        """後始末（通知音を閉じ、FrameHub を止め、Moonraker への HTTP
        クライアント、サーボのバス、新ファームのステージを閉じる）.

        実行中のジョブの完了音を捨てないよう、ジョブを止めてから呼ぶ。
        """
        self._audio.close()
        self.rebuild_camera()
        with self._klipper_lock:
            moonraker = self._moonraker
            self._moonraker = None
        if moonraker is not None:
            moonraker.close()
        with self._servo_lock:
            servo_bus = self._fake_servo_bus or self._servo_bus
            self._servo_bus = None
            self._servo_port = None
        if servo_bus is not None:
            servo_bus.close()
        with self._motion_lock:
            motion = self._fake_motion or self._motion_stage
            if motion is not None:
                # 閉じ終えるまで参照を外さない（restart_motion_stage と同じ）
                motion.close()
            self._motion_stage = None
            self._motion_settings = None

    def _klipper(
        self, endpoint: KlipperConfig | None
    ) -> MoonrakerKlipper | FakeKlipper:
        if self._fake_klipper is not None:
            return self._fake_klipper
        if endpoint is None:
            endpoint = self._klipper_endpoint()
        with self._klipper_lock:
            if self._moonraker is None or endpoint != self._endpoint:
                # 古いクライアントは閉じない（実行中のジョブの接続がまだ使っているかもしれない）
                self._moonraker = hal.open_klipper(endpoint.host, endpoint.port)
                self._endpoint = endpoint
            return self._moonraker

    def _klipper_endpoint(self) -> KlipperConfig:
        try:
            return load_klipper(self._config_path)
        except ConfigError:
            if load_motion(self._config_path) is not None:
                raise DeviceError(NO_KLIPPER) from None
            raise

    def _motion_config(self) -> MotionConfig | None:
        """[pnp.motion]（無いか、読めず [klipper] が読める機体は None）."""
        try:
            return load_motion(self._config_path)
        except ConfigError:
            if self._klipper_readable():
                return None
            raise

    def _klipper_readable(self) -> bool:
        try:
            load_klipper(self._config_path)
        except ConfigError:
            return False
        return True

    def _required_motion(self) -> MotionConfig:
        motion = load_motion(self._config_path)
        if motion is None:
            raise DeviceError(
                "この機体は新ファームのステージを使いません（machine.toml に [pnp.motion] がありません）"
            )
        return motion

    def _read_motion_settings(self) -> McuMotionSettings:
        return motion_settings(self._required_motion())

    def _open_motion(self, settings: McuMotionSettings) -> MotionStage:
        """MCU を開いて覚える（``_motion_lock`` を持って呼ぶ）."""
        stage = mcu.open_motion_stage(settings)
        self._motion_stage = stage
        self._motion_settings = settings
        return stage

    def _servo_config(self) -> ServoConfig:
        pnp = load_machine(self._config_path).pnp
        if pnp is None or pnp.servo is None:
            raise ConfigError(
                "machine.toml に [pnp.servo] がありません", frozenset({"pnp"})
            )
        return pnp.servo

    def _camera(self) -> Camera:
        if self._open_camera is not None:
            return self._open_camera()
        camera = load_machine(self._config_path).camera
        return hal.create_camera(
            device_id=camera.device_id,
            width=camera.width,
            height=camera.height,
            fps=camera.fps,
            format=camera.format,
            backend=camera.backend,
        )


def open_devices(
    config_path: Path,
    journal_dir: Path,
    *,
    fake: bool,
    scene: Callable[[], Sequence[Polygon]] = tuple,
    camera_image: Path | None = None,
    audio: AudioPlayer | None = None,
) -> Devices:
    """machine.toml の機体の装置を組む（ここでは外部装置へ接続しない）.

    Args:
        config_path: machine.toml
        journal_dir: G-code ジャーナルの置き場所
        fake: Klipper を実機と同じ printer.cfg の FakeKlipper にし、カメラをその位置から scene を描く SceneCamera にする。
            サーボは ID 1 がステップモード、ID 2 が位置モードの FakeServoBus にし、サーボ設定ジョブを済ませた
            角度制限（ID 1 は制限なし、ID 2 は [pnp.servo] の閉じ切り〜開き切り）を持たせる。
            コントローラは何も押していない FakeGamepad にする。
            [pnp.motion] があれば、その可動域と最高速度の FakeMotionStage（原点は「未」。
            [pnp.motion.load_cell] があればロードセルも）を持たせる
            （ここで [pnp.motion] が無いか読めなければ持たせない）
        scene: fake のカメラに映す図形（機械座標 [mm]）を返す関数（カメラを開くたびに呼ぶ）
        camera_image: 与えると、カメラをこの画像を返し続ける FakeCamera にする（fake より優先する）
        audio: 通知音のプレイヤー（None は ALSA で鳴らす。テストは FakeAudioPlayer を渡す）
    """
    klipper = FakeKlipper(FAKE_MACHINE_PRINTER_CONFIG) if fake else None
    camera: Callable[[], Camera] | None = None
    if camera_image is not None:
        camera = functools.partial(_still, camera_image)
    elif klipper is not None:
        camera = functools.partial(_scene_camera, klipper, config_path, scene)
    servo_bus = _fake_servo_bus(config_path) if fake else None
    gamepad = FakeGamepad() if fake else None
    motion = _fake_motion_stage(config_path) if fake else None
    return Devices(
        config_path,
        journal_dir,
        klipper=klipper,
        camera=camera,
        audio=audio,
        servo_bus=servo_bus,
        gamepad=gamepad,
        motion=motion,
    )


def motion_settings(config: MotionConfig) -> McuMotionSettings:
    """[pnp.motion] を MCU のステージの設定に直す（ピンの ``^`` と ``!`` を解く）.

    付けられる記号はピンごとに決まっていて、それ以外（``~`` のプルダウンなど）は設定エラーにする。
    ``^`` はリミットスイッチと TMC2209 の受信ピンに、``!`` は dir・enable・リミットスイッチのピンに付けられる。
    LED とロードセルのピンには付けられない。

    Raises:
        ConfigError: ピンの書き方が不正な場合
    """
    tmc_rx_pin, tmc_rx_pull_up, _ = _pin(config.tmc_rx_pin, "tmc_rx_pin", pull_up=True)
    tmc_tx_pin, _, _ = _pin(config.tmc_tx_pin, "tmc_tx_pin")
    led_pin = None
    if config.led_pin is not None:
        led_pin, _, _ = _pin(config.led_pin, "led_pin")
    return McuMotionSettings(
        device=config.serial,
        kinematics=config.kinematics,
        x=_axis_settings("x", config.x),
        y=_axis_settings("y", config.y),
        z=_axis_settings("z", config.z),
        tmc_rx_pin=tmc_rx_pin,
        tmc_tx_pin=tmc_tx_pin,
        tmc_rx_pull_up=tmc_rx_pull_up,
        led_pin=led_pin,
        load_cell=(
            None if config.load_cell is None else _load_cell_settings(config.load_cell)
        ),
    )


# config_hx71x の gain_channel（HX711 データシート: 読み出しの 24 bit の後に足すパルスが 1 なら A-128、
# 2 なら B-32、3 なら A-64）
_GAIN_CHANNELS = {"A-128": 1, "B-32": 2, "A-64": 3}


def _load_cell_settings(config: LoadCellConfig) -> LoadCellSettings:
    sclk_pin, _, _ = _pin(config.sclk_pin, "load_cell.sclk_pin")
    dout_pin, _, _ = _pin(config.dout_pin, "load_cell.dout_pin")
    return LoadCellSettings(
        sclk_pin=sclk_pin,
        dout_pin=dout_pin,
        gain_channel=_GAIN_CHANNELS[config.gain],
        sample_rate=config.sample_rate,
        counts_per_gram=config.counts_per_gram,
        inverted=config.sensor_orientation == "inverted",
        contact=_contact_settings(config),
        free_tare_zone=config.free_tare_zone,
    )


def _contact_settings(config: LoadCellConfig) -> ContactSettings:
    return ContactSettings(
        trigger_force=config.trigger_force,
        release_force=config.release_force,
        press_positive=config.press_positive,
    )


def _axis_settings(name: str, axis: MotionAxisConfig) -> AxisSettings:
    step_pin, _, _ = _pin(axis.step_pin, f"{name}.step_pin")
    dir_pin, _, invert_dir = _pin(axis.dir_pin, f"{name}.dir_pin", invert=True)
    enable_pin, _, invert_enable = _pin(
        axis.enable_pin, f"{name}.enable_pin", invert=True
    )
    endstop_pin, endstop_pull_up, endstop_invert = _pin(
        axis.endstop_pin, f"{name}.endstop_pin", pull_up=True, invert=True
    )
    second_homing_speed = axis.second_homing_speed
    if second_homing_speed is None:
        second_homing_speed = axis.homing_speed / 2
    return AxisSettings(
        step_pin=step_pin,
        dir_pin=dir_pin,
        invert_dir=invert_dir,
        enable_pin=enable_pin,
        invert_enable=invert_enable,
        endstop_pin=endstop_pin,
        endstop_pull_up=endstop_pull_up,
        endstop_invert=endstop_invert,
        steps_per_mm=axis.steps_per_mm,
        position_min=axis.position_min,
        position_max=axis.position_max,
        position_endstop=axis.position_endstop,
        homing_positive=axis.homing_positive,
        homing_speed=axis.homing_speed,
        second_homing_speed=second_homing_speed,
        homing_retract=axis.homing_retract,
        max_velocity=axis.max_velocity,
        max_accel=axis.max_accel,
        tmc_address=axis.tmc_address,
        tmc=TmcSettings(
            run_current=axis.run_current,
            hold_current=axis.hold_current,
            sense_resistor=axis.sense_resistor,
            microsteps=axis.microsteps,
        ),
    )


def _pin(
    text: str, key: str, *, pull_up: bool = False, invert: bool = False
) -> tuple[str, bool, bool]:
    """Klipper の表記のピンを (名前, プルアップ, 反転) に分ける（先頭の ``^`` と ``!`` は順不同で 1 つずつ）."""
    name = text.lstrip("^!~")
    marks = text[: len(text) - len(name)]
    allowed = ("^" if pull_up else "") + ("!" if invert else "")
    if not name or len(set(marks)) != len(marks) or not set(marks) <= set(allowed):
        usable = (
            f"先頭に付けられる記号は {' と '.join(allowed)} だけ"
            if allowed
            else "先頭に記号を付けられない"
        )
        raise ConfigError(
            f"machine.toml の [pnp.motion] の {key}（{text!r}）が読めません（{usable}。"
            "^ はプルアップ、! は反転）",
            frozenset({"pnp"}),
        )
    return name, "^" in marks, "!" in marks


def _fake_motion_stage(config_path: Path) -> FakeMotionStage | None:
    try:
        motion = load_motion(config_path)
    except ConfigError:
        return None
    if motion is None:
        return None
    x, y, z = motion.x, motion.y, motion.z
    return FakeMotionStage(
        _motion_limits(motion),
        Velocity(x.max_velocity, y.max_velocity, z.max_velocity),
        load_cell=motion.load_cell is not None,
        contact=(
            None if motion.load_cell is None else _contact_settings(motion.load_cell)
        ),
        counts_per_gram=(
            1000.0 if motion.load_cell is None else motion.load_cell.counts_per_gram
        ),
    )


def _motion_limits(motion: MotionConfig) -> Limits:
    x, y, z = motion.x, motion.y, motion.z
    return Limits(
        x=ScalarLimits(x.position_min, x.position_max),
        y=ScalarLimits(y.position_min, y.position_max),
        z=ScalarLimits(z.position_min, z.position_max),
        v=ScalarLimits(0.0, max(x.max_velocity, y.max_velocity, z.max_velocity)),
    )


def _fake_servo_bus(config_path: Path) -> FakeServoBus:
    limits = {1: (0, 0)}
    try:
        pnp = load_machine(config_path).pnp
    except ConfigError:
        pnp = None
    servo = pnp.servo if pnp is not None else None
    if (
        servo is not None
        and servo.closed_angle is not None
        and servo.open_angle is not None
    ):
        low, high = sorted(
            hal.degrees_to_position(angle)
            for angle in (servo.closed_angle, servo.open_angle)
        )
        limits[2] = (low, high)
    return FakeServoBus({1: ServoMode.STEP, 2: ServoMode.POSITION}, angle_limits=limits)


def _still(image: Path) -> Camera:
    return FakeCamera([Image.load(image)], fps=15.0)


def _scene_camera(
    klipper: FakeKlipper, config_path: Path, scene: Callable[[], Sequence[Polygon]]
) -> Camera:
    """校正ファイルと machine.toml のとおりに、klipper の位置から scene を描くカメラ."""
    machine = load_machine(config_path)
    camera_model = CameraModel.load(machine.camera.calibration_file)
    solder = machine.solder
    # 塗布機でなければ塗布痕は描かれないので、ノズルの位置と吐出量の換算は使わない
    if solder is None:
        toolhead, rotations_per_ul = Point2d(0.0, 0.0), 1.0
    else:
        offset = solder.nozzle.offset
        toolhead = Point2d(offset.x, offset.y)
        rotations_per_ul = solder.dispenser.rotations_per_ul
    return SceneCamera(
        klipper,
        pixel_per_mm=camera_model.pixel_per_mm,
        toolhead=toolhead,
        rotations_per_ul=rotations_per_ul,
        size=camera_model.image_size,
        shapes=scene(),
    )
