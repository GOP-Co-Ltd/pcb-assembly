"""カメラプレビュー（MJPEG ストリーム / スナップショット）の API."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Generator
from typing import override

import anyio
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from webui.dependencies import PreviewDep, StateDep
from webui.preview import MJPEG_MEDIA_TYPE, OverlayKind
from webui.state import AppState

router = APIRouter(prefix="/api")

# ジェネレータ close の再試行上限。FrameSource.capture の timeout（5s）より
# 長く取り、worker thread が next() 実行中でも close を完遂できるようにする
_CLOSE_DEADLINE = 10.0


def _close_when_suspended(gen: Generator[bytes]) -> None:
    """ジェネレータが yield で停止するのを待って close する.

    クライアント切断はキャンセルで伝わるため、worker thread が next() を 実行中（= generator
    already executing）のことがある。その場合は 停止を待って再試行する。
    """
    deadline = time.monotonic() + _CLOSE_DEADLINE
    while True:
        try:
            gen.close()
            return
        except ValueError:  # generator already executing
            if time.monotonic() >= deadline:
                return  # 最後は GC の finalizer に任せる
            time.sleep(0.05)


class _ClosingStreamingResponse(StreamingResponse):
    """切断・エラー時に同期ジェネレータを確実に close する StreamingResponse.

    starlette は sync iterator をクライアント切断時に close しない （GC
    任せで遅延・不確定）ため、参照カウント解放（mjpeg_stream の finally）を決定的に走らせる。
    """

    def __init__(self, content: Generator[bytes], media_type: str) -> None:
        super().__init__(content, media_type=media_type)
        self._sync_content = content

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(_close_when_suspended, self._sync_content)

    @override
    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            # キャンセル中でも close を完遂する
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(_close_when_suspended, self._sync_content)


@router.get("/preview/stream")
def preview_stream(
    state: StateDep,
    preview: PreviewDep,
    overlay: OverlayKind = "none",
    canny_low: float | None = None,
    canny_high: float | None = None,
) -> StreamingResponse:
    # ジェネレータ方式では最初のフレーム取得前にエラーを検出できないため、
    # カメラ構築エラーはここで 503 に変換する（配信開始後のエラーは
    # ストリーム切断として扱い、クライアントのリトライに任せる）
    _build_hub_or_503(state)
    return _ClosingStreamingResponse(
        preview.mjpeg_stream(overlay, canny_low, canny_high),
        media_type=MJPEG_MEDIA_TYPE,
    )


@router.get("/preview/snapshot")
def preview_snapshot(
    state: StateDep,
    preview: PreviewDep,
    overlay: OverlayKind = "none",
    canny_low: float | None = None,
    canny_high: float | None = None,
) -> Response:
    _build_hub_or_503(state)
    try:
        jpeg = preview.snapshot(overlay, canny_low, canny_high)
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(content=jpeg, media_type="image/jpeg")


def _build_hub_or_503(state: AppState) -> None:
    """FrameHub を（必要なら）構築し、カメラ構築失敗を 503 へ変換する."""
    try:
        state.frame_hub()
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=503, detail=f"カメラを構築できません: {exc}"
        ) from exc
