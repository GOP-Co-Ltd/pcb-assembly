"""`pcbasm.hal.klipper` の仕様テスト.

Moonraker のプロトコルは httpx.MockTransport で固定する。

接続先と timeout の受け渡しは、応答しない実ソケットで確かめる。
"""

import socket
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from pcbasm.gcode import GCode
from pcbasm.hal.fake import FakeKlipper
from pcbasm.hal.klipper import KlipperError, MoonrakerKlipper, open_klipper
from pcbasm.machine.journal import Journal, format_block


def _moonraker(handler: Callable[[httpx.Request], httpx.Response]) -> MoonrakerKlipper:
    return MoonrakerKlipper("moonraker", 7125, transport=httpx.MockTransport(handler))


class TestMoonrakerKlipper:
    """Moonraker REST API との対応."""

    def test_send_gcode_posts_script_and_returns_response(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"result": "ok"})

        response = _moonraker(handler).send_gcode(["G28", "M400"])

        assert response == {"result": "ok"}
        (request,) = requests
        assert request.method == "POST"
        assert request.url.path == "/printer/gcode/script"
        assert request.url.params["script"] == "G28\nM400"

    def test_send_gcode_applies_per_send_timeout(self):
        timeouts: list[float | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            timeouts.append(request.extensions["timeout"]["read"])
            return httpx.Response(200, json={"result": "ok"})

        klipper = MoonrakerKlipper(
            "moonraker", 7125, timeout=60.0, transport=httpx.MockTransport(handler)
        )
        klipper.send_gcode("M400")
        klipper.send_gcode("M400", timeout=5.0)

        assert timeouts == [60.0, 5.0]

    @pytest.mark.parametrize(
        "call",
        [
            lambda klipper: klipper.send_gcode("G1 X1000"),
            lambda klipper: klipper.emergency_stop(),
        ],
        ids=["send_gcode", "emergency_stop"],
    )
    def test_refusal_raises_klipper_error_caught_as_runtime_error(
        self, call: Callable[[MoonrakerKlipper], object]
    ):
        klipper = _moonraker(lambda request: httpx.Response(400))

        with pytest.raises(KlipperError, match="Bad Request") as exc:
            call(klipper)

        assert isinstance(exc.value, RuntimeError)

    def test_emergency_stop_posts_to_emergency_stop_endpoint(self):
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(f"{request.method} {request.url.path}")
            return httpx.Response(200, json={"result": "ok"})

        _moonraker(handler).emergency_stop()

        assert paths == ["POST /printer/emergency_stop"]

    def test_get_status_queries_object_attribute(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/printer/objects/query"
            assert dict(request.url.params) == {"toolhead": "homed_axes"}
            status = {"toolhead": {"homed_axes": "xy"}}
            return httpx.Response(200, json={"result": {"status": status}})

        assert _moonraker(handler).get_status("toolhead", "homed_axes") == "xy"

    def test_get_config_returns_printer_cfg(self):
        config = {"stepper_x": {"position_max": "300"}}
        queries: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            queries.append(dict(request.url.params))
            status = {"configfile": {"config": config}}
            return httpx.Response(200, json={"result": {"status": status}})

        assert _moonraker(handler).get_config() == config
        assert queries == [{"configfile": "config"}]

    def test_get_config_reads_printer_cfg_once_per_connection(self):
        """Mainsail で printer.cfg を書き換えると、次に開いた接続から見える."""
        configs = [{"stepper_x": {"position_max": "300"}}, {"stepper_x": {}}]

        def handler(request: httpx.Request) -> httpx.Response:
            status = {"configfile": {"config": configs.pop(0)}}
            return httpx.Response(200, json={"result": {"status": status}})

        klipper = _moonraker(handler)
        connection = klipper.connection()

        assert connection.get_config() == {"stepper_x": {"position_max": "300"}}
        assert connection.get_config() == {"stepper_x": {"position_max": "300"}}
        assert klipper.connection().get_config() == {"stepper_x": {}}

    def test_connection_replaces_only_timeout_and_journal(self, tmp_path: Path):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            status = {"toolhead": {"homed_axes": "xyz"}}
            return httpx.Response(200, json={"result": {"status": status}})

        klipper = _moonraker(handler)
        journal = Journal(tmp_path / "journal.gcode")
        connection = klipper.connection(timeout=10.0, journal=journal)

        connection.send_gcode("M400")
        connection.get_status("toolhead", "homed_axes")
        klipper.send_gcode("G28")

        assert [
            (request.url.host, request.extensions["timeout"]["read"])
            for request in requests
        ] == [("moonraker", 10.0), ("moonraker", 10.0), ("moonraker", None)]
        assert journal.path.read_text(encoding="utf-8") == format_block(
            GCode("M400"), 10.0
        )

    def test_query_error_response_raises_http_error(self):
        klipper = _moonraker(lambda request: httpx.Response(503))

        with pytest.raises(httpx.HTTPStatusError):
            klipper.get_status("toolhead", "homed_axes")


class TestOpenKlipper:
    """open_klipper は接続先と timeout をそのまま写す.

    root conftest が差し替えるのは ``pcbasm.hal.open_klipper`` なので、
    ``pcbasm.hal.klipper`` から import した実物を検査できる。
    """

    @pytest.mark.timeout(10)
    def test_connects_to_configured_address_with_timeout(self):
        # 待ち受けるだけで応答しない相手。接続先が違えば ConnectError、timeout が無ければ待ち続ける
        with socket.create_server(("127.0.0.1", 0)) as server:
            klipper = open_klipper("127.0.0.1", server.getsockname()[1], timeout=0.2)

            with pytest.raises(httpx.ReadTimeout):
                klipper.send_gcode("M115")

    @pytest.mark.timeout(10)
    def test_journals_each_send_as_fake_klipper_records_it(self, tmp_path: Path):
        # 応答しない相手なので送信は失敗するが、送ろうとしたブロックはジャーナルに残る
        journal = Journal(tmp_path / "journal.gcode")
        with socket.create_server(("127.0.0.1", 0)) as server:
            klipper = open_klipper("127.0.0.1", server.getsockname()[1]).connection(
                timeout=0.2, journal=journal
            )
            with pytest.raises(httpx.ReadTimeout):
                klipper.send_gcode("G28\nM400")
            with pytest.raises(httpx.ReadTimeout):
                klipper.send_gcode(GCode.relax(), timeout=0.1)
        fake = FakeKlipper(timeout=0.2)
        fake.send_gcode("G28\nM400")
        fake.send_gcode(GCode.relax(), timeout=0.1)

        assert journal.path.read_text(encoding="utf-8") == "".join(
            format_block(sent.gcode, sent.timeout) for sent in fake.sent
        )
