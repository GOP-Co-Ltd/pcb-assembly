"""ALSA を使った通知音再生."""

from __future__ import annotations

import subprocess
import wave
from abc import ABC, abstractmethod
from concurrent.futures import Future, ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Literal, override

import numpy as np

from pcbasm.config import Audio

Sound = Literal["success", "failure"]

_APLAY_TIMEOUT = 30.0
_SOUNDS_DIR = Path(__file__).with_name("sounds")
_SOUND_PATHS: dict[Sound, Path] = {
    "success": _SOUNDS_DIR / "success.wav",
    "failure": _SOUNDS_DIR / "failure.wav",
}


class AudioPlaybackError(RuntimeError):
    """通知音を再生できない場合の例外."""


class AudioPlayer(ABC):
    """通知音プレイヤーの抽象インターフェース."""

    @abstractmethod
    def play(self, sound: Sound, config: Audio) -> Future[None]:
        """通知音を非同期に再生する."""

    @abstractmethod
    def close(self) -> None:
        """プレイヤーを終了する."""


class AlsaAudioPlayer(AudioPlayer):
    """単一ワーカーで通知音を直列再生する ALSA プレイヤー."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="audio-playback"
        )
        self._closed = False

    @override
    def play(self, sound: Sound, config: Audio) -> Future[None]:
        if self._closed:
            future: Future[None] = Future()
            future.set_exception(AudioPlaybackError("音声プレイヤーは終了済みです"))
        else:
            future = self._executor.submit(self._play, sound, config)
        return future

    @override
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=True)

    @staticmethod
    def _play(sound: Sound, config: Audio) -> None:
        try:
            wav_data = _wav_with_gain(_SOUND_PATHS[sound], config.volume)
        except (OSError, ValueError, wave.Error) as exc:
            raise AudioPlaybackError(f"通知音を読み込めません: {exc}") from exc

        try:
            subprocess.run(
                ["aplay", "-q", "-D", config.device],
                input=wav_data,
                capture_output=True,
                check=True,
                timeout=_APLAY_TIMEOUT,
            )
        except FileNotFoundError as exc:
            raise AudioPlaybackError("aplay が見つかりません") from exc
        except subprocess.TimeoutExpired as exc:
            raise AudioPlaybackError(
                f"通知音の再生が {_APLAY_TIMEOUT:.0f} 秒でタイムアウトしました"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode(errors="replace").strip()
            message = f"aplay が終了コード {exc.returncode} で失敗しました"
            if detail:
                message = f"{message}: {detail}"
            raise AudioPlaybackError(message) from exc


def _wav_with_gain(path: Path, volume: float) -> bytes:
    with wave.open(str(path), "rb") as source:
        params = source.getparams()
        if params.sampwidth != 2 or params.comptype != "NONE":
            raise AudioPlaybackError(
                "通知音は非圧縮 16-bit PCM WAV である必要があります"
            )
        samples = np.frombuffer(source.readframes(params.nframes), dtype="<i2")

    gained = np.rint(samples.astype(np.float64) * volume)
    gained = np.clip(gained, -32768, 32767).astype("<i2")
    output = BytesIO()
    with wave.open(output, "wb") as destination:
        destination.setparams(params)
        destination.writeframes(gained.tobytes())
    return output.getvalue()
