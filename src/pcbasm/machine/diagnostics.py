"""装置診断（Klipper の接続、printer.cfg の必須 section、カメラ解像度と校正の一致、ALSA）.

読み取りだけで、G-code は送らない。
直近の結果はファイルに残し、立ち上げ状況（:mod:`pcbasm.commissioning`）が読む。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import attrs
import httpx

from pcbasm.atomic import write_text_atomic
from pcbasm.config import MachineConfig
from pcbasm.document import structure_document
from pcbasm.hal import AudioDevice, Camera, KlipperClient, PasteDispenser, XYZStage
from pcbasm.machine.park import PRESENT_SECTION
from pcbasm.vision import CameraModel

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


def save_checks(path: Path, checks: Sequence[Check]) -> None:
    """診断の結果を、直近の結果として残す（前の結果は置き換える）."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [attrs.asdict(check) for check in checks]
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_checks(path: Path) -> tuple[tuple[Check, ...] | None, str | None]:
    """:func:`save_checks` が残した直近の結果を読む.

    Returns:
        ``(checks, None)``。まだ診断していないか、読めなければ ``(None, 理由)``
    """
    if not path.is_file():
        return None, "まだ診断していません"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return structure_document(data, tuple[Check, ...]), None
    except Exception:  # Check の field を変える前に残した結果、手で壊したファイル
        return (
            None,
            f"直近の結果 {path.name} を読めません。装置診断をやり直してください",
        )


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
    """校正がジョブに使えるかと、カメラの解像度が校正と一致するかを確かめる."""
    name = "カメラと校正"
    try:
        camera_model = CameraModel.load(calibration_file)
    except OSError as exc:
        return Check(name, False, f"校正ファイルを読めません: {exc}")
    except ValueError as exc:  # 焦点 Z の無い校正
        return Check(name, False, str(exc))
    try:
        with open_camera() as camera:
            size = camera.capture().size
    except (OSError, RuntimeError) as exc:
        return Check(name, False, f"カメラから撮れません: {exc}")
    error = camera_model.image_size_error(size)
    if error is not None:
        return Check(name, False, error)
    return Check(name, True, f"{size[0]}x{size[1]}（校正と一致）")


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
