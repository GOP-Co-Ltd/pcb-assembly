"""機体 1 台の装置（Klipper、カメラ、通知音）.

装置を組み立てる場所はここだけで、fake への切替もここで行う。
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from shapely import Polygon

from pcbasm import hal
from pcbasm.config import load_klipper, load_machine
from pcbasm.config.klipper import KlipperConfig
from pcbasm.geometry import Point2d
from pcbasm.hal import (
    AlsaAudioPlayer,
    AudioPlayer,
    Camera,
    FrameHub,
    KlipperClient,
    MoonrakerKlipper,
)
from pcbasm.hal.fake import (
    FAKE_MACHINE_PRINTER_CONFIG,
    FakeCamera,
    FakeKlipper,
    SceneCamera,
)
from pcbasm.machine.journal import Journal
from pcbasm.vision import CameraModel, Image

EMERGENCY_STOP_TIMEOUT = 10.0


class Devices:
    """機体 1 台の装置.

    作っただけでは外部装置へ接続しない。
    Moonraker へは最初の送信か問い合わせで、カメラへは最初に :meth:`frame_hub` を呼んだときに接続する。
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
    ) -> None:
        """Devices を初期化する.

        Args:
            config_path: machine.toml（[klipper] は接続先を渡されないときに、[camera] はカメラを開くときに読む）
            journal_dir: G-code ジャーナルの置き場所
            klipper: Moonraker の代わりに使う fake 機体（None は [klipper] の Moonraker）
            camera: カメラを開く関数（None は [camera] のカメラを開く）
            audio: 通知音のプレイヤー（None は ALSA で鳴らす）
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
        """
        if journal is None:
            journal = Journal.for_today(self._journal_dir)
        return self._klipper(endpoint).connection(timeout=timeout, journal=journal)

    def emergency_stop(self) -> None:
        """緊急停止を送る（使い回す HTTP クライアントとは別に送り、実行中の長い送信の後ろで待たない）.

        Raises:
            ConfigError: [klipper] が読めない場合
            KlipperError: Klipper が要求を断った場合
        """
        if self._fake_klipper is not None:
            self._fake_klipper.connection(
                timeout=EMERGENCY_STOP_TIMEOUT
            ).emergency_stop()
            return
        endpoint = load_klipper(self._config_path)
        klipper = hal.open_klipper(
            endpoint.host, endpoint.port, timeout=EMERGENCY_STOP_TIMEOUT
        )
        try:
            klipper.emergency_stop()
        finally:
            klipper.close()

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

    def close(self) -> None:
        """後始末（通知音を閉じ、FrameHub を止め、Moonraker への HTTP クライアントを閉じる）.

        実行中のジョブの完了音を捨てないよう、ジョブを止めてから呼ぶ。
        """
        self._audio.close()
        self.rebuild_camera()
        with self._klipper_lock:
            moonraker = self._moonraker
            self._moonraker = None
        if moonraker is not None:
            moonraker.close()

    def _klipper(
        self, endpoint: KlipperConfig | None
    ) -> MoonrakerKlipper | FakeKlipper:
        if self._fake_klipper is not None:
            return self._fake_klipper
        if endpoint is None:
            endpoint = load_klipper(self._config_path)
        with self._klipper_lock:
            if self._moonraker is None or endpoint != self._endpoint:
                # 古いクライアントは閉じない（実行中のジョブの接続がまだ使っているかもしれない）
                self._moonraker = hal.open_klipper(endpoint.host, endpoint.port)
                self._endpoint = endpoint
            return self._moonraker

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
        fake: Klipper を実機と同じ printer.cfg の FakeKlipper にし、カメラをその位置から scene を描く SceneCamera にする
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
    return Devices(
        config_path, journal_dir, klipper=klipper, camera=camera, audio=audio
    )


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
