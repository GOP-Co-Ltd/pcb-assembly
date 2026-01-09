import functools
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, Self

import attrs
import httpx


@attrs.frozen
class Macro:
    """Klipperマクロ情報を保持するクラス."""

    gcode: str
    description: str | None = None
    variables: dict[str, Any] = attrs.Factory(dict)


class GCodeBuffer:
    def __init__(self) -> None:
        self._commands: list[str] = []

    def add(self, gcode: str) -> None:
        self._commands.append(gcode)

    def build(self) -> str:
        return "\n".join(self._commands)


class Klipper:
    """Moonraker REST APIのシンプルなラッパー.

    Example:
        klipper = Klipper("192.168.1.100")
        with klipper.buffered():
            klipper.queue("G28")
            klipper.queue("M400")
        position = klipper.get_status("gcode_move", "gcode_position")
    """

    def __init__(self, host: str = "localhost", port: int = 7125) -> None:
        """Klipperクライアントを初期化する.

        Args:
            host: MoonrakerサーバーのホストIPアドレス
            port: Moonrakerサーバーのポート番号
        """
        self._base_url = f"http://{host}:{port}"
        self._client = httpx.Client(timeout=None)

    def _send_gcode(self, gcode: str) -> dict[str, Any]:
        """G-codeを送信する.

        Args:
            gcode: 送信するG-codeコマンド

        Returns:
            Moonrakerからの応答

        Raises:
            RuntimeError: G-codeの実行に失敗した場合
        """
        response = self._client.post(
            f"{self._base_url}/printer/gcode/script",
            params={"script": gcode},
        )
        if response.status_code >= 400:
            raise RuntimeError(response.reason_phrase)
        return response.json()

    _gcode_buffer: GCodeBuffer | None = None

    @contextmanager
    def buffered(self) -> Generator[Self]:
        """G-codeコマンドをバッファリングし、コンテキスト終了時にまとめて送信する.

        Example:
            with klipper.buffered():
                klipper.queue("G28")
                klipper.queue("M400")

        Raises:
            RuntimeError: すでにコンテキストに入っていた場合
        """
        if self._gcode_buffer is not None:
            raise RuntimeError("すでにbuffered()コンテキストに入っています。")
        self._gcode_buffer = GCodeBuffer()
        yield self
        gcode = self._gcode_buffer.build()
        if gcode:
            self._send_gcode(gcode)
        self._gcode_buffer = None

    def queue(self, gcode: str) -> None:
        """G-codeコマンドをバッファに追加する.

        Args:
            gcode: 追加するG-codeコマンド

        Raises:
            RuntimeError: buffered()コンテキスト外で呼び出された場合
        """
        if self._gcode_buffer is None:
            raise RuntimeError("queue()はbuffered()コンテキスト内で呼び出してください")
        self._gcode_buffer.add(gcode)

    def get_status(self, object: str, attribute: str) -> Any:
        """指定したオブジェクトの属性値を取得する.

        Args:
            object: オブジェクト名（gcode_move, toolhead等）
            attribute: 属性名（gcode_position, homed_axes等）

        Returns:
            属性の値
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
        """プリンター設定を取得する.

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
    def get_macros(self) -> dict[str, Macro]:
        """使用可能なマクロをすべて取得する.

        Returns:
            マクロ名をキー、Macroオブジェクトを値とする辞書
        """
        config = self.get_config()
        macros: dict[str, Macro] = {}
        for key, value in config.items():
            if key.startswith("gcode_macro "):
                macro_name = key.removeprefix("gcode_macro ")
                gcode = value.get("gcode", "")
                description = value.get("description")
                variables = {
                    k.removeprefix("variable_"): v
                    for k, v in value.items()
                    if k.startswith("variable_")
                }
                macros[macro_name] = Macro(
                    gcode=gcode, description=description, variables=variables
                )
        return macros

    def has_macro(self, name: str) -> bool:
        """指定した名前のマクロが存在するか確認する.

        Args:
            name: マクロ名

        Returns:
            マクロが存在すればTrue、なければFalse
        """
        return name in self.get_macros()
