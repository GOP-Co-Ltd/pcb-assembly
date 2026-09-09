"""通知音 HAL の公開契約テスト."""

import re
import wave
from pathlib import Path
from typing import get_args

import pytest

from pcbasm.config import Audio
from pcbasm.hal import audio as audio_module
from pcbasm.hal.audio import (
    AlsaAudioPlayer,
    AudioDevice,
    AudioPlaybackError,
    Sound,
    parse_aplay_devices,
    selectable_devices,
)
from tests.helpers import mark_hardware, skip_if_no_alsa_audio

# 実機（Raspberry Pi 5 + HifiBerry DAC）で採取した `aplay -L` の出力
APLAY_OUTPUT = """\
null
    Discard all samples (playback) or generate zero samples (capture)
default
    Default Audio Device
sysdefault
    Default Audio Device
hw:CARD=vc4hdmi0,DEV=0
    vc4-hdmi-0, MAI PCM i2s-hifi-0
    Direct hardware device without any conversions
plughw:CARD=vc4hdmi0,DEV=0
    vc4-hdmi-0, MAI PCM i2s-hifi-0
    Hardware device with all software conversions
default:CARD=vc4hdmi0
    vc4-hdmi-0, MAI PCM i2s-hifi-0
    Default Audio Device
sysdefault:CARD=vc4hdmi0
    vc4-hdmi-0, MAI PCM i2s-hifi-0
    Default Audio Device
hdmi:CARD=vc4hdmi0,DEV=0
    vc4-hdmi-0, MAI PCM i2s-hifi-0
    HDMI Audio Output
dmix:CARD=vc4hdmi0,DEV=0
    vc4-hdmi-0, MAI PCM i2s-hifi-0
    Direct sample mixing device
hw:CARD=sndrpihifiberry,DEV=0
    snd_rpi_hifiberry_dac, HifiBerry DAC HiFi pcm5102a-hifi-0
    Direct hardware device without any conversions
plughw:CARD=sndrpihifiberry,DEV=0
    snd_rpi_hifiberry_dac, HifiBerry DAC HiFi pcm5102a-hifi-0
    Hardware device with all software conversions
default:CARD=sndrpihifiberry
    snd_rpi_hifiberry_dac, HifiBerry DAC HiFi pcm5102a-hifi-0
    Default Audio Device
dmix:CARD=sndrpihifiberry,DEV=0
    snd_rpi_hifiberry_dac, HifiBerry DAC HiFi pcm5102a-hifi-0
    Direct sample mixing device
hw:CARD=vc4hdmi1,DEV=0
    vc4-hdmi-1, MAI PCM i2s-hifi-0
    Direct hardware device without any conversions
plughw:CARD=vc4hdmi1,DEV=0
    vc4-hdmi-1, MAI PCM i2s-hifi-0
    Hardware device with all software conversions
hdmi:CARD=vc4hdmi1,DEV=0
    vc4-hdmi-1, MAI PCM i2s-hifi-0
    HDMI Audio Output
"""

HARDWARE_DEVICE_PATTERN = re.compile(r"^(default|plughw:CARD=[^,]+,DEV=0)$")


def write_8bit_wav(path: Path) -> Path:
    """8-bit PCM（不正フォーマット）の WAV を書き出す."""
    with wave.open(str(path), "wb") as destination:
        destination.setnchannels(1)
        destination.setsampwidth(1)
        destination.setframerate(44_100)
        destination.writeframes(bytes(128))
    return path


class TestParseAplayDevices:
    """`aplay -L` 出力からの候補抽出（先頭は常にシステム既定 + 各カードの plughw DEV=0）."""

    def test_returns_default_first_then_plughw_devices_in_order(self):
        assert parse_aplay_devices(APLAY_OUTPUT) == (
            AudioDevice("default", "システム既定"),
            AudioDevice("plughw:CARD=vc4hdmi0,DEV=0", "vc4-hdmi-0, MAI PCM i2s-hifi-0"),
            AudioDevice(
                "plughw:CARD=sndrpihifiberry,DEV=0",
                "snd_rpi_hifiberry_dac, HifiBerry DAC HiFi pcm5102a-hifi-0",
            ),
            AudioDevice("plughw:CARD=vc4hdmi1,DEV=0", "vc4-hdmi-1, MAI PCM i2s-hifi-0"),
        )

    def test_returns_default_only_for_empty_output(self):
        assert parse_aplay_devices("") == (AudioDevice("default", "システム既定"),)

    @pytest.mark.parametrize(
        "name",
        [
            "null",
            "default",
            "sysdefault",
            "sysdefault:CARD=vc4hdmi0",
            "default:CARD=vc4hdmi0",
            "hw:CARD=vc4hdmi0,DEV=0",
            "dmix:CARD=vc4hdmi0,DEV=0",
            "hdmi:CARD=vc4hdmi0,DEV=0",
            "plughw:CARD=vc4hdmi0,DEV=1",
            "plughw:CARD=vc4hdmi0,DEV=2",
        ],
    )
    def test_excludes_non_selectable_pcm(self, name: str):
        output = f"{name}\n    some description\n"

        assert parse_aplay_devices(output) == (AudioDevice("default", "システム既定"),)


