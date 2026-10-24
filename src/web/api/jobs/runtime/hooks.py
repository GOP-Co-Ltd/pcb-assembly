"""ジョブの節目に装置へさせること（終了時の駐機と、入力待ち・完了の通知音）.

どちらも best-effort で、失敗してもジョブの結果は変えない。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future

from pcbasm.config.audio import AudioConfig
from pcbasm.hal import XYZStage
from pcbasm.hal.audio import Sound
from pcbasm.machine.devices import Devices
from pcbasm.machine.park import PRESENT_TIMEOUT, park
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import JobStatus

_logger = logging.getLogger(__name__)


def park_after_job(ctx: JobContext) -> None:
    """装置を動かしたジョブの終了時に、ノズルキャップ（未記録なら PRESENT）へ退避して脱力する."""
    try:
        klipper = ctx.klipper(PRESENT_TIMEOUT)
        park(
            klipper,
            XYZStage(klipper),
            ctx.machine.paste_dispenser.nozzle_cap.position,
            relax=True,
            warn=ctx.log,
        )
    except Exception as exc:
        ctx.log(f"タスク終了時の退避に失敗: {exc}")


def operator_notifier(devices: Devices, audio: AudioConfig) -> Callable[[], None]:
    """オペレータ待ちで鳴らす入力待ち音の再生関数を作る.

    再生完了を待たずに戻り、再生失敗は warning のみ（待ちをブロックしない）。
    """
    return lambda: _play(devices, "prompt", audio, "入力待ち音")


def play_completion_sound(
    devices: Devices, audio: AudioConfig, status: JobStatus
) -> None:
    """成功・失敗の通知音を非同期に開始する（中止は鳴らさない。失敗は warning のみ）.

    鳴らすのは装置を動かすジョブ（``uses_machine``）だけで、呼び出し側が判定する。
    実時間がかかり作業者が装置の前を離れうるのはこの区分で、PCB 生成のような即終了ジョブは画面で足りる。
    """
    if status not in (JobStatus.SUCCEEDED, JobStatus.FAILED):
        return
    sound: Sound = "success" if status is JobStatus.SUCCEEDED else "failure"
    _play(devices, sound, audio, "ジョブ完了通知音")


def _play(devices: Devices, sound: Sound, audio: AudioConfig, label: str) -> None:
    """通知音を非同期に開始する（開始・再生の失敗はいずれも warning のみ）."""
    try:
        future = devices.audio.play(sound, audio.device, audio.volume)
    except Exception:
        _logger.warning("%sを開始できませんでした", label, exc_info=True)
        return
    future.add_done_callback(_warn_sound_failure(label))


def _warn_sound_failure(label: str) -> Callable[[Future[None]], None]:
    """通知音の再生失敗を warning ログに記録する done callback を作る."""

    def callback(future: Future[None]) -> None:
        if (error := future.exception()) is not None:
            _logger.warning("%sの再生に失敗しました: %s", label, error)

    return callback
