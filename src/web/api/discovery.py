"""Backend WebAPI の mDNS (DNS-SD) 広告.

ワイヤ定数と ``API_VERSION`` は探索側（`web.ui.discovery`）と共有する `web.contract.discovery` に置く。

広告は装置操作の前提ではない。マルチキャストが使えない環境（コンテナ・AP の
マルチキャスト抑制）では WARNING を出して広告なしで動き続ける。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

import ifaddr
from zeroconf import Error as ZeroconfError, InterfaceChoice, IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from web.contract.discovery import (
    API_VERSION,
    SERVICE_TYPE,
    TXT_KEY_API,
    TXT_KEY_CAPABILITIES,
    TXT_KEY_ID,
)

logger = logging.getLogger(__name__)

# 広告に載せないアドレスの prefix（loopback と APIPA のリンクローカル）。
# 他ホストから到達できないアドレスを載せると frontend が到達不能な URL を組む
_EXCLUDED_PREFIXES = ("127.", "169.254.")


def local_ipv4_addresses() -> tuple[str, ...]:
    """このホストの IPv4 アドレスを列挙する（列挙順のまま）.

    ``socket.getaddrinfo(gethostname())`` は使わない（`/etc/hosts` の
    ``127.0.1.1`` が返り、広告が到達不能になる）。
    """
    return tuple(
        ip.ip
        for adapter in ifaddr.get_adapters()
        for ip in adapter.ips
        if ip.is_IPv4 and isinstance(ip.ip, str)
    )


def select_advertise_addresses(candidates: Iterable[str]) -> tuple[str, ...]:
    """広告に載せるアドレスを選ぶ（純関数）.

    Args:
        candidates: well-formed な IPv4 dotted-quad の列（`local_ipv4_addresses`）

    Returns:
        loopback / リンクローカルを除き、重複を除いた入力順のアドレス
    """
    selected: list[str] = []
    for address in candidates:
        if address.startswith(_EXCLUDED_PREFIXES) or address in selected:
            continue
        selected.append(address)
    return tuple(selected)


def build_service_info(
    *,
    machine_id: str,
    port: int,
    addresses: Sequence[str],
    capabilities: Sequence[str],
    service_type: str = SERVICE_TYPE,
) -> ServiceInfo:
    """広告する ServiceInfo を組む（純関数）.

    ``server`` は必ず明示する。省略すると zeroconf は instance 名で A レコードを
    publish し、avahi が自ホスト名を改名して SSH / Mainsail の ``.local`` 名で
    接続できなくなる。
    frontend は名前解決せずアドレスから URL を組むので、この名前は誰も引かない。

    Args:
        machine_id: backend の自己申告 ID（instance 名と TXT の ``id``）
        port: backend WebAPI の port
        addresses: 広告する IPv4 アドレス（`select_advertise_addresses` の結果）
        capabilities: 機体の機能（TXT の ``caps``）
        service_type: DNS-SD のサービス型（テストはランダム型に閉じる）

    Returns:
        登録に渡す ServiceInfo
    """
    return ServiceInfo(
        type_=service_type,
        name=f"{machine_id}.{service_type}",
        port=port,
        server=f"{machine_id}-pcbasm.local.",
        properties={
            TXT_KEY_ID: machine_id,
            TXT_KEY_API: str(API_VERSION),
            TXT_KEY_CAPABILITIES: ",".join(capabilities),
        },
        parsed_addresses=list(addresses),
    )


class ServiceAdvertiser:
    """自機の backend WebAPI を mDNS で広告する.

    `start` / `stop` は lifespan から await する。広告する内容は machine.toml と
    ホスト名から起動時に決まり、運転中に変わらない。
    """

    def __init__(
        self,
        *,
        machine_id: str,
        port: int,
        addresses: Sequence[str],
        capabilities: Sequence[str],
        service_type: str = SERVICE_TYPE,
        interfaces: Sequence[str] | None = None,
    ) -> None:
        """広告する内容を保持する（ソケットは `start` まで開かない）.

        Args:
            machine_id: backend の自己申告 ID
            port: backend WebAPI の port
            addresses: 広告する IPv4 アドレス
            capabilities: 機体の機能
            service_type: DNS-SD のサービス型
            interfaces: 使うインターフェース（None は zeroconf 既定 = 全 IF。
                テストは ``["127.0.0.1"]`` でループバックに閉じる）
        """
        self._machine_id = machine_id
        self._port = port
        self._addresses = tuple(addresses)
        self._capabilities = tuple(capabilities)
        self._service_type = service_type
        self._interfaces = tuple(interfaces) if interfaces is not None else None
        self._zeroconf: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None

    def _build_info(self) -> ServiceInfo:
        return build_service_info(
            machine_id=self._machine_id,
            port=self._port,
            addresses=self._addresses,
            capabilities=self._capabilities,
            service_type=self._service_type,
        )

    async def start(self) -> None:
        """広告を開始する（失敗しても例外は投げない）.

        マルチキャストが使えない環境では WARNING を出して広告なしで続行する
        （装置操作は mDNS に依存しない）。失敗後の `stop` は no-op。

        ``_build_info`` も try の内側に置く。長すぎる ``machine_id``（ホスト名）で
        ServiceInfo の組み立て自体が失敗しうるため、外に置くと lifespan で例外が
        抜けて起動不能になる。
        """
        if self._zeroconf is not None:
            return
        zeroconf: AsyncZeroconf | None = None
        try:
            info = self._build_info()
            zeroconf = AsyncZeroconf(
                ip_version=IPVersion.V4Only,
                interfaces=(
                    list(self._interfaces)
                    if self._interfaces is not None
                    else InterfaceChoice.All
                ),
            )
            # 返る Awaitable は announce の送信タスク。await しない（`stop` の
            # コメント参照。待つと lifespan が約 0.5s 余分にブロックする）
            await zeroconf.async_register_service(info, allow_name_change=True)
        # instance 名が長すぎると BadTypeInNameException（ZeroconfError のサブクラス）
        except (OSError, ZeroconfError) as exc:
            logger.warning("mDNS 広告を開始できません: %s: %s", type(exc).__name__, exc)
            if zeroconf is not None:
                await zeroconf.async_close()
            return
        self._zeroconf = zeroconf
        self._info = info
        logger.info("mDNS で広告を開始しました: %s", info.name)

    async def stop(self) -> None:
        """広告を取り下げてソケットを閉じる（未 start なら no-op）.

        ``async_unregister_service`` は goodbye（TTL 0）の送信タスクを返すだけなので、
        それを await してからソケットを閉じる。await せずに閉じると送信がキャンセル
        され、正常終了した機体が探索側の一覧に最大 75 分（PTR の other-TTL）残る。
        """
        zeroconf, info = self._zeroconf, self._info
        self._zeroconf = None
        self._info = None
        if zeroconf is None:
            return
        try:
            if info is not None:
                # 送信完了を待つのは goodbye だけ。goodbye が届かないと機体が一覧に
                # 最大 75 分残る。announce は待たなくても次のクエリに応答できるので
                # 問題にならない
                await (await zeroconf.async_unregister_service(info))
        except (OSError, ValueError, ZeroconfError) as exc:
            logger.warning("mDNS 広告を取り下げられません: %s", exc)
        finally:
            await zeroconf.async_close()
