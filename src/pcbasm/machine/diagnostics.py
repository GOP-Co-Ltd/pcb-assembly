"""装置診断（Klipper の接続、printer.cfg の必須 section、カメラ解像度と校正の一致、ALSA）.

読み取りだけで、G-code は送らない。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import attrs
import httpx

from pcbasm.config import MachineConfig
from pcbasm.hal import AudioDevice, Camera, KlipperClient, PasteDispenser, XYZStage
from pcbasm.machine.park import PRESENT_SECTION
from pcbasm.vision import CalibrationResult

# ProbeExecutor（pcbasm.pasting.probe）が要求する section
# pasting は machine より上の層で import できないので、ProbeExecutor を作らずに section だけを見る
_PROBE_SECTION = "load_cell_probe"


@attrs.frozen
class Check:
    """診断 1 項目の結果.

    Attributes:
        name: 項目名
        ok: 合格か
        detail: 確かめた値か、不合格の理由
    """

    name: str
    ok: bool
    detail: str


def check_klipper(klipper: KlipperClient, machine: MachineConfig) -> list[Check]:
    """Klipper の状態と、printer.cfg にステージ・ディスペンサー・プローブ・駐機が要求する section があるか確かめる.

    接続できなければ printer.cfg の項目は並べず、shutdown（MCU の切断など）では読めるので並べる。
    """
    name = "Klipper の接続"
    endpoint = f"{machine.klipper.host}:{machine.klipper.port}"
    try:
        config = klipper.get_config()
        state = klipper.get_status("webhooks", "state")
        message = klipper.get_status("webhooks", "state_message")
    except httpx.HTTPError as exc:
        return [Check(name, False, f"{endpoint} に接続できません: {exc}")]
    if state == "ready":
        connection = Check(name, True, endpoint)
    else:
        connection = Check(
            name, False, f"{endpoint} の Klipper が {state} です: {message}"
        )
    return [
        connection,
        _component("ステージ", lambda: _stage(klipper)),
        _component("ディスペンサー", lambda: _dispenser(klipper, machine)),
        _section(config, "プローブ", _PROBE_SECTION),
        _section(config, "駐機", PRESENT_SECTION),
    ]


def check_camera(
    open_camera: Callable[[], AbstractContextManager[Camera]], calibration_file: Path
) -> Check:
    """カメラから 1 枚撮り、その解像度が校正ファイルの解像度と一致するか確かめる."""
    name = "カメラの解像度"
    try:
        calibration = CalibrationResult.load(calibration_file)
    except OSError as exc:
        return Check(name, False, f"校正ファイルを読めません: {exc}")
    try:
        with open_camera() as camera:
            size = camera.capture().size
    except (OSError, RuntimeError) as exc:
        return Check(name, False, f"カメラから撮れません: {exc}")
    if size != calibration.resolution:
        return Check(
            name,
            False,
            f"カメラ {_size(size)} が校正 {_size(calibration.resolution)} と違います",
        )
    return Check(name, True, f"{_size(size)}（校正と一致）")


def check_audio(devices: Sequence[AudioDevice], device: str) -> Check:
    """[audio] の出力デバイスが ALSA の再生デバイスに見えるか確かめる.

    Args:
        devices: ``AudioPlayer.list_devices()`` の結果（先頭は常にシステム既定）
        device: [audio] の出力デバイス
    """
    name = "通知音（ALSA）"
    if len(devices) < 2:
        return Check(name, False, "ALSA の再生デバイスが見つかりません")
    for candidate in devices:
        if candidate.name == device:
            return Check(name, True, candidate.label)
    return Check(name, False, f"出力デバイス {device} が見つかりません")


def _component(name: str, build: Callable[[], str]) -> Check:
    """HAL の部品を作り、printer.cfg の不足で作れなければその理由を返す."""
    try:
        return Check(name, True, build())
    except (KeyError, RuntimeError) as exc:
        return Check(name, False, str(exc.args[0]))


def _stage(klipper: KlipperClient) -> str:
    limits = XYZStage(klipper).limits
    return (
        f"X {limits.x.min:g}〜{limits.x.max:g} / Y {limits.y.min:g}〜{limits.y.max:g}"
        f" / Z {limits.z.min:g}〜{limits.z.max:g} mm、最大 {limits.v.max:g} mm/s"
    )


def _dispenser(klipper: KlipperClient, machine: MachineConfig) -> str:
    PasteDispenser(klipper, machine.paste_dispenser.rotations_per_ul)
    return "必要な section があります"


def _section(config: dict[str, dict[str, Any]], name: str, section: str) -> Check:
    if section in config:
        return Check(name, True, f"[{section}] があります")
    return Check(name, False, f"printer.cfg に [{section}] を追加してください")


def _size(size: tuple[int, int]) -> str:
    return f"{size[0]}x{size[1]}"
