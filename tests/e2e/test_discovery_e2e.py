"""MDNS 広告 → 探索の通し E2E（実 zeroconf・1 プロセス・ループバック限定）.

広告側と探索側で **別の `AsyncZeroconf`**（= 別ソケット）を使う。同一インスタンスを
共有すると自分の登録をキャッシュから読むだけになり、マルチキャスト経路（実際に
frontend が別ホストの広告を受け取る経路）が検証されない。

**実 LAN を汚さないための決めごと**:

- サービス型を毎回ランダム化する（`_pcbasmt<hex>._tcp.local.`）。実運用の
  `_pcbasm._tcp` を使うと、同じ LAN の実機や CI の並列ジョブが混ざる
- `interfaces=["127.0.0.1"]` でループバックに閉じ、広告アドレスも `127.0.0.1`
- assert は「**期待した machine_id が現れる**」で書く。総件数では assert しない

`skip_if_no_mdns` を付けるのは、5353 の共有 bind とマルチキャスト join ができない
環境（一部のコンテナ）で失敗ではなく skip にするため。

テスト本体は同期関数で、async のシナリオは `run()` が**専用スレッドの新しい event
loop** で回す。`make test-e2e` は同じセッションで playwright の sync API を使い、
それが main thread の event loop を回し続けるため、`asyncio.run` も anyio も main
thread では「another loop is running」で失敗する（実測）。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import attrs
import httpx
import pytest

from tests.e2e.conftest import (
    LiveServer,
    make_api_settings,
    make_ui_settings,
    start_app,
)
from tests.helpers import random_service_type, skip_if_no_mdns
from web.api.app import create_app as create_backend_app
from web.api.discovery import ServiceAdvertiser, build_service_info
from web.api.settings import Settings
from web.ui.discovery import MachineDiscovery
from web.ui.machines import MachineEndpoint
from web.ui.settings import Settings as UiSettings

_HTTP_TIMEOUT = 10.0

# 探索結果を待つ上限（マルチキャストの往復 + 解決）
_DISCOVERY_TIMEOUT = 20.0

# シナリオ全体の締め切り（pytest-timeout はスレッドの待ちを中断できない）
_SCENARIO_TIMEOUT = 60.0

LOOPBACK = "127.0.0.1"


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """専用スレッドの新しい event loop で async シナリオを実行する.

    main thread は playwright の sync API が event loop を回したままにするので
    使えない。シナリオ内の例外（`pytest.fail` を含む）は呼び出し側へ送り直す。
    """
    failures: list[BaseException] = []

    def target() -> None:
        try:
            asyncio.run(scenario())
        # 失敗（AssertionError / pytest.fail）も含めて main thread へ送り直す
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=target, name="mdns-e2e")
    thread.start()
    thread.join(timeout=_SCENARIO_TIMEOUT)
    if thread.is_alive():
        pytest.fail(f"{_SCENARIO_TIMEOUT}s 以内にシナリオが終わりませんでした")
    if failures:
        raise failures[0]


async def wait_for(
    predicate: Callable[[], bool], *, timeout: float = _DISCOVERY_TIMEOUT
) -> None:
    """条件が成立するまで event loop を回して待つ（固定 sleep で assert しない）."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"{timeout}s 以内に mDNS の条件が成立しませんでした")


class Collector:
    """`MachineDiscovery.on_change` の受け口（最後に通知された一覧を持つ）."""

    def __init__(self) -> None:
        self.endpoints: tuple[MachineEndpoint, ...] = ()

    def __call__(self, endpoints: tuple[MachineEndpoint, ...]) -> None:
        self.endpoints = endpoints

    def find(self, machine_id: str) -> MachineEndpoint | None:
        """期待する machine_id の 1 件を返す（総件数は見ない）."""
        return next(
            (
                endpoint
                for endpoint in self.endpoints
                if endpoint.machine_id == machine_id
            ),
            None,
        )

    def named(self, machine_id: str, name: str) -> bool:
        found = self.find(machine_id)
        return found is not None and found.name == name


@asynccontextmanager
async def browsing(service_type: str) -> AsyncIterator[Collector]:
    """ループバックに閉じた探索を動かす."""
    collector = Collector()
    discovery = MachineDiscovery(
        on_change=collector,
        service_type=service_type,
        interfaces=(LOOPBACK,),
    )
    await discovery.start()
    try:
        yield collector
    finally:
        await discovery.stop()


@asynccontextmanager
async def advertising(
    service_type: str, *, machine_id: str = "e2emdns", name: str | None = None
) -> AsyncIterator[ServiceAdvertiser]:
    """ループバックに閉じた広告を動かす（探索側とは別ソケット）."""
    advertiser = ServiceAdvertiser(
        machine_id=machine_id,
        port=18081,
        name=name,
        machine_type="paste",
        addresses=(LOOPBACK,),
        service_type=service_type,
        interfaces=(LOOPBACK,),
    )
    await advertiser.start()
    try:
        yield advertiser
    finally:
        await advertiser.stop()


@pytest.fixture
def service_type() -> str:
    return random_service_type()


