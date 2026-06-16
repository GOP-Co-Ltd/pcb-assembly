from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

import attrs
import httpx

from pcbasm import gcode
from pcbasm.gcode import GCode, GCodeLike

logger = logging.getLogger(__name__)

PRESENT_MACRO = "PRESENT"


@attrs.frozen
class GCodeMacro:
    """Klipperマクロ情報を保持するクラス."""

    gcode: GCode
    description: str | None = None
    variables: dict[str, Any] = attrs.Factory(dict)


class Klipper:
    """Moonraker REST APIのシンプルなラッパー.

    Example:
        klipper = Klipper("192.168.1.100")
        klipper.send_gcode("G28 X Y Z")
        klipper.wait_for_move()
        position = klipper.get_status("gcode_move", "gcode_position")
    """

    def __init__(
        self, host: str = "localhost", port: int = 7125, timeout: float | None = None
    ) -> None:
        """Klipperクライアントを初期化する.

        Args:
            host: MoonrakerサーバーのホストIPアドレス
            port: Moonrakerサーバーのポート番号
            timeout: HTTPリクエストのタイムアウト秒数（Noneは無制限）
        """
        self._base_url = f"http://{host}:{port}"
        self._client = httpx.Client(timeout=timeout)
        self._readonly = ReadonlyKlipper(self)

    def __del__(self) -> None:
        """破棄される際にhttpxクライアントを破棄."""
        if hasattr(self, "_client"):
            self._client.close()

    @property
    def readonly(self) -> ReadonlyKlipper:
        return self._readonly

    def send_gcode(self, gcode: GCodeLike) -> dict[str, Any]:
        """G-codeを送信する.

        Args:
            gcode: 送信するG-codeコマンド（文字列、Iterable、またはGCodeオブジェクト）

        Returns:
            Moonrakerからの応答

        Raises:
            RuntimeError: G-codeの実行に失敗した場合
        """
        response = self._client.post(
            f"{self._base_url}/printer/gcode/script",
            params={"script": str(GCode(gcode))},
        )
        if response.status_code >= 400:
            raise RuntimeError(response.reason_phrase)
        return response.json()

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
    def get_macros(self) -> dict[str, GCodeMacro]:
        """使用可能なマクロをすべて取得する.

        Returns:
            マクロ名をキー、GCodeMacroオブジェクトを値とする辞書
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

    def has_macro(self, name: str) -> bool:
        """指定した名前のマクロが存在するか確認する.

        Args:
            name: マクロ名

        Returns:
            マクロが存在すればTrue、なければFalse
        """
        return name in self.get_macros()


def send_present_or_relax(
    klipper: Klipper, *, warn: Callable[[str], None] | None = None
) -> None:
    """PRESENTマクロがあれば実行し、無ければ警告してM84にフォールバックする."""
    warning = warn if warn is not None else logger.warning
    try:
        has_present = klipper.has_macro(PRESENT_MACRO)
    except Exception as exc:
        warning(
            f"{PRESENT_MACRO} マクロ確認に失敗しました: {exc}。"
            "relax (M84) にフォールバックします"
        )
    else:
        if has_present:
            klipper.send_gcode(gcode.present())
            return
        warning(
            f"{PRESENT_MACRO} マクロが見つかりません。"
            "relax (M84) にフォールバックします"
        )

    klipper.send_gcode(gcode.relax())


class ReadonlyKlipper:
    def __init__(self, klipper: Klipper) -> None:
        self.get_status = klipper.get_status
        self.get_config = klipper.get_config
        self.get_macros = klipper.get_macros
        self.has_macro = klipper.has_macro
