"""`web.api.discovery` の仕様テスト（計画書 web-api-ui-split.md「MR5」節）.

ここは **ソケットを開かない範囲**を押さえる:

- `select_advertise_addresses` の純関数契約（到達不能アドレスを広告に載せない）
- `build_service_info` の TXT ラウンドトリップと ``server`` の回帰
  （``server`` を省略すると zeroconf が instance 名で A レコードを publish し、
  avahi が自ホスト名を改名して SSH / Mainsail の ``.local`` 名が壊れる）
- マルチキャストが使えないときに `start` が例外を投げないこと

広告 → 探索の通しは `tests/e2e/test_discovery_e2e.py`（ランダムなサービス型 +
ループバック限定）で見る。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from zeroconf import ServiceInfo

from tests.helpers import random_service_type
from web.api.app import create_app
from web.api.discovery import (
    SERVICE_TYPE,
    TXT_KEY_API,
    TXT_KEY_ID,
    TXT_KEY_NAME,
    TXT_KEY_TYPE,
    ServiceAdvertiser,
    build_service_info,
    local_ipv4_addresses,
    select_advertise_addresses,
)
from web.api.models import API_VERSION
from web.api.settings import Settings, resolve_machine_id

# このホストに存在しないアドレス（TEST-NET-3）。zeroconf は bind できず OSError
UNUSABLE_INTERFACE = "203.0.113.9"

# TXT の 1 エントリは "key=value" で 255 bytes まで（"name=" の 5 bytes を引く）
MAX_TXT_NAME_BYTES = 250


class TestServiceTypeConstant:
    """ワイヤに出るサービス型（外部の手順・ドキュメントが値に依存する）."""

    def test_service_type_is_the_documented_literal(self):
        """実機確認手順 ``avahi-browse -rt _pcbasm._tcp`` と README がこの値を指す.

        リテラルをここでピンする（定数同士の比較では改名を検出できない）。
        """
        assert SERVICE_TYPE == "_pcbasm._tcp.local."


class TestSelectAdvertiseAddresses:
    """広告に載せるアドレスの選択（純関数）."""

    @pytest.mark.parametrize(
        ("candidates", "expected"),
        [
            # ループバック・リンクローカルは広告に載せない
            (("127.0.0.1", "192.168.1.5"), ("192.168.1.5",)),
            (("169.254.10.20", "10.0.0.3"), ("10.0.0.3",)),
            # 重複は落とし、入力順は保つ
            (
                ("192.168.1.5", "10.0.0.3", "192.168.1.5", "172.16.0.9"),
                ("192.168.1.5", "10.0.0.3", "172.16.0.9"),
            ),
            ((), ()),
            (("127.0.0.1", "169.254.1.1"), ()),
        ],
    )
    def test_selects_only_reachable_addresses(
        self, candidates: tuple[str, ...], expected: tuple[str, ...]
    ):
        assert select_advertise_addresses(candidates) == expected


class TestLocalIpv4Addresses:
    """実インターフェースの列挙（IPv6 と非文字列を混ぜない）."""

    def test_returns_only_parsable_ipv4_addresses(self):
        addresses = local_ipv4_addresses()

        assert addresses
        for address in addresses:
            assert ipaddress.IPv4Address(address)


class TestBuildServiceInfo:
    """広告する ServiceInfo の中身（ワイヤ契約）."""

    @pytest.fixture
    def info(self) -> ServiceInfo:
        return build_service_info(
            machine_id="kurousagi",
            port=8081,
            name="黒兎",
            machine_type="paste",
            addresses=("192.168.100.201",),
        )

    def test_server_is_a_dedicated_name_not_the_avahi_hostname(self, info: ServiceInfo):
        """Avahi が持つ ``kurousagi.local.`` を主張しない（改名事故の回帰）."""
        assert info.server == "kurousagi-pcbasm.local."

    def test_instance_name_is_the_machine_id(self, info: ServiceInfo):
        assert info.name == f"kurousagi.{SERVICE_TYPE}"
        assert info.type == SERVICE_TYPE

    def test_txt_round_trips_as_utf8_bytes(self, info: ServiceInfo):
        assert info.properties == {
            TXT_KEY_ID.encode(): b"kurousagi",
            TXT_KEY_API.encode(): str(API_VERSION).encode(),
            TXT_KEY_NAME.encode(): "黒兎".encode(),
            TXT_KEY_TYPE.encode(): b"paste",
        }

    def test_address_and_port_are_advertised(self, info: ServiceInfo):
        assert info.parsed_addresses() == ["192.168.100.201"]
        assert info.port == 8081

    def test_missing_name_and_type_are_absent_from_txt(self):
        info = build_service_info(
            machine_id="alpha",
            port=8081,
            name=None,
            machine_type=None,
            addresses=("10.0.0.3",),
        )

        assert set(info.properties) == {TXT_KEY_ID.encode(), TXT_KEY_API.encode()}

    def test_instance_override_keeps_the_registered_name(self):
        """改名された広告を更新するときは登録済み instance 名を使う."""
        info = build_service_info(
            machine_id="alpha",
            port=8081,
            name="A",
            machine_type=None,
            addresses=("10.0.0.3",),
            instance=f"alpha-2.{SERVICE_TYPE}",
        )

        assert info.name == f"alpha-2.{SERVICE_TYPE}"
        assert info.server == "alpha-pcbasm.local."


class TestLongDisplayName:
    """長すぎる ``machine_name`` で広告が壊れない（M2 の回帰）.

    ``machine_name`` は自由入力（`PUT /api/settings/machine`）。日本語 100 文字
    （300 bytes）をそのまま TXT に載せると zeroconf が ValueError を投げ、
    (1) `_republish` が Task 例外で無音死し、(2) **次回起動で lifespan の
    `start()` が同じ例外で落ちて backend が起動不能**になる（machine.toml を手で
    直すまで復旧しない）。表示名が切れても広告は生かすのが契約。
    """

    LONG_NAME = "黒" * 100

    @pytest.fixture
    def info(self) -> ServiceInfo:
        return build_service_info(
            machine_id="alpha",
            port=8081,
            name=self.LONG_NAME,
            machine_type="paste",
            addresses=("10.0.0.3",),
        )

    def test_txt_name_fits_in_one_entry(self, info: ServiceInfo):
        name = info.properties[b"name"]

        assert name is not None
        assert 0 < len(name) <= MAX_TXT_NAME_BYTES

    def test_txt_name_is_still_valid_utf8_and_a_prefix(self, info: ServiceInfo):
        """文字境界で切る（壊れた末尾バイトを残すと探索側が name を捨てる）."""
        name = info.properties[b"name"]

        assert name is not None
        assert self.LONG_NAME.startswith(name.decode())

    def test_other_fields_are_unaffected(self, info: ServiceInfo):
        assert info.properties[b"id"] == b"alpha"
        assert info.properties[b"type"] == b"paste"

    @staticmethod
    def _truncation_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.name == "web.api.discovery" and "切り詰め" in record.getMessage()
        ]

    def test_truncation_is_logged(self, caplog: pytest.LogCaptureFixture):
        """無音で切らない（表示名が縮んだ理由をログから辿れるようにする）."""
        with caplog.at_level(logging.INFO, logger="web.api.discovery"):
            build_service_info(
                machine_id="alpha",
                port=8081,
                name=self.LONG_NAME,
                machine_type=None,
                addresses=("10.0.0.3",),
            )

        assert self._truncation_logs(caplog)

    def test_a_name_that_fits_is_not_logged(self, caplog: pytest.LogCaptureFixture):
        """収まる名前でログを出すと、切り詰めの検知に使えない."""
        with caplog.at_level(logging.INFO, logger="web.api.discovery"):
            build_service_info(
                machine_id="alpha",
                port=8081,
                name="黒兎",
                machine_type=None,
                addresses=("10.0.0.3",),
            )

        assert self._truncation_logs(caplog) == []


class TestAdvertiserWithoutMulticast:
    """マルチキャストが使えない環境（広告できなくてもアプリは落とさない）.

    サービス型は毎回ランダム化する。存在しない IF の bind は必ず失敗するが、 万一成功したときに運用のサービス型で実 LAN
    へ広告しないため。
    """

    @pytest.fixture
    def advertiser(self) -> ServiceAdvertiser:
        return ServiceAdvertiser(
            machine_id="alpha",
            port=8081,
            name="A",
            machine_type=None,
            addresses=("10.0.0.3",),
            service_type=random_service_type(),
            interfaces=(UNUSABLE_INTERFACE,),
        )

    def test_start_does_not_raise(self, advertiser: ServiceAdvertiser):
        asyncio.run(advertiser.start())

    def test_update_and_stop_without_a_live_start_are_no_ops(
        self, advertiser: ServiceAdvertiser
    ):
        async def scenario() -> None:
            # start 前と、失敗した start の後のどちらでも例外にならない
            advertiser.update("新しい名前")
            await advertiser.stop()
            await advertiser.start()
            advertiser.update("新しい名前")
            await advertiser.stop()

        asyncio.run(scenario())


class TestAdvertiserWithUnbuildableServiceInfo:
    """ServiceInfo が組めない設定でもアプリを落とさない（M2 の回帰）.

    `_build_info` は machine.toml 由来の値（machine_id / TXT）を使うので、組み立て
    自体が失敗しうる。組み立てを `start` の try の外に置くと lifespan で例外が抜け、
    ``Application startup failed. Exiting.`` になって backend が起動しない。
    """

    @staticmethod
    def _advertiser(
        *, machine_id: str = "alpha", machine_type: str | None = None
    ) -> ServiceAdvertiser:
        return ServiceAdvertiser(
            machine_id=machine_id,
            port=8081,
            name="A",
            machine_type=machine_type,
            addresses=("10.0.0.3",),
            service_type=random_service_type(),
            interfaces=(UNUSABLE_INTERFACE,),
        )

    def test_too_long_machine_id_warns_instead_of_raising(
        self, caplog: pytest.LogCaptureFixture
    ):
        """Instance 名は 63 bytes まで（超えると BadTypeInNameException）."""
        advertiser = self._advertiser(machine_id="a" * 70)

        with caplog.at_level(logging.WARNING):
            asyncio.run(advertiser.start())

        assert "BadTypeInNameException" in caplog.text

    def test_too_long_txt_value_warns_instead_of_raising(
        self, caplog: pytest.LogCaptureFixture
    ):
        """クランプ対象外の TXT 値（machine.toml の machine_type）が長すぎる場合.

        zeroconf は ValueError を投げる。`zeroconf.Error` のサブクラスではないので
        except に ValueError を含めていないと lifespan まで抜ける。
        """
        advertiser = self._advertiser(machine_type="x" * 300)

        with caplog.at_level(logging.WARNING):
            asyncio.run(advertiser.start())

        assert "ValueError" in caplog.text


class TestMachineIdResolution:
    """`resolve_machine_id` — 広告と `/api/machine-info` が同じ ID を使う（D7）."""

    def test_injected_hostname_wins(self):
        assert resolve_machine_id(Settings(hostname="injected")) == "injected"

    def test_machine_info_and_advertisement_share_the_id(
        self, client: TestClient, webui_settings: Settings
    ):
        """`/api/machine-info` の machine_id と広告の instance 名が一致する."""
        machine_id = client.get("/api/machine-info").json()["machine_id"]
        info = build_service_info(
            machine_id=resolve_machine_id(webui_settings),
            port=webui_settings.port,
            name=None,
            machine_type=None,
            addresses=(),
        )

        assert info.name == f"{machine_id}.{SERVICE_TYPE}"


class TestDiscoveryIsolation:
    """共有 fixture が実 LAN へ広告しないことの見張り（T3）.

    ここが落ちたら fixture の `discovery_enabled=False` が外れている。 `tests/web/api`
    は 1000 件超が同じ fixture を使うので、外れると全件が mDNS を撒く。
    """

    def test_app_built_from_the_fixture_has_no_advertiser(self, app: FastAPI):
        assert app.state.advertiser is None

    def test_app_built_from_env_has_no_advertiser(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        config_dir: Path,
    ):
        """`Settings.from_env()` 経路（uvicorn --factory と同じ）でも広告しない.

        共有 fixture の `discovery_enabled=False` はこの経路を通らない。守っているのは
        `tests/conftest.py` の autouse fixture（`PCBASM_API_DISCOVERY_ENABLED=0`）だけ
        なので、そこが外れたらここが落ちる（外れると env から Settings を組む既存
        テストが運用サービス型で全 IF に広告を出す）。

        lifespan は起動しない（`TestClient` を被せない）。`app.state.advertiser` は
        `create_app` が設定し `_lifespan` は start / stop するだけなので、起動すると
        この見張りが守っている性質そのもの（実 LAN に広告を出さない）を破る。
        """
        monkeypatch.setenv("PCBASM_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("PCBASM_API_DATA_DIR", str(tmp_path / "data"))

        app = create_app(Settings.from_env())

        assert app.state.advertiser is None
