"""例外 → HTTP ステータスの唯一の写像.

ハンドラは例外の型の継承をたどり、最も近い型のものが選ばれる。

BusyError（409）と ControlDeniedError（423）は RuntimeError の派生でも自分の型で写る。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from pcbasm.config import ConfigError
from pcbasm.errors import DeviceError
from pcbasm.kicad.units import KicadError
from pcbasm.pasting.testboard.config import BoardConfigError
from web.api.control import ControlDeniedError, LeaseInfo
from web.api.machine_config import InvalidSettingError
from web.api.routers.common import control_payload
from web.api.state import BusyError


def _detail(exc: Exception) -> str:
    return str(exc) or type(exc).__name__


@contextmanager
def hal_errors_to_502() -> Iterator[None]:
    """装置を扱う間に HAL が送出した RuntimeError / KeyError を、理由つきの 502 にする.

    printer.cfg の節やキーの不足は型を持たないので、範囲を区切って運転者へ文言を届ける。

    装置排他（BusyError）と操作権（ControlDeniedError）の検証は、巻き込まないようこの外で行う。
    """
    try:
        yield
    except (RuntimeError, KeyError) as exc:
        raise HTTPException(status_code=502, detail=_detail(exc)) from exc


def install_error_handlers(app: FastAPI) -> None:
    """例外ハンドラを app に登録する."""

    @app.exception_handler(DeviceError)
    async def device_error_handler(request: Request, exc: DeviceError) -> JSONResponse:
        """装置が要求を断った（Klipper の G-code エラー・停止中など）."""
        return JSONResponse(status_code=502, content={"detail": _detail(exc)})

    @app.exception_handler(httpx.HTTPError)
    async def moonraker_unreachable_handler(
        request: Request, exc: httpx.HTTPError
    ) -> JSONResponse:
        """Moonraker へ接続できない・タイムアウトした."""
        return JSONResponse(status_code=502, content={"detail": _detail(exc)})

    @app.exception_handler(BusyError)
    async def busy_error_handler(request: Request, exc: BusyError) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"detail": str(exc), "owner": exc.owner}
        )

    @app.exception_handler(ControlDeniedError)
    async def control_denied_handler(
        request: Request, exc: ControlDeniedError
    ) -> JSONResponse:
        """操作権の拒否を 423 Locked + 保持者情報にする."""
        holder: LeaseInfo = request.app.state.control.snapshot()
        return JSONResponse(
            status_code=423,
            content={
                "detail": str(exc),
                "holder": (
                    control_payload(holder).model_dump(mode="json")
                    if holder.held
                    else None
                ),
            },
        )

    @app.exception_handler(ConfigError)
    async def config_error_handler(request: Request, exc: ConfigError) -> JSONResponse:
        """設定エラー（degraded）の間は machine.toml を要る操作を 503 で断る."""
        return JSONResponse(
            status_code=503, content={"detail": f"設定エラーのため使えません: {exc}"}
        )

    @app.exception_handler(InvalidSettingError)
    async def invalid_setting_handler(
        request: Request, exc: InvalidSettingError
    ) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(BoardConfigError)
    async def paste_test_board_config_error_handler(
        request: Request, exc: BoardConfigError
    ) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(KicadError)
    async def kicad_error_handler(request: Request, exc: KicadError) -> JSONResponse:
        # KiCad footprint library / 座標範囲のエラー（テスト塗布基板生成）
        return JSONResponse(status_code=503, content={"detail": str(exc)})
