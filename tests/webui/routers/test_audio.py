"""開発用の Raspberry Pi 音声テスト API の仕様テスト."""

from pathlib import Path

import pytest
import tomlkit
from fastapi.testclient import TestClient

from pcbasm.config import Audio
from tests.helpers import FakeAudioPlayer
from webui.app import create_app
from webui.settings import Settings


def _set_audio(configs_root: Path, audio: Audio | None) -> None:
    path = configs_root / "kurousagi" / "machine.toml"
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    if audio is None:
        document.pop("audio", None)
    else:
        document["audio"] = {"device": audio.device, "volume": audio.volume}
    path.write_text(tomlkit.dumps(document), encoding="utf-8")


class TestAudioTestApi:
    """POST /api/dev/audio/test."""

    @pytest.mark.parametrize("sound", ["success", "failure"])
    def test_plays_selected_sound_and_returns_device(
        self,
        webui_settings: Settings,
        configs_root: Path,
        sound: str,
    ):
        audio = Audio(device="test-speaker", volume=0.25)
        _set_audio(configs_root, audio)
        player = FakeAudioPlayer()

        with TestClient(create_app(webui_settings, audio_player=player)) as client:
            response = client.post("/api/dev/audio/test", json={"sound": sound})

        assert response.status_code == 200
        assert response.json() == {"played": True, "device": audio.device}
        assert player.played == ((sound, audio),)

    def test_returns_400_when_audio_is_not_configured(
        self, webui_settings: Settings, configs_root: Path
    ):
        _set_audio(configs_root, None)
        player = FakeAudioPlayer()

        with TestClient(create_app(webui_settings, audio_player=player)) as client:
            response = client.post("/api/dev/audio/test", json={"sound": "success"})

        assert response.status_code == 400
        assert player.played == ()

    def test_returns_502_when_playback_fails(
        self, webui_settings: Settings, configs_root: Path
    ):
        _set_audio(configs_root, Audio(device="test-speaker"))
        player = FakeAudioPlayer(RuntimeError("speaker disconnected"))

        with TestClient(create_app(webui_settings, audio_player=player)) as client:
            response = client.post("/api/dev/audio/test", json={"sound": "failure"})

        assert response.status_code == 502
        assert "speaker disconnected" in response.text

    @pytest.mark.parametrize(
        "body",
        [{"sound": "other"}, {"sound": ""}, {"sound": None}, {}],
        ids=["unknown", "blank", "null", "missing"],
    )
    def test_rejects_invalid_sound(
        self, webui_settings: Settings, body: dict[str, object]
    ):
        with TestClient(
            create_app(webui_settings, audio_player=FakeAudioPlayer())
        ) as client:
            response = client.post("/api/dev/audio/test", json=body)

        assert response.status_code == 422
