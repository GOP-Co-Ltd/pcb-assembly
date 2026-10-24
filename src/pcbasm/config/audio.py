"""[audio] 節：通知音の出力先と音量（節ごと省略可）."""

from math import isfinite

import attrs

DEFAULT_AUDIO_DEVICE = "default"  # ALSA のシステム既定 PCM
DEFAULT_AUDIO_VOLUME = 0.75


def validate_audio_device(device: str) -> str | None:
    """通知音の出力デバイス名を検証する."""
    if not device.strip():
        return "audio.deviceは空でない文字列である必要があります"
    return None


def validate_audio_volume(volume: float) -> str | None:
    """通知音の音量を検証する."""
    if not isfinite(volume) or not 0.0 <= volume <= 1.0:
        return f"audio.volumeは0以上1以下の有限値である必要があります: {volume!r}"
    return None


@attrs.frozen
class AudioConfig:
    """通知音の出力設定."""

    device: str = DEFAULT_AUDIO_DEVICE
    volume: float = DEFAULT_AUDIO_VOLUME

    def __attrs_post_init__(self) -> None:
        if error := validate_audio_device(self.device):
            raise ValueError(error)
        if isinstance(self.volume, bool) or not isinstance(self.volume, (int, float)):
            raise ValueError(
                f"audio.volumeは0以上1以下の有限値である必要があります: {self.volume!r}"
            )
        volume = float(self.volume)
        if error := validate_audio_volume(volume):
            raise ValueError(error)
        object.__setattr__(self, "device", self.device.strip())
        object.__setattr__(self, "volume", volume)
