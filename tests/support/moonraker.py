"""``httpx.MockTransport`` で応える Moonraker.

起動の遅れ、Mainsail での printer.cfg の書き換え、終わらない長い送信を模擬する。
"""

import copy
import threading
from typing import Any

import httpx

from pcbasm.hal.fake import FAKE_PRINTER_CONFIG


class FakeMoonraker:
    """``MoonrakerKlipper`` の要求に応える Moonraker.

    Attributes:
        up: False の間は接続を断る（電源投入直後で、まだ起動していない）
        config: printer.cfg の内容（書き換えると次の問い合わせから見える）
        released: clear している間、G-code の送信は応答を返さずに待つ（長い送信の途中）
        requests: 受けた要求（断った要求も含む）
    """

    def __init__(self) -> None:
        self.up = True
        self.config: dict[str, dict[str, Any]] = copy.deepcopy(FAKE_PRINTER_CONFIG)
        self.released = threading.Event()
        self.released.set()
        self.requests: list[httpx.Request] = []
        self._status: dict[str, dict[str, Any]] = {
            "gcode_move": {"gcode_position": [0.0, 0.0, 0.0, 0.0]},
            "toolhead": {"homed_axes": ""},
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.up:
            raise httpx.ConnectError("Connection refused", request=request)
        if request.url.path == "/printer/objects/query":
            [(name, attribute)] = request.url.params.items()
            value = (
                self.config if name == "configfile" else self._status[name][attribute]
            )
            status = {name: {attribute: value}}
            return httpx.Response(200, json={"result": {"status": status}})
        if request.url.path == "/printer/gcode/script":
            self.released.wait()
        return httpx.Response(200, json={"result": "ok"})
