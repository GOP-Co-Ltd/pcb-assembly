"""`pcbasm.hal.gamepad` の仕様テスト.

Xbox コントローラの LT・LB・RB と、左スティック・右スティックの縦を evdev から読む。`EvdevGamepad` は `/dev/uinput` で作った仮想デバイスを、
モジュールの実物 `pcbasm.hal.gamepad.open_gamepad` で開いて確かめる（`pcbasm.hal.open_gamepad` は root conftest
が塞ぐ）。ドライバごとの割り当ての判別と LT・スティックの正規化も、軸を変えた仮想デバイスで確かめる。
スティックは静止（中点）がちょうど 0 になるが、中点の両側を幅（32767 など）で割るので、途中の値は 0.5 ちょうどなどにならない。比べるときは `_rounded` で丸める。
CI の runner は専用の `uinput` group で `/dev/uinput` に書け、job の container には `ci/run.sh` がデバイスと group を
渡す（`github-runner/README.md`）。書けない環境
（既定の権限の Raspberry Pi OS）では、仮想デバイスを使うテストを skip する。
パスを渡さない自動検出は実機のコントローラを開きうるので、ここでは確かめない（受け入れ試験 A26 が持つ）。
"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import attrs
import pytest
from evdev import AbsInfo, UInput, UInputError, ecodes

from pcbasm.hal import EvdevGamepad, GamepadError, GamepadState
from pcbasm.hal.gamepad import open_gamepad
from tests.support.polling import wait_until


def _abs(minimum: int, maximum: int, value: int | None = None) -> AbsInfo:
    """軸の範囲（value を省けば min。スティックは中立の値を渡す）."""
    return AbsInfo(
        value=minimum if value is None else value,
        min=minimum,
        max=maximum,
        fuzz=0,
        flat=0,
        resolution=0,
    )


STICK = _abs(-32768, 32767, 0)  # xpad のスティック（中立は 0）
BT_STICK = _abs(0, 65535, 32768)  # hid-microsoft のスティック（中立は 32768）
TRIGGER = _abs(0, 1023)
BUMPERS = [ecodes.BTN_TL, ecodes.BTN_TR]
# LT と LB・RB に加えて、最小限のスティック（左の X・Y と右の Y）を持つ軸
STICKS = [(ecodes.ABS_X, STICK), (ecodes.ABS_Y, STICK), (ecodes.ABS_RY, STICK)]

# USB の xpad（Xbox Series S|X を実機で確認）: LT が ABS_Z、RT が ABS_RZ
XPAD = {
    ecodes.EV_KEY: [ecodes.BTN_SOUTH, ecodes.BTN_EAST, *BUMPERS],
    ecodes.EV_ABS: [
        (ecodes.ABS_X, STICK),
        (ecodes.ABS_Y, STICK),
        (ecodes.ABS_Z, TRIGGER),
        (ecodes.ABS_RX, STICK),
        (ecodes.ABS_RY, STICK),
        (ecodes.ABS_RZ, TRIGGER),
    ],
}
# Bluetooth の hid-microsoft: 右スティックが ABS_Z / ABS_RZ、LT が ABS_BRAKE、RT が ABS_GAS
BLUETOOTH = {
    ecodes.EV_KEY: [ecodes.BTN_SOUTH, ecodes.BTN_EAST, *BUMPERS],
    ecodes.EV_ABS: [
        (ecodes.ABS_X, BT_STICK),
        (ecodes.ABS_Y, BT_STICK),
        (ecodes.ABS_Z, BT_STICK),
        (ecodes.ABS_RZ, BT_STICK),
        (ecodes.ABS_GAS, TRIGGER),
        (ecodes.ABS_BRAKE, TRIGGER),
    ],
}


type MakeDevice = Callable[[dict[int, Any]], UInput]
type OpenGamepad = Callable[[UInput], EvdevGamepad]


def _uinput(events: dict[int, Any]) -> UInput:
    """仮想の入力デバイスを作る（/dev/uinput に書けなければ skip）.

    events の軸は ``(code, AbsInfo)`` で渡す（evdev の型注釈は int の列だが、実装は軸の組を受け付ける）。
    """
    try:
        device = UInput(events, name="pcbasm-test-gamepad")
    except (OSError, UInputError) as exc:
        pytest.skip(f"/dev/uinput で仮想デバイスを作れません: {exc}")
    if device.device is None:
        # 作れても event ノードを読めなければ evdev は開けない（CI は github-runner/setup.sh の udev 規則が読ませる）
        device.close()
        pytest.skip("作った仮想デバイスの /dev/input/event* を読めません")
    return device


@pytest.fixture
def make_device() -> Iterator[MakeDevice]:
    """仮想の入力デバイスを作る関数（テストの終わりに消す）."""
    devices: list[UInput] = []

    def make(events: dict[int, Any]) -> UInput:
        device = _uinput(events)
        devices.append(device)
        return device

    yield make
    for device in devices:
        device.close()


@pytest.fixture
def pad(make_device: MakeDevice) -> UInput:
    """USB の xpad と同じ割り当ての仮想ゲームパッド（LT は ABS_Z、0〜1023）."""
    return make_device(XPAD)


@pytest.fixture
def opened() -> Iterator[OpenGamepad]:
    """仮想デバイスを open_gamepad で開く関数（テストの終わりに閉じる）."""
    gamepads: list[EvdevGamepad] = []

    def open_(device: UInput) -> EvdevGamepad:
        gamepad = open_gamepad(os.fsdecode(device.device.path))
        gamepads.append(gamepad)
        return gamepad

    yield open_
    for gamepad in gamepads:
        gamepad.close()


def _rounded(state: GamepadState) -> GamepadState:
    """スティックを小数 3 桁に丸める（幅で割ることで生じる 1e-5 程度のずれを消す）."""
    return attrs.evolve(
        state,
        left_x=round(state.left_x, 3),
        left_y=round(state.left_y, 3),
        right_y=round(state.right_y, 3),
    )


def _send(device: UInput, *events: tuple[int, int, int]) -> None:
    for event_type, code, value in events:
        device.write(event_type, code, value)
    device.syn()


def _press_left_bumper_and_wait(device: UInput, gamepad: EvdevGamepad) -> None:
    """LB を押し、それが読めるまで待つ（その前に送ったイベントも読み終えている）."""
    _send(device, (ecodes.EV_KEY, ecodes.BTN_TL, 1))
    wait_until(lambda: gamepad.state().left_bumper)


class TestEvdevGamepad:
    @pytest.mark.parametrize("layout", ["usb", "bluetooth"])
    def test_is_exactly_at_rest_when_nothing_is_pressed(
        self, make_device: MakeDevice, opened: OpenGamepad, layout: str
    ):
        # スティックの中立（xpad は 0、hid-microsoft は 32768）はちょうど 0 になる（デッドゾーンが 0 でも漂わない）
        device = make_device(XPAD if layout == "usb" else BLUETOOTH)

        assert opened(device).state() == GamepadState()

    def test_reads_lt_and_bumpers_pressed_after_opening(
        self, pad: UInput, opened: OpenGamepad
    ):
        gamepad = opened(pad)
        # 0〜1023 の 1/3 は 341（ちょうど割り切れる）
        expected = GamepadState(left_trigger=1 / 3, left_bumper=True, right_bumper=True)

        _send(
            pad,
            (ecodes.EV_ABS, ecodes.ABS_Z, 341),
            (ecodes.EV_KEY, ecodes.BTN_TL, 1),
            (ecodes.EV_KEY, ecodes.BTN_TR, 1),
        )

        wait_until(lambda: _rounded(gamepad.state()) == expected)

    def test_bumpers_follow_release(self, pad: UInput, opened: OpenGamepad):
        gamepad = opened(pad)
        _send(pad, (ecodes.EV_KEY, ecodes.BTN_TL, 1), (ecodes.EV_KEY, ecodes.BTN_TR, 1))
        wait_until(lambda: gamepad.state().right_bumper)

        _send(pad, (ecodes.EV_KEY, ecodes.BTN_TL, 0))

        wait_until(lambda: _rounded(gamepad.state()) == GamepadState(right_bumper=True))

    def test_reads_what_is_held_when_opened(self, pad: UInput, opened: OpenGamepad):
        _send(
            pad, (ecodes.EV_ABS, ecodes.ABS_Z, 1023), (ecodes.EV_KEY, ecodes.BTN_TR, 1)
        )

        gamepad = opened(pad)

        assert _rounded(gamepad.state()) == GamepadState(
            left_trigger=1.0, right_bumper=True
        )

    def test_returns_the_latest_even_after_the_kernel_drops_events(
        self, pad: UInput, opened: OpenGamepad
    ):
        gamepad = opened(pad)

        # 読まずに 128 フレーム送り、カーネルの溜まり（数十イベント）をあふれさせて SYN_DROPPED を起こす
        for raw in range(0, 1024, 8):
            _send(pad, (ecodes.EV_ABS, ecodes.ABS_Z, raw))
        _send(pad, (ecodes.EV_ABS, ecodes.ABS_Z, 0), (ecodes.EV_KEY, ecodes.BTN_TL, 1))

        wait_until(lambda: _rounded(gamepad.state()) == GamepadState(left_bumper=True))

    def test_rereads_the_sticks_after_the_kernel_drops_events(
        self, pad: UInput, opened: OpenGamepad
    ):
        gamepad = opened(pad)

        for raw in range(-32768, 32767, 512):
            _send(pad, (ecodes.EV_ABS, ecodes.ABS_X, raw))
        _send(
            pad,
            (ecodes.EV_ABS, ecodes.ABS_X, 32767),
            (ecodes.EV_ABS, ecodes.ABS_RY, -32768),
            (ecodes.EV_KEY, ecodes.BTN_TL, 1),
        )

        wait_until(
            lambda: _rounded(gamepad.state())
            == GamepadState(left_bumper=True, left_x=1.0, right_y=1.0)
        )

    @pytest.mark.parametrize(
        ("maximum", "raw", "expected"),
        [
            (1023, 1023, 1.0),  # Xbox One / Series（xpad、Bluetooth）
            (255, 51, 0.2),  # Xbox 360（xpad）
            (255, 255, 1.0),
            (255, 300, 1.0),  # 範囲外は収める
        ],
    )
    def test_normalizes_lt_by_the_axis_range(
        self,
        make_device: MakeDevice,
        opened: OpenGamepad,
        maximum: int,
        raw: int,
        expected: float,
    ):
        device = make_device(
            {
                ecodes.EV_KEY: BUMPERS,
                ecodes.EV_ABS: [(ecodes.ABS_Z, _abs(0, maximum)), *STICKS],
            }
        )
        gamepad = opened(device)

        _send(device, (ecodes.EV_ABS, ecodes.ABS_Z, raw))
        _press_left_bumper_and_wait(device, gamepad)

        assert gamepad.state().left_trigger == pytest.approx(expected)

    @pytest.mark.parametrize(
        "event",
        [
            (ecodes.EV_ABS, ecodes.ABS_RX, 30000),  # 右スティックの横
            (ecodes.EV_ABS, ecodes.ABS_RZ, 1023),  # RT
            (ecodes.EV_KEY, ecodes.BTN_SOUTH, 1),  # A
        ],
        ids=["right-stick-x", "right-trigger", "a-button"],
    )
    def test_ignores_other_inputs(
        self, pad: UInput, opened: OpenGamepad, event: tuple[int, int, int]
    ):
        gamepad = opened(pad)

        _send(pad, event)
        _press_left_bumper_and_wait(pad, gamepad)

        assert _rounded(gamepad.state()) == GamepadState(left_bumper=True)

    def test_reads_lt_from_abs_brake_on_bluetooth_where_abs_z_is_the_right_stick(
        self, make_device: MakeDevice, opened: OpenGamepad
    ):
        device = make_device(BLUETOOTH)
        gamepad = opened(device)

        _send(
            device,
            (ecodes.EV_ABS, ecodes.ABS_Z, 65535),  # 右スティック
            (ecodes.EV_ABS, ecodes.ABS_BRAKE, 1023),
        )
        _press_left_bumper_and_wait(device, gamepad)

        assert _rounded(gamepad.state()) == GamepadState(
            left_trigger=1.0, left_bumper=True
        )

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ((ecodes.EV_ABS, ecodes.ABS_X, 32767), GamepadState(left_x=1.0)),
            ((ecodes.EV_ABS, ecodes.ABS_X, -32768), GamepadState(left_x=-1.0)),
            ((ecodes.EV_ABS, ecodes.ABS_Y, -32768), GamepadState(left_y=1.0)),
            ((ecodes.EV_ABS, ecodes.ABS_Y, 16384), GamepadState(left_y=-0.5)),
            ((ecodes.EV_ABS, ecodes.ABS_RY, -32768), GamepadState(right_y=1.0)),
            ((ecodes.EV_ABS, ecodes.ABS_RY, 32767), GamepadState(right_y=-1.0)),
        ],
        ids=[
            "left-right",
            "left-left",
            "left-up",
            "left-half-down",
            "right-up",
            "right-down",
        ],
    )
    def test_reads_sticks_with_right_and_up_positive(
        self,
        pad: UInput,
        opened: OpenGamepad,
        event: tuple[int, int, int],
        expected: GamepadState,
    ):
        # evdev の Y は下が +。GamepadState では上を + にそろえる
        gamepad = opened(pad)

        _send(pad, event)
        _press_left_bumper_and_wait(pad, gamepad)

        assert _rounded(gamepad.state()) == attrs.evolve(expected, left_bumper=True)

    def test_reads_the_right_stick_y_from_abs_rz_on_bluetooth(
        self, make_device: MakeDevice, opened: OpenGamepad
    ):
        device = make_device(BLUETOOTH)
        gamepad = opened(device)

        _send(
            device,
            (ecodes.EV_ABS, ecodes.ABS_X, 49151),  # 右へ半分
            (ecodes.EV_ABS, ecodes.ABS_Y, 0),  # 上へ倒し切る
            (ecodes.EV_ABS, ecodes.ABS_RZ, 0),  # 右スティックを上へ倒し切る
        )
        _press_left_bumper_and_wait(device, gamepad)

        assert _rounded(gamepad.state()) == GamepadState(
            left_bumper=True, left_x=0.5, left_y=1.0, right_y=1.0
        )

    def test_reads_sticks_held_when_opened(self, pad: UInput, opened: OpenGamepad):
        _send(
            pad,
            (ecodes.EV_ABS, ecodes.ABS_X, -32768),
            (ecodes.EV_ABS, ecodes.ABS_RY, 32767),
        )

        gamepad = opened(pad)

        assert _rounded(gamepad.state()) == GamepadState(left_x=-1.0, right_y=-1.0)

    @pytest.mark.parametrize(
        "events",
        [
            {ecodes.EV_KEY: BUMPERS, ecodes.EV_ABS: STICKS},
            {
                ecodes.EV_KEY: [ecodes.BTN_TL],
                ecodes.EV_ABS: [(ecodes.ABS_Z, TRIGGER), *STICKS],
            },
            {ecodes.EV_KEY: [ecodes.KEY_A, ecodes.KEY_B]},
            {ecodes.EV_KEY: BUMPERS, ecodes.EV_ABS: [(ecodes.ABS_Z, TRIGGER)]},
            {
                ecodes.EV_KEY: BUMPERS,
                ecodes.EV_ABS: [
                    (ecodes.ABS_Z, TRIGGER),
                    (ecodes.ABS_X, STICK),
                    (ecodes.ABS_Y, STICK),
                    (ecodes.ABS_RZ, TRIGGER),
                ],
            },
            {
                ecodes.EV_KEY: BUMPERS,
                ecodes.EV_ABS: [
                    (ecodes.ABS_BRAKE, TRIGGER),
                    (ecodes.ABS_X, BT_STICK),
                    (ecodes.ABS_Y, BT_STICK),
                    (ecodes.ABS_RY, BT_STICK),
                ],
            },
        ],
        ids=[
            "no-trigger",
            "no-right-bumper",
            "keyboard",
            "no-stick",
            "xpad-without-right-stick-y",
            "bluetooth-without-abs-rz",
        ],
    )
    def test_refuses_an_input_device_without_lt_bumpers_and_sticks(
        self, make_device: MakeDevice, events: dict[int, Any]
    ):
        device = make_device(events)

        with pytest.raises(GamepadError, match="LT"):
            open_gamepad(os.fsdecode(device.device.path))

    def test_disconnect_raises(self, pad: UInput, opened: OpenGamepad):
        gamepad = opened(pad)

        pad.close()

        with pytest.raises(GamepadError, match="切断"):
            gamepad.state()

    def test_state_after_close_says_it_is_closed(
        self, pad: UInput, opened: OpenGamepad
    ):
        gamepad = opened(pad)

        gamepad.close()

        with pytest.raises(GamepadError, match="閉じています"):
            gamepad.state()


class TestOpenGamepad:
    def test_refuses_a_missing_path(self, tmp_path: Path):
        with pytest.raises(GamepadError, match="開けません"):
            open_gamepad(str(tmp_path / "event99"))

    def test_refuses_a_file_that_is_not_an_input_device(self, tmp_path: Path):
        path = tmp_path / "event0"
        path.write_bytes(b"")

        with pytest.raises(GamepadError, match="開けません"):
            open_gamepad(str(path))