class TestSelectableDevices:
    """設定中デバイスの一覧への補完."""

    def test_keeps_list_when_selected_is_present(self):
        devices = parse_aplay_devices(APLAY_OUTPUT)

        assert (
            selectable_devices(devices, "plughw:CARD=sndrpihifiberry,DEV=0") == devices
        )

    def test_keeps_list_when_selected_is_default(self):
        devices = parse_aplay_devices("")

        assert selectable_devices(devices, "default") == devices

    def test_appends_missing_selected_device_as_undetected(self):
        devices = parse_aplay_devices("")

        assert selectable_devices(devices, "plughw:CARD=Gone,DEV=0") == (
            AudioDevice("default", "システム既定"),
            AudioDevice("plughw:CARD=Gone,DEV=0", "plughw:CARD=Gone,DEV=0（未検出）"),
        )


class TestSoundAssets:
    """`Sound` の全種が同梱の 16-bit PCM WAV に対応する（差し替え前提の契約）."""

    def test_every_sound_has_a_16bit_pcm_wav(self):
        paths = audio_module._SOUND_PATHS  # pyright: ignore[reportPrivateUsage]

        assert set(paths) == set(get_args(Sound))
        for sound, path in paths.items():
            assert path.is_file(), sound
            with wave.open(str(path), "rb") as source:
                assert source.getsampwidth() == 2, sound
                assert source.getcomptype() == "NONE", sound


class TestAlsaAudioPlayer:
    """AlsaAudioPlayer の終了契約と実 ALSA での再生."""

    def test_play_after_close_fails_with_playback_error(self):
        player = AlsaAudioPlayer()
        player.close()

        future = player.play("success", Audio())

        with pytest.raises(AudioPlaybackError, match="終了済み"):
            future.result(timeout=1.0)

    def test_close_is_idempotent(self):
        player = AlsaAudioPlayer()

        player.close()
        player.close()

        with pytest.raises(AudioPlaybackError):
            player.play("failure", Audio()).result(timeout=1.0)

    def test_play_racing_with_close_still_returns_failed_future(self):
        """終了処理と競合した play() も例外を投げず Future 経由で失敗を返す.

        終了フラグを見た後に executor が止まる並行呼び出しでは submit が同期 raise
        する。停止済みプレイヤーの終了フラグだけ戻して決定的に再現する。
        """
        player = AlsaAudioPlayer()
        player.close()
        player._closed = False  # pyright: ignore[reportPrivateUsage]

        future = player.play("success", Audio())

        with pytest.raises(AudioPlaybackError, match="終了済み"):
            future.result(timeout=1.0)

    def test_rejects_sound_file_that_is_not_16bit_pcm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """差し替えた音声ファイルが 16-bit PCM でなければ aplay に届く前に失敗する."""
        monkeypatch.setitem(
            audio_module._SOUND_PATHS,  # pyright: ignore[reportPrivateUsage]
            "success",
            write_8bit_wav(tmp_path / "success.wav"),
        )
        player = AlsaAudioPlayer()

        try:
            with pytest.raises(AudioPlaybackError, match="16-bit PCM"):
                player.play("success", Audio()).result(timeout=5.0)
        finally:
            player.close()

    @mark_hardware
    @skip_if_no_alsa_audio
    def test_lists_default_and_plughw_devices(self):
        player = AlsaAudioPlayer()
        try:
            devices = player.list_devices()
        finally:
            player.close()

        assert devices[0].name == "default"
        assert all(HARDWARE_DEVICE_PATTERN.match(device.name) for device in devices)

    @mark_hardware
    @skip_if_no_alsa_audio
    def test_plays_success_and_failure_sounds(self):
        player = AlsaAudioPlayer()
        try:
            assert player.play("success", Audio()).result(timeout=30.0) is None
            assert (
                player.play("failure", Audio(volume=0.3)).result(timeout=30.0) is None
            )
        finally:
            player.close()
