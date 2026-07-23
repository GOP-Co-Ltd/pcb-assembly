"""開発用の通知音テスト API."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from webui.dependencies import AudioPlayerDep, StateDep

_PLAYBACK_TIMEOUT = 10.0

router = APIRouter(prefix="/api/dev/audio", tags=["dev"])


class AudioTestRequest(BaseModel):
    """通知音テストのリクエスト."""

    sound: Literal["success", "failure"]


class AudioTestResponse(BaseModel):
    """通知音テストのレスポンス."""

    played: bool
    device: str


@router.post("/test", response_model=AudioTestResponse)
def test_audio(
    body: AudioTestRequest, state: StateDep, audio_player: AudioPlayerDep
) -> AudioTestResponse:
    """選択中マシンの音声設定で通知音を再生する."""
    config = state.machine().audio
    if config is None:
        raise HTTPException(
            status_code=400, detail="選択中のマシンにaudio設定がありません"
        )
    try:
        audio_player.play(body.sound, config).result(timeout=_PLAYBACK_TIMEOUT)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"通知音を再生できませんでした: {exc}"
        ) from exc
    return AudioTestResponse(played=True, device=config.device)