class TestAdvertiseAndDiscover:
    """`ServiceAdvertiser` → `MachineDiscovery` の通し."""

    @skip_if_no_mdns
    def test_advertised_machine_reaches_the_discovery_side(self, service_type: str):
        async def scenario() -> None:
            async with (
                advertising(service_type, name="E2E 黒兎"),
                browsing(service_type) as collector,
            ):
                await wait_for(lambda: collector.find("e2emdns") is not None)

                found = collector.find("e2emdns")
                assert found is not None
                assert (found.host, found.port) == (LOOPBACK, 18081)
                assert found.name == "E2E 黒兎"
                assert found.machine_type == "paste"
                assert found.source == "mdns"

        run(scenario)

    @skip_if_no_mdns
    def test_update_publishes_the_new_display_name(self, service_type: str):
        """`update(name)` の後は新しい表示名が探索側へ届く.

        届かないと、名前を変えてもドロップダウンには最大 75 分（PTR の other-TTL） 古い名前が残る。
        """

        async def scenario() -> None:
            async with (
                advertising(service_type, name="古い名前") as advertiser,
                browsing(service_type) as collector,
            ):
                await wait_for(lambda: collector.named("e2emdns", "古い名前"))

                advertiser.update("新しい名前")

                await wait_for(lambda: collector.named("e2emdns", "新しい名前"))

        run(scenario)

    @skip_if_no_mdns
    def test_unregistered_machine_disappears_from_the_list(self, service_type: str):
        """広告を取り下げる（goodbye が飛ぶ）と探索側の一覧から消える.

        `MachineDiscovery` の ``Removed`` 分岐を通す唯一の経路。消えないと、
        意図的に止めた機体がドロップダウンに残り続ける。総件数では assert せず
        「期待した machine_id が消える」で見る。
        """

        async def scenario() -> None:
            async with browsing(service_type) as collector:
                async with advertising(service_type, name="消える機体"):
                    await wait_for(lambda: collector.find("e2emdns") is not None)
                # 明示的な unregister では goodbye（TTL 0）が飛ぶ
                await wait_for(lambda: collector.find("e2emdns") is None)

        run(scenario)

    @skip_if_no_mdns
    def test_update_with_a_very_long_name_still_publishes(self, service_type: str):
        """255 bytes を超える表示名でも `update` が広告を壊さない（M2 の回帰）.

        クランプが無いと zeroconf が ValueError を投げ、`_republish` の Task 例外
        として消える（そのうえ次回起動時は `start` が同じ例外で落ちて backend が
        起動不能になる）。期待値は `build_service_info`（公開 API）から取る。
        """
        long_name = "黒" * 100
        expected = (
            build_service_info(
                machine_id="e2emdns",
                port=18081,
                name=long_name,
                machine_type=None,
                addresses=(LOOPBACK,),
                service_type=service_type,
            ).properties[b"name"]
            or b""
        ).decode()

        async def scenario() -> None:
            async with (
                advertising(service_type, name="短い名前") as advertiser,
                browsing(service_type) as collector,
            ):
                await wait_for(lambda: collector.named("e2emdns", "短い名前"))

                advertiser.update(long_name)

                await wait_for(lambda: collector.named("e2emdns", expected))
                assert 0 < len(expected.encode()) <= 250
                assert long_name.startswith(expected)

        run(scenario)


class TestBackendAppAdvertises:
    """実 backend アプリ（uvicorn）の広告と設定変更での更新（実装契約 §4 の配線）."""

    @pytest.fixture
    def advertising_settings(self, tmp_path: Path, service_type: str) -> Settings:
        """広告を有効にした backend Settings（ループバックに閉じる）."""
        return attrs.evolve(
            make_api_settings(tmp_path / "api", hostname="e2eadv"),
            discovery_enabled=True,
            advertise_addresses=(LOOPBACK,),
            discovery_service_type=service_type,
            discovery_interfaces=(LOOPBACK,),
        )

    @pytest.fixture
    def live_backend(self, advertising_settings: Settings) -> Iterator[LiveServer]:
        running = start_app(create_backend_app(advertising_settings))
        try:
            yield LiveServer(
                base_url=f"http://{LOOPBACK}:{running.port}",
                settings=advertising_settings,
                port=running.port,
            )
        finally:
            running.stop()

    @skip_if_no_mdns
    def test_lifespan_advertises_and_renames_on_settings_put(
        self, live_backend: LiveServer, service_type: str
    ):
        """広告 ID は `/api/machine-info` と同じ導出（D7）で、改名が広告に届く."""

        async def scenario() -> None:
            async with browsing(service_type) as collector:
                await wait_for(lambda: collector.find("e2eadv") is not None)

                async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                    info = await client.get(f"{live_backend.base_url}/api/machine-info")
                    assert info.json()["machine_id"] == "e2eadv"

                    response = await client.put(
                        f"{live_backend.base_url}/api/settings/machine",
                        json={"values": {"machine_name": "改名後の機体"}},
                    )
                    assert response.status_code == 200, response.text

                await wait_for(lambda: collector.named("e2eadv", "改名後の機体"))

        run(scenario)


class TestDiscoveryIsolation:
    """E2E の共有 fixture が実 LAN へ広告・探索しないことの見張り（T3）.

    ここが落ちたら fixture の `discovery_enabled=False` が外れている。E2E は実 uvicorn
    を起こすので、外れると実 LAN に広告が出る。
    """

    def test_shared_settings_opt_out_of_discovery(
        self, tmp_path: Path, e2e_settings: Settings
    ):
        # 既定は有効（実運用は広告する）。だから共有 fixture 側の明示が要件になる
        assert Settings().discovery_enabled is True
        assert UiSettings().discovery_enabled is True

        # e2e_settings が同じ tmp_path の "api" を使うのでディレクトリを分ける
        api = make_api_settings(tmp_path / "probe", hostname="probe")
        ui = make_ui_settings((), machines_file=tmp_path / "absent.toml")

        assert api.discovery_enabled is False
        assert ui.discovery_enabled is False
        assert e2e_settings.discovery_enabled is False
