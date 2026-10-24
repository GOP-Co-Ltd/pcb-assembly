"""[audio] 節：通知音の出力先と音量（節ごと省略可）."""

from math import isfinite

import attrs

from pcbasm.config.fields import setting

DEFAULT_AUDIO_DEVICE = "default"  # ALSA のシステム既定 PCM
DEFAULT_AUDIO_VOLUME = 0.75


def _device(_: object, __: attrs.Attribute, value: str) -> None:
    if not value.strip():
        raise ValueError("audio.deviceは空でない文字列である必要があります")


def _volume(_: object, __: attrs.Attribute, value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise ValueError(
            f"audio.volumeは0以上1以下の有限値である必要があります: {value!r}"
        )


@attrs.frozen
class AudioConfig:
    """通知音の出力設定."""

    device: str = attrs.field(
        default=DEFAULT_AUDIO_DEVICE,
        validator=_device,
        metadata=setting(label="出力デバイス", group="通知音", order=1),
    )
    volume: float = attrs.field(
        default=DEFAULT_AUDIO_VOLUME,
        validator=_volume,
        metadata=setting(label="音量", group="通知音", order=2, minimum=0.0),
    )

    def __attrs_post_init__(self) -> None:
        object.__setattr__(self, "device", self.device.strip())
        object.__setattr__(self, "volume", float(self.volume))
