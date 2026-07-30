"""ジョブ完了通知音の設定取得とテスト再生 API."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pcbasm.hal import AudioPlaybackError, selectable_devices
from webui.dependencies import AudioPlayerDep, StateDep

# 再生完了を待つ上限。success.wav が 4.8 秒あり、直前の再生が残っていると
# キュー待ちが加わるため 10 秒では誤タイムアウトしうる
_PLAYBACK_TIMEOUT = 15.0

router = APIRouter(prefix="/api/audio", tags=["audio"])


class AudioDeviceOption(BaseModel):
    """選択できる出力デバイス 1 件."""

    name: str
    label: str


class AudioSettingsResponse(BaseModel):
    """通知音の設定と選択候補."""

    devices: list[AudioDeviceOption]
    device: str
    volume: float


class AudioTestRequest(BaseModel):
    """通知音テストのリクエスト."""

    sound: Literal["success", "failure"]


class AudioTestResponse(BaseModel):
    """通知音テストのレスポンス."""

    played: bool
    device: str


@router.get("/settings")
def get_audio_settings(
    state: StateDep, audio_player: AudioPlayerDep
) -> AudioSettingsResponse:
    """machine.toml で解決済みの通知音設定と、選択できるデバイス一覧を返す."""
    audio = state.machine().audio
    devices = selectable_devices(audio_player.list_devices(), audio.device)
    return AudioSettingsResponse(
        devices=[
            AudioDeviceOption(name=device.name, label=device.label)
            for device in devices
        ],
        device=audio.device,
        volume=audio.volume,
    )


@router.post("/test")
def test_audio(
    body: AudioTestRequest, state: StateDep, audio_player: AudioPlayerDep
) -> AudioTestResponse:
    """現在の通知音設定で指定の音を再生する."""
    audio = state.machine().audio
    try:
        audio_player.play(body.sound, audio).result(timeout=_PLAYBACK_TIMEOUT)
    except (AudioPlaybackError, TimeoutError) as exc:
        raise HTTPException(
            status_code=502, detail=f"通知音を再生できませんでした: {exc}"
        ) from exc
    return AudioTestResponse(played=True, device=audio.device)
