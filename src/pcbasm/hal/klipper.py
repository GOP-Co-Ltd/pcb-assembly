"""Moonraker 経由で Klipper と通信するクライアント.

HAL の他のクラス（``XYZStage`` / ``ManualStepper`` / ``AirPump`` /
``PasteDispenser``）は G-code を生成するだけで送信しない。
生成した G-code を装置へ送るのは :meth:`Klipper.send_gcode` だけである。
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

import attrs
import httpx

from pcbasm.gcode import PRESENT_MACRO, GCode, GCodeLike

logger = logging.getLogger(__name__)

PRESENT_TIMEOUT = 30.0


@attrs.frozen
class GCodeMacro:
    """Klipper マクロ情報を保持するクラス."""

    gcode: GCode
    description: str | None = None
    variables: dict[str, Any] = attrs.Factory(dict)


class Klipper:
    """Moonraker REST API のシンプルなラッパー.

    Example:
        klipper = Klipper("192.168.1.100")
        klipper.send_gcode(GCode.homing(x=True, y=True, z=True) + GCode.wait_for_done())
        position = klipper.get_status("gcode_move", "gcode_position")
    """

    def __init__(
        self, host: str = "localhost", port: int = 7125, timeout: float | None = None
    ) -> None:
        """Klipper クライアントを初期化する.

        Args:
            host: Moonraker サーバーのホスト IP アドレス
            port: Moonraker サーバーのポート番号
            timeout: HTTP リクエストのタイムアウト秒数（None は無制限）
        """
        self._base_url = f"http://{host}:{port}"
        self._client = httpx.Client(timeout=timeout)
        self._readonly = ReadonlyKlipper(self)

    def __del__(self) -> None:
        """破棄時に httpx クライアントを閉じる."""
        if hasattr(self, "_client"):
            self._client.close()

    @property
    def readonly(self) -> ReadonlyKlipper:
        """G-code を送れない読み取り専用ビュー（HAL クラスの初期化に渡す）."""
        return self._readonly

    def send_gcode(
        self, gcode: GCodeLike, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """G-code を送信し、Klipper がスクリプトを処理し終えるまで待つ.

        処理済みでも移動が物理的に終わったとは限らない。
        到達を待つときは末尾に ``GCode.wait_for_done()``（M400）を付ける。

        Args:
            gcode: 送信する G-code コマンド（文字列、Iterable、または GCode オブジェクト）
            timeout: この送信だけに適用する HTTP リクエストタイムアウト秒数

        Returns:
            Moonraker からの応答

        Raises:
            RuntimeError: Moonraker が HTTP 400 以上を返した場合
                （G-code エラー・Klipper 停止中など）
            httpx.HTTPError: 接続できない・タイムアウトした場合（包まずに送出する）
        """
        url = f"{self._base_url}/printer/gcode/script"
        params = {"script": str(GCode(gcode))}
        if timeout is None:
            response = self._client.post(url, params=params)
        else:
            response = self._client.post(url, params=params, timeout=timeout)
        if response.status_code >= 400:
            raise RuntimeError(response.reason_phrase)
        return response.json()

    def get_status(self, object: str, attribute: str) -> Any:
        """指定したオブジェクトの属性値を取得する.

        Args:
            object: オブジェクト名（gcode_move, toolhead 等）
            attribute: 属性名（gcode_position, homed_axes 等）

        Returns:
            属性の値

        Raises:
            httpx.HTTPError: 接続失敗、または HTTP エラー応答の場合
        """
        response = self._client.get(
            f"{self._base_url}/printer/objects/query",
            params={object: attribute},
        )
        response.raise_for_status()
        result = response.json()["result"]
        return result["status"][object][attribute]

    @functools.cache
    def get_config(self) -> dict[str, dict[str, Any]]:
        """プリンター設定（printer.cfg の内容）を取得する.

        結果はインスタンスごとにキャッシュする。
        printer.cfg を変更した後に読み直すには、Klipper を作り直す。

        Returns:
            プリンターの設定辞書
        """
        response = self._client.get(
            f"{self._base_url}/printer/objects/query",
            params={"configfile": "config"},
        )
        response.raise_for_status()
        result = response.json()["result"]
        return result["status"]["configfile"]["config"]

    @functools.cache
    def get_macros(self) -> dict[str, GCodeMacro]:
        """使用可能なマクロをすべて取得する.

        Returns:
            マクロ名をキー、GCodeMacro オブジェクトを値とする辞書
        """
        config = self.get_config()
        macros: dict[str, GCodeMacro] = {}
        for key, value in config.items():
            if key.startswith("gcode_macro "):
                macro_name = key.removeprefix("gcode_macro ")
                gcode = GCode(value.get("gcode", ""))
                description = value.get("description")
                variables = {
                    k.removeprefix("variable_"): v
                    for k, v in value.items()
                    if k.startswith("variable_")
                }
                macros[macro_name] = GCodeMacro(
                    gcode=gcode, description=description, variables=variables
                )
        return macros

    def emergency_stop(self) -> None:
        """緊急停止を実行する.

        Raises:
            RuntimeError: 緊急停止の実行に失敗した場合
        """
        response = self._client.post(f"{self._base_url}/printer/emergency_stop")
        if response.status_code >= 400:
            raise RuntimeError(response.reason_phrase)

    def firmware_restart(self) -> None:
        """ファームウェア再起動を実行する."""
        self.send_gcode(GCode.firmware_restart())

    def has_macro(self, name: str) -> bool:
        """指定した名前のマクロが存在するか確認する.

        Args:
            name: マクロ名

        Returns:
            マクロが存在すれば True、なければ False
        """
        return name in self.get_macros()

    def send_present_or_relax(
        self,
        *,
        warn: Callable[[str], None] | None = None,
        timeout: float = PRESENT_TIMEOUT,
    ) -> None:
        """PRESENT マクロがあれば実行し、無ければ警告して M84 にフォールバックする."""
        warning = warn if warn is not None else logger.warning
        try:
            has_present = self.has_macro(PRESENT_MACRO)
        except Exception as exc:
            warning(
                f"{PRESENT_MACRO} マクロ確認に失敗しました: {exc}。"
                "relax (M84) にフォールバックします"
            )
        else:
            if has_present:
                self.send_gcode(GCode.present(), timeout=timeout)
                return
            warning(
                f"{PRESENT_MACRO} マクロが見つかりません。"
                "relax (M84) にフォールバックします"
            )

        self.send_gcode(GCode.relax(), timeout=timeout)


class ReadonlyKlipper:
    """状態と設定の読み取りだけを公開する Klipper のビュー.

    ``send_gcode`` を持たないので、これを受け取った HAL クラスは装置を動かせない。
    """

    def __init__(self, klipper: Klipper) -> None:
        self.get_status = klipper.get_status
        self.get_config = klipper.get_config
        self.get_macros = klipper.get_macros
        self.has_macro = klipper.has_macro
