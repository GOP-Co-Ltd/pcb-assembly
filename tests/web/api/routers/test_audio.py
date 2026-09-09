"""`web.api.routers.audio` の仕様テスト.

計画書 memory/agents/implementation-planner 相当（`/home/gop/.claude/plans/
claude-mr-141-main-webui-velvet-gem.md`）「`src/webui/routers/audio.py`」節が契約
（2 プロセス分離後、音を鳴らすのは Pi の ALSA なので router は backend にある）:

- GET /api/audio/settings は machine.toml 解決済みの device / volume と、
  プレイヤーが列挙したデバイス候補を返す（`[audio]` 未設定でも既定値）
- 設定中デバイスが候補に無ければ末尾へ補完され、`device` はそのまま保持される
- POST /api/audio/test は現在の設定で再生し、再生失敗は 502、未知の音種は 422

MR6（操作権リース）が追記契約:

- GET /api/audio/settings は閲覧者にも開放（全 GET と同じ扱い）
- POST /api/audio/test は機体のスピーカーを鳴らすので操作権を要し、
  保持者以外は 423（`ControlDep` がハンドラ本体に入る前に断る）
"""

import pytest
from fastapi.testclient import TestClient

from pcbasm.config import DEFAULT_AUDIO_DEVICE, DEFAULT_AUDIO_VOLUME, Audio
from pcbasm.hal import AudioDevice, AudioPlaybackError
from tests.helpers import FAKE_AUDIO_DEVICES, FakeAudioPlayer
from web.api.app import create_app
from web.api.settings import Settings


class TestAudioSettingsApi:
    """GET /api/audio/settings."""

    def test_get_returns_defaults_and_player_devices(self, client: TestClient):
        """`[audio]` 未設定の machine.toml でも既定値が解決されて返る."""
        response = client.get("/api/audio/settings")

        assert response.status_code == 200
        assert response.json() == {
            "devices": [
                {"name": device.name, "label": device.label}
                for device in FAKE_AUDIO_DEVICES
            ],
            "device": DEFAULT_AUDIO_DEVICE,
            "volume": DEFAULT_AUDIO_VOLUME,
        }

    def test_get_reflects_saved_settings(self, client: TestClient):
        selected = FAKE_AUDIO_DEVICES[1].name
        put = client.put(
            "/api/settings/machine",
            json={"values": {"audio.device": selected, "audio.volume": 0.4}},
        )
        assert put.status_code == 200, put.text

        data = client.get("/api/audio/settings").json()

        assert data["device"] == selected
        assert data["volume"] == 0.4

    def test_get_appends_unlisted_selected_device(
        self, webui_settings: Settings, audio_player: FakeAudioPlayer
    ):
        """列挙されないデバイスが設定されていても候補末尾に現れ、選択値は保持される."""
        with TestClient(
            create_app(webui_settings, audio_player=audio_player)
        ) as client:
            client.put(
                "/api/settings/machine",
                json={"values": {"audio.device": "plughw:CARD=Unplugged,DEV=0"}},
            )
            data = client.get("/api/audio/settings").json()

        assert data["device"] == "plughw:CARD=Unplugged,DEV=0"
        assert data["devices"][-1] == {
            "name": "plughw:CARD=Unplugged,DEV=0",
            "label": "plughw:CARD=Unplugged,DEV=0（未検出）",
        }


class TestAudioTestApi:
    """POST /api/audio/test."""

    @pytest.mark.parametrize("sound", ["success", "failure", "prompt"])
    def test_plays_selected_sound_with_current_settings(
        self, client: TestClient, audio_player: FakeAudioPlayer, sound: str
    ):
        response = client.post("/api/audio/test", json={"sound": sound})

        assert response.status_code == 200
        assert response.json() == {"played": True, "device": DEFAULT_AUDIO_DEVICE}
        assert audio_player.played == ((sound, Audio()),)

    def test_returns_502_when_playback_fails(self, webui_settings: Settings):
        player = FakeAudioPlayer(
            playback_error=AudioPlaybackError("speaker disconnected")
        )

        with TestClient(create_app(webui_settings, audio_player=player)) as client:
            response = client.post("/api/audio/test", json={"sound": "failure"})

        assert response.status_code == 502
        assert "speaker disconnected" in response.text

    @pytest.mark.parametrize(
        "body",
        [{"sound": "other"}, {"sound": ""}, {"sound": None}, {}],
        ids=["unknown", "blank", "null", "missing"],
    )
    def test_rejects_invalid_sound(
        self,
        client: TestClient,
        audio_player: FakeAudioPlayer,
        body: dict[str, object],
    ):
        response = client.post("/api/audio/test", json=body)

        assert response.status_code == 422
        assert audio_player.played == ()


class TestDeviceListSource:
    """デバイス候補はプレイヤーの列挙結果に従う（router は絞り込まない）."""

    def test_single_device_player_returns_only_that_device(
        self, webui_settings: Settings
    ):
        only = AudioDevice(DEFAULT_AUDIO_DEVICE, "システム既定")
        player = FakeAudioPlayer(devices=[only])

        with TestClient(create_app(webui_settings, audio_player=player)) as client:
            data = client.get("/api/audio/settings").json()

        assert data["devices"] == [{"name": only.name, "label": only.label}]
