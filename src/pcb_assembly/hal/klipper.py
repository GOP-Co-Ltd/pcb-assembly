from typing import Any

import attrs
import httpx


@attrs.frozen
class Macro:
    """Klipperマクロ情報を保持するクラス."""

    gcode: str
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

    def __init__(self, host: str = "localhost", port: int = 7125) -> None:
        """Klipperクライアントを初期化する.

        Args:
            host: MoonrakerサーバーのホストIPアドレス
            port: Moonrakerサーバーのポート番号
        """
        self._base_url = f"http://{host}:{port}"
        self._client = httpx.Client(timeout=None)

    def send_gcode(self, gcode: str) -> dict[str, Any]:
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

    def wait_for_move(self) -> None:
        """全ての動作が完了するまで待機する."""
        self.send_gcode("M400")

    def home(self) -> None:
        """全軸ホーミング."""
        self.send_gcode("G28")
        self.wait_for_move()

    def relax(self) -> None:
        """全軸のモーターをリラックス（脱力）させる."""
        self.send_gcode("M18")

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

    def is_homed(self, axis: str = "xyz") -> bool:
        """指定した軸がホーミング済みかを確認する.

        Args:
            axis: 確認する軸（例: "x", "xy", "xyz"）

        Returns:
            指定した全ての軸がホーミング済みならTrue
        """
        homed_axes: str = self.get_status("toolhead", "homed_axes")
        return all(a in homed_axes for a in axis.lower())

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
