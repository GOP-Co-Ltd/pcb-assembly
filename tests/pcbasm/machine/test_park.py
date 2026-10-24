"""`pcbasm.machine.park` の仕様テスト.

`park` の分岐契約:

- キャップ記録済み → キャップへの退避移動（`XYZStage.travel_to`）+ M400 を **1 回の send_gcode** で送る。
  relax なら M84 も同じ送信に付け、しないならホーミング状態を保つ
- キャップ未記録・可動域外（ValueError）・printer.cfg の limits 設定不備（KeyError）→ 理由を warn して False。
  片付けの経路なので例外にしない
- 行けないとき relax なら PRESENT マクロ、無いか確かめられなければ warn + M84 に退避する。relax しないなら何も送らない

Klipper は FakeKlipper を使う（マクロ確認の失敗だけ MockTransport の Moonraker）。
"""

import httpx
import pytest

from pcbasm.gcode import GCode
from pcbasm.geometry import Point3d
from pcbasm.hal import KlipperClient, MoonrakerKlipper, XYZStage
from pcbasm.hal.fake import FakeKlipper, SentGCode
from pcbasm.machine.park import park

_CAP = Point3d(10.0, 20.0, 3.5)

_LIMITS_CONFIG = {
    "stepper_x": {"position_min": "0", "position_max": "100"},
    "stepper_y": {"position_min": "0", "position_max": "200"},
    "stepper_z": {"position_min": "0", "position_max": "50"},
    "printer": {"max_velocity": "300"},
}

_PRESENT_MACRO = {"gcode_macro PRESENT": {"gcode": "G1 Y300"}}

_TRAVEL = [
    "G90",
    "G1 Z0.0 F1200.0",
    "G1 X10.0 Y20.0 F1200.0",
    "G1 Z3.5 F1200.0",
    "M400",
]


def _park(
    klipper: KlipperClient, cap: Point3d | None, *, relax: bool
) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    parked = park(klipper, XYZStage(klipper), cap, relax=relax, warn=warnings.append)
    return parked, warnings


class TestPark:
    """`park` の分岐契約."""

    @pytest.mark.parametrize(
        ("relax", "lines"), [(True, [*_TRAVEL, "M84"]), (False, _TRAVEL)]
    )
    def test_recorded_cap_is_sent_in_single_send_gcode(
        self, relax: bool, lines: list[str]
    ):
        klipper = FakeKlipper(_LIMITS_CONFIG, timeout=12.5)

        parked, warnings = _park(klipper, _CAP, relax=relax)

        assert parked
        assert klipper.sent == (SentGCode(GCode(lines), 12.5),)
        assert warnings == []

    @pytest.mark.parametrize(
        ("cap", "config", "reason"),
        [
            (None, _LIMITS_CONFIG, "未記録"),
            (Point3d(999.0, 20.0, 3.5), _LIMITS_CONFIG, "制限外"),
            (_CAP, {}, "printer.cfg"),
        ],
        ids=["unrecorded", "out-of-limits", "missing-limits"],
    )
    def test_unreachable_cap_falls_back_to_present_when_relaxing(
        self, cap: Point3d | None, config: dict, reason: str
    ):
        klipper = FakeKlipper({**config, **_PRESENT_MACRO})

        parked, warnings = _park(klipper, cap, relax=True)

        assert not parked
        assert klipper.sent_lines == ["PRESENT"]
        assert reason in warnings[0]

    def test_unreachable_cap_sends_nothing_without_relaxing(self):
        """ホーミング状態を保つ駐機は、行けなければ動かずに理由だけを返す."""
        klipper = FakeKlipper({**_LIMITS_CONFIG, **_PRESENT_MACRO})

        parked, warnings = _park(klipper, None, relax=False)

        assert not parked
        assert klipper.sent == ()
        assert warnings == ["ノズルキャップ位置が未記録です"]

    def test_falls_back_to_relax_without_present_macro(self):
        klipper = FakeKlipper(_LIMITS_CONFIG)

        _, warnings = _park(klipper, None, relax=True)

        assert klipper.sent_lines == ["M84"]
        assert "PRESENT マクロが見つかりません" in warnings[-1]
        assert "M84" in warnings[-1]

    def test_falls_back_to_relax_when_macro_check_fails(self):
        scripts: list[str] = []

        def moonraker(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/printer/objects/query":
                return httpx.Response(503)
            scripts.append(request.url.params["script"])
            return httpx.Response(200, json={"result": "ok"})

        klipper = MoonrakerKlipper(
            "moonraker", 7125, transport=httpx.MockTransport(moonraker)
        )

        _, warnings = _park(klipper, None, relax=True)

        assert scripts == ["M84"]
        assert "確認に失敗しました" in warnings[-1]
        assert "503" in warnings[-1]
