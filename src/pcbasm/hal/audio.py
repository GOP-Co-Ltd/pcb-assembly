"""ALSA を使った通知音再生.

音声ファイルの差し替えは ``src/pcbasm/hal/sounds/`` の ``success.wav`` /
``failure.wav`` / ``prompt.wav`` を非圧縮 16-bit PCM WAV で同名のまま上書きする
だけでよい。それ以外の設定変更は不要（フォーマットが不正なら再生時に
:class:`AudioPlaybackError` になるので WebUI のテスト再生で分かる）。

``prompt`` はジョブがオペレータ待ちに入ったことを知らせる音で、装置の前を離れた
作業者を呼び戻すのに使う。``success`` / ``failure`` はジョブの終了通知。どの場面で
どれを鳴らすかは :mod:`web.api.jobs.manager` が決める。
"""

from __future__ import annotations

import subprocess
import wave
from abc import ABC, abstractmethod
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Literal, override

import attrs
import numpy as np

from pcbasm.config import DEFAULT_AUDIO_DEVICE, Audio

Sound = Literal["success", "failure", "prompt"]

_APLAY_TIMEOUT = 30.0
_APLAY_LIST_TIMEOUT = 5.0
_DEFAULT_DEVICE_LABEL = "システム既定"
_SOUNDS_DIR = Path(__file__).with_name("sounds")
_SOUND_PATHS: dict[Sound, Path] = {
    "success": _SOUNDS_DIR / "success.wav",
    "failure": _SOUNDS_DIR / "failure.wav",
    "prompt": _SOUNDS_DIR / "prompt.wav",
}


class AudioPlaybackError(RuntimeError):
    """通知音を再生できない場合の例外."""


@attrs.frozen
class AudioDevice:
    """選択できる音声出力デバイス."""

    name: str  # aplay -D に渡す ALSA PCM 名
    label: str  # UI 表示名


def parse_aplay_devices(output: str) -> tuple[AudioDevice, ...]:
    """``aplay -L`` の出力から選択候補を抽出する.

    先頭は常にシステム既定 (``default``) で、続いて各カードの
    ``plughw:CARD=<card>,DEV=0`` を出力順に返す。ラベルは記述行の1行目をそのまま使う。
    """
    devices = [AudioDevice(DEFAULT_AUDIO_DEVICE, _DEFAULT_DEVICE_LABEL)]
    pending: str | None = None
    for line in output.splitlines():
        if not line.strip():
            continue
        if line[0].isspace():
            if pending is not None:
                devices.append(AudioDevice(pending, line.strip()))
                pending = None
            continue
        pending = line.strip() if _is_selectable_pcm(line.strip()) else None
    return tuple(devices)


def selectable_devices(
    devices: Sequence[AudioDevice], selected: str
) -> tuple[AudioDevice, ...]:
    """設定中デバイスが一覧に無ければ「（未検出）」付きで末尾に補う."""
    if any(device.name == selected for device in devices):
        return tuple(devices)
    return (*devices, AudioDevice(selected, f"{selected}（未検出）"))


def _is_selectable_pcm(name: str) -> bool:
    """``plughw:CARD=<card>,DEV=0`` のみを候補として採用する."""
    return name.startswith("plughw:CARD=") and name.endswith(",DEV=0")


class AudioPlayer(ABC):
    """通知音プレイヤーの抽象インターフェース."""

    @abstractmethod
    def list_devices(self) -> tuple[AudioDevice, ...]:
        """選択できる出力デバイスを列挙する."""

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
    def list_devices(self) -> tuple[AudioDevice, ...]:
        """``aplay -L`` の出力から候補を返す（列挙に失敗したら既定のみ）."""
        try:
            result = subprocess.run(
                ["aplay", "-L"],
                capture_output=True,
                text=True,
                timeout=_APLAY_LIST_TIMEOUT,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return parse_aplay_devices("")
        if result.returncode != 0:
            return parse_aplay_devices("")
        return parse_aplay_devices(result.stdout)

    @override
    def play(self, sound: Sound, config: Audio) -> Future[None]:
        if not self._closed:
            try:
                return self._executor.submit(self._play, sound, config)
            except RuntimeError:
                # close() と競合して executor が停止済み。契約どおり Future で返す
                pass
        return _closed_player_future()

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


def _closed_player_future() -> Future[None]:
    """終了済みプレイヤーが返す、失敗済みの Future."""
    future: Future[None] = Future()
    future.set_exception(AudioPlaybackError("音声プレイヤーは終了済みです"))
    return future


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
