"""音声出力 HAL の公開契約テスト."""

import pytest

from pcbasm.config import Audio, get_machine_config
from pcbasm.hal.audio import AlsaAudioPlayer
from tests.helpers import (
    FakeAudioPlayer,
    mark_hardware,
    skip_if_no_alsa_audio,
)


class TestAudioPlayer:
    """AudioPlayer の Future 契約と実 ALSA adapter."""

    def test_fake_reports_completion_and_records_request(self):
        player = FakeAudioPlayer()
        config = Audio(device="test-device", volume=0.4)

        future = player.play("success", config)

        assert future.result(timeout=1.0) is None
        assert player.played == (("success", config),)

    @mark_hardware
    @skip_if_no_alsa_audio
    def test_alsa_player_plays_success_sound_on_configured_device(self):
        config = get_machine_config("kurousagi").audio
        if config is None:
            pytest.skip("kurousagi の [audio] が設定されていません")
        player = AlsaAudioPlayer()
        try:
            assert player.play("success", config).result(timeout=10.0) is None
        finally:
            player.close()
