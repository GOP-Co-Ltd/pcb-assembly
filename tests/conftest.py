"""全テスト共通の fixture.

``pcbasm.hal`` は picamera2 を、``tests.helpers`` は pcbnew を import するため、
Raspberry Pi と KiCAD が無い環境では読み込めない。root conftest が module 冒頭で
それらを import すると、実機に依存しないテストの collect まで巻き込んで失敗する。
実機依存の import はそれを必要とする fixture の中だけで行う。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import pytest

if TYPE_CHECKING:
    from pcbasm.hal.fake import FakeKlipper
    from tests.support.moonraker import FakeMoonraker


@pytest.fixture(autouse=True)
def disable_mdns_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Settings.from_env()` 経路の mDNS 広告・探索を全テストで無効にする.

    共有 fixture は `discovery_enabled=False` を明示的に渡しているが、env から Settings
    を組むテスト（`uvicorn --factory` と同じ経路の検証）はそれを通らない。 実 LAN
    へ広告・探索を漏らさないための最後の砦なので、mDNS を実際に使う テストは env ではなくコンストラクタ注入（ランダムなサービス型
    + `interfaces=["127.0.0.1"]`）で自分の設定を作る。
    """
    monkeypatch.setenv("PCBASM_API_DISCOVERY_ENABLED", "0")
    monkeypatch.setenv("PCBASM_UI_DISCOVERY_ENABLED", "0")


@pytest.fixture(autouse=True)
def forbid_real_klipper(monkeypatch: pytest.MonkeyPatch) -> None:
    """`open_klipper` を呼ぶとテストが落ちるようにし、実機の Moonraker へ G-code を届かせない.

    テストは本番機の上でも走るので、取りこぼした経路があっても実機は動かない。
    Moonraker 不通の経路を確かめるテストは `unreachable_klipper` を要求する。
    """
    try:
        from pcbasm import hal
    except ImportError:
        return  # hal を読めない環境では、どの src も open_klipper に届かない

    def refuse(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("テストから open_klipper が呼ばれました")

    monkeypatch.setattr(hal, "open_klipper", refuse)


@pytest.fixture
def unreachable_klipper(monkeypatch: pytest.MonkeyPatch) -> None:
    """`open_klipper` が接続先の無い Klipper を返すようにする（送信は httpx の接続エラーになる）.

    設定の接続先は使わないので、設定が実機を指していても届かない。
    """
    from pcbasm.hal import MoonrakerKlipper

    # localhost でなく 127.0.0.1 なのは、IPv6 を試す分だけ失敗が遅れるため
    monkeypatch.setattr(
        "pcbasm.hal.open_klipper",
        lambda host, port, *, timeout=None: MoonrakerKlipper(
            "127.0.0.1", 7126, timeout=timeout
        ),
    )


@pytest.fixture
def moonraker(monkeypatch: pytest.MonkeyPatch) -> FakeMoonraker:
    """`open_klipper` が、HTTP を FakeMoonraker に応えさせる Klipper を返すようにする."""
    import httpx

    from pcbasm.hal import MoonrakerKlipper
    from tests.support.moonraker import FakeMoonraker

    server = FakeMoonraker()
    monkeypatch.setattr(
        "pcbasm.hal.open_klipper",
        lambda host, port, *, timeout=None: MoonrakerKlipper(
            host, port, timeout=timeout, transport=httpx.MockTransport(server.handle)
        ),
    )
    return server


@pytest.fixture
def mount_klipper(monkeypatch: pytest.MonkeyPatch) -> Callable[[FakeKlipper], None]:
    """``open_klipper`` がどれも同じ fake 機体への接続を返すようにする関数を返す.

    backend を組み立て経路（``open_devices`` → ``open_klipper``）ごと確かめるテスト用。
    golden は ``Devices`` に fake 機体を注入する（tests/golden/conftest.py）。
    """

    def mount(klipper: FakeKlipper) -> None:
        monkeypatch.setattr(
            "pcbasm.hal.open_klipper",
            lambda host, port, *, timeout=None: klipper.connection(timeout=timeout),
        )

    return mount


@pytest.fixture(scope="session")
def refusing_bin(tmp_path_factory: pytest.TempPathFactory) -> str:
    """呼ばれたら何もせず失敗する実行ファイル."""
    path = tmp_path_factory.mktemp("refuse") / "refuse"
    path.write_text(
        '#!/bin/sh\necho "テストから $0 $* が呼ばれました" >&2\nexit 1\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


@pytest.fixture(autouse=True)
def forbid_real_systemctl(monkeypatch: pytest.MonkeyPatch, refusing_bin: str) -> None:
    """自己更新の `sudo` と `systemctl` の既定を、呼ばれたら失敗する stub に向ける.

    実機のサービスを再起動させないため。
    スタブを使うテストは `UpdateSettings` に自分のスタブを明示して渡す。
    テストからは既定値（`/usr/bin/systemctl` など）が見えないので、既定値を使う契約は値を明示して確かめる。
    """
    from web.selfupdate.settings import UpdateSettings

    init = UpdateSettings.__init__

    def init_with_refusing_bins(
        self: UpdateSettings,
        *args: Any,
        systemctl_bin: str = refusing_bin,
        sudo_bin: str = refusing_bin,
        **kwargs: Any,
    ) -> None:
        init(self, *args, systemctl_bin=systemctl_bin, sudo_bin=sudo_bin, **kwargs)

    monkeypatch.setattr(UpdateSettings, "__init__", init_with_refusing_bins)


@pytest.fixture(scope="session")
def paste_test_board_footprint_root(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """System KiCad inventoryに依存しない実 ``*.pretty`` library."""
    from tests.helpers import make_paste_test_board_footprint_root

    return make_paste_test_board_footprint_root(
        tmp_path_factory.mktemp("paste-test-board") / "footprints"
    )
