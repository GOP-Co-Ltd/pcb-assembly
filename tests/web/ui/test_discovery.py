"""`web.ui.discovery` の仕様テスト（計画書 web-api-ui-split.md「MR5」節）.

`endpoint_from_service_info` は純関数なので、実 `ServiceInfo` を組んで検証する
（モックを使わない）。広告 → 探索の通しは `tests/e2e/test_discovery_e2e.py`。

TXT は bytes なので、キー欠損・非 UTF-8 バイトでも例外にせず「その項目を捨てる」
のが契約（他機体の広告 1 件で frontend の一覧が壊れないようにする）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from zeroconf import ServiceInfo

from tests.helpers import random_service_type
from web.api.discovery import SERVICE_TYPE
from web.api.models import API_VERSION
from web.ui.app import create_app
from web.ui.discovery import MachineDiscovery, endpoint_from_service_info
from web.ui.settings import Settings

# このホストに存在しないアドレス（TEST-NET-3）。zeroconf は bind できず OSError
UNUSABLE_INTERFACE = "203.0.113.9"


def make_info(
    *,
    instance: str = "kurousagi",
    properties: dict[bytes, bytes] | None = None,
    addresses: list[str] | None = None,
    port: int | None = 8081,
) -> ServiceInfo:
    """探索側が受け取る形の ServiceInfo を組む（TXT は bytes）."""
    if properties is None:
        properties = {
            b"id": b"kurousagi",
            b"type": b"paste",
            b"api": str(API_VERSION).encode(),
        }
    return ServiceInfo(
        type_=SERVICE_TYPE,
        name=f"{instance}.{SERVICE_TYPE}",
        port=port,
        server=f"{instance}-pcbasm.local.",
        properties=properties,
        parsed_addresses=["192.168.100.201"] if addresses is None else addresses,
    )


class TestEndpointFromServiceInfo:
    """広告 → MachineEndpoint の変換（純関数）."""

    def test_full_advertisement_maps_every_field(self):
        endpoint = endpoint_from_service_info(make_info())

        assert endpoint is not None
        assert endpoint.machine_id == "kurousagi"
        assert endpoint.host == "192.168.100.201"
        assert endpoint.port == 8081
        assert endpoint.machine_type == "paste"
        assert endpoint.source == "mdns"

    @pytest.mark.parametrize(
        "properties",
        (
            pytest.param({b"type": b"paste"}, id="id 欠落"),
            pytest.param({b"id": b"\xff\xfe"}, id="id が非 UTF-8"),
        ),
    )
    def test_unreadable_id_falls_back_to_the_instance_name(
        self, properties: dict[bytes, bytes]
    ):
        endpoint = endpoint_from_service_info(
            make_info(instance="alpha", properties=properties)
        )

        assert endpoint is not None
        assert endpoint.machine_id == "alpha"

    def test_non_utf8_machine_type_is_dropped(self):
        endpoint = endpoint_from_service_info(
            make_info(properties={b"id": b"alpha", b"type": b"\xfe"})
        )

        assert endpoint is not None
        assert endpoint.machine_type is None

    def test_advertisement_without_ipv4_address_is_dropped(self):
        assert endpoint_from_service_info(make_info(addresses=[])) is None

    def test_advertisement_without_port_is_dropped(self):
        assert endpoint_from_service_info(make_info(port=0)) is None

    def test_mismatched_api_version_is_dropped(self):
        """描けない版の backend はドロップダウンに出さない（D3）."""
        info = make_info(
            properties={b"id": b"alpha", b"api": str(API_VERSION + 1).encode()}
        )

        assert endpoint_from_service_info(info) is None

    @pytest.mark.parametrize(
        "properties",
        (
            {b"id": b"alpha"},
            {b"id": b"alpha", b"api": b"\xff"},
            {b"id": b"alpha", b"api": b"not-a-number"},
        ),
        ids=("missing", "non_utf8", "not_a_number"),
    )
    def test_unreadable_api_version_is_treated_as_compatible(
        self, properties: dict[bytes, bytes]
    ):
        endpoint = endpoint_from_service_info(make_info(properties=properties))

        assert endpoint is not None
        assert endpoint.machine_id == "alpha"


class TestDiscoveryWithoutMulticast:
    """マルチキャストが使えない環境（探索できなくても frontend は動く）.

    `web.ui.discovery` は async なので anyio の pytest プラグインで駆動する
    （`tests/web/ui/test_machine_client.py` と同じ形）。

    サービス型は毎回ランダム化する。存在しない IF の bind は必ず失敗するが、万一
    成功したときに運用のサービス型で実 LAN を探索しないため。
    """

    pytestmark = pytest.mark.anyio

    @pytest.fixture
    def anyio_backend(self) -> str:
        return "asyncio"

    @pytest.fixture
    def discovery(self) -> MachineDiscovery:
        return MachineDiscovery(
            on_change=lambda _endpoints: None,
            service_type=random_service_type(),
            interfaces=(UNUSABLE_INTERFACE,),
        )

    async def test_start_does_not_raise(self, discovery: MachineDiscovery):
        await discovery.start()
        await discovery.stop()


class TestDiscoveryIsolation:
    """共有 fixture が実 LAN を探索しないことの見張り（T3）.

    ここが落ちたら fixture の `discovery_enabled=False` が外れている。
    """

    def test_shared_settings_fixture_disables_discovery(self, ui_settings: Settings):
        assert ui_settings.discovery_enabled is False

    def test_app_built_from_the_fixture_has_no_discovery(self, frontend_app: FastAPI):
        assert frontend_app.state.discovery is None

    def test_app_built_from_env_has_no_discovery(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """`Settings.from_env()` 経路（uvicorn --factory と同じ）でも探索しない.

        共有 fixture の `discovery_enabled=False` はこの経路を通らない。守っているのは
        `tests/conftest.py` の autouse fixture（`PCBASM_UI_DISCOVERY_ENABLED=0`）だけ
        なので、そこが外れたらここが落ちる。

        lifespan は起動しない（`TestClient` を被せない）。`app.state.discovery` は
        `create_app` が設定し `_lifespan` は start / stop するだけなので、起動すると
        この見張りが守っている性質そのもの（実 LAN を探索しない）を破る。
        """
        # リポジトリの config/machines.toml を拾って実機の登録を混ぜない
        monkeypatch.setenv("PCBASM_UI_MACHINES_FILE", str(tmp_path / "absent.toml"))

        app = create_app()

        assert app.state.discovery is None
