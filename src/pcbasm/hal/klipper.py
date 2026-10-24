"""Klipper との通信口.

HAL の他のクラス（``XYZStage`` / ``ManualStepper`` / ``AirPump`` /
``PasteDispenser``）は G-code を生成するだけで送信しない。
生成した G-code を装置へ送るのは :meth:`KlipperClient.send_gcode` だけである。
実装は Moonraker へ送る :class:`MoonrakerKlipper` と、``pcbasm.hal.fake.FakeKlipper`` の 2 つに限る。
"""

from __future__ import annotations

import functools
from typing import Any, Protocol

import httpx

from pcbasm.errors import DeviceError
from pcbasm.gcode import GCode, GCodeLike


class KlipperError(DeviceError):
    """Klipper が要求を断った（Moonraker の HTTP 400 以上。G-code エラー・Klipper 停止中など）."""


class GCodeJournal(Protocol):
    """送信ブロックの書き先（実装は ``pcbasm.machine.journal.Journal``）."""

    def write(self, gcode: GCode, timeout: float | None) -> None: ...


class KlipperClient(Protocol):
    """Klipper への G-code 送信と、状態・printer.cfg の読み取り."""

    def send_gcode(
        self, gcode: GCodeLike, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """G-code を送信し、Klipper がスクリプトを処理し終えるまで待つ.

        処理済みでも移動が物理的に終わったとは限らない。
        到達を待つときは末尾に ``GCode.wait_for_done()``（M400）を付ける。

        Args:
            gcode: 送信する G-code
            timeout: この送信だけに適用するタイムアウト秒数（None は接続の既定値）

        Raises:
            KlipperError: Klipper が送信を断った場合
        """
        ...

    def get_status(self, object: str, attribute: str) -> Any:
        """オブジェクト（gcode_move, toolhead 等）の属性値を返す."""
        ...

    def get_config(self) -> dict[str, dict[str, Any]]:
        """printer.cfg の内容（セクション名 → キーと値）を返す."""
        ...

    def emergency_stop(self) -> None:
        """緊急停止を実行する.

        Raises:
            KlipperError: Klipper が要求を断った場合
        """
        ...


class MoonrakerKlipper:
    """Moonraker REST API で Klipper と通信する :class:`KlipperClient`.

    接続できない・タイムアウトした場合は httpx.HTTPError を包まずに送出する。

    Example:
        klipper = hal.open_klipper(machine.klipper.host, machine.klipper.port)
        klipper.send_gcode(GCode.homing(x=True, y=True, z=True) + GCode.wait_for_done())
        position = klipper.get_status("gcode_move", "gcode_position")
    """

    def __init__(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        journal: GCodeJournal | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Moonraker への接続を用意する（ここでは通信しない）.

        Args:
            host: Moonraker サーバーのホスト IP アドレス
            port: Moonraker サーバーのポート番号
            timeout: HTTP リクエストのタイムアウト秒数（None は無制限）
            journal: 送信ブロックを書くジャーナル（None は書かない）
            transport: HTTP の送り先（None は実ネットワーク。テストは httpx.MockTransport を渡す）
        """
        self._base_url = f"http://{host}:{port}"
        self._timeout = timeout
        self._journal = journal
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def __del__(self) -> None:
        """破棄時に httpx クライアントを閉じる."""
        if hasattr(self, "_client"):
            self._client.close()

    def send_gcode(
        self, gcode: GCodeLike, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """G-code を送信し、Klipper がスクリプトを処理し終えるまで待つ.

        ジャーナルには送る前に書くので、届かなかった送信も残る。

        Returns:
            Moonraker からの応答
        """
        script = GCode(gcode)
        if self._journal is not None:
            self._journal.write(script, self._timeout if timeout is None else timeout)
        url = f"{self._base_url}/printer/gcode/script"
        params = {"script": str(script)}
        if timeout is None:
            response = self._client.post(url, params=params)
        else:
            response = self._client.post(url, params=params, timeout=timeout)
        _raise_for_klipper(response)
        return response.json()

    def get_status(self, object: str, attribute: str) -> Any:
        """オブジェクトの属性値を返す（HTTP エラー応答も httpx.HTTPError で送出する）."""
        response = self._client.get(
            f"{self._base_url}/printer/objects/query",
            params={object: attribute},
        )
        response.raise_for_status()
        result = response.json()["result"]
        return result["status"][object][attribute]

    @functools.cache
    def get_config(self) -> dict[str, dict[str, Any]]:
        """printer.cfg の内容を返す.

        読み直すにはクライアントを作り直す（結果はインスタンスごとにキャッシュする）。
        """
        response = self._client.get(
            f"{self._base_url}/printer/objects/query",
            params={"configfile": "config"},
        )
        response.raise_for_status()
        result = response.json()["result"]
        return result["status"]["configfile"]["config"]

    def emergency_stop(self) -> None:
        """緊急停止を実行する."""
        response = self._client.post(f"{self._base_url}/printer/emergency_stop")
        _raise_for_klipper(response)


def _raise_for_klipper(response: httpx.Response) -> None:
    if response.status_code >= 400:
        raise KlipperError(response.reason_phrase)


def open_klipper(
    host: str,
    port: int,
    *,
    timeout: float | None = None,
    journal: GCodeJournal | None = None,
) -> KlipperClient:
    """Moonraker の接続先へ Klipper クライアントを作る.

    Klipper を作る唯一の口で、テストはこれを差し替えて実機への接続を断つ。
    呼び出し側は ``hal.open_klipper`` とモジュール経由で呼ぶ。

    Args:
        host: Moonraker サーバーのホスト
        port: Moonraker サーバーのポート番号
        timeout: HTTP リクエストのタイムアウト秒数（None は無制限）
        journal: 送信ブロックを書くジャーナル（None は書かない）
    """
    return MoonrakerKlipper(host, port, timeout=timeout, journal=journal)
