"""Backend WebAPI の mDNS (DNS-SD) 広告と、探索側と共有するワイヤ定数.

frontend（`web.ui.discovery`）はここの ``SERVICE_TYPE`` と TXT キーを import して
探索する（ワイヤ定数の定義箇所を 1 つにするため）。``API_VERSION`` は
`web.api.models` の定数をそのまま広告に載せる（二重定義を作らない）。

広告は装置操作の前提ではない。マルチキャストが使えない環境（コンテナ・AP の
マルチキャスト抑制）では WARNING を出して広告なしで動き続ける。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

import ifaddr
from zeroconf import Error as ZeroconfError, InterfaceChoice, IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from web.api.models import API_VERSION

logger = logging.getLogger(__name__)

# DNS-SD のサービス型（広告側・探索側で共有する）
SERVICE_TYPE = "_pcbasm._tcp.local."

# TXT レコードのキー（値は UTF-8 bytes で載る）
TXT_KEY_ID = "id"
TXT_KEY_TYPE = "type"
TXT_KEY_API = "api"

# 広告に載せないアドレスの prefix（loopback と APIPA のリンクローカル）。
# 他ホストから到達できないアドレスを載せると frontend が到達不能な URL を組む
_EXCLUDED_PREFIXES = ("127.", "169.254.")


def local_ipv4_addresses() -> tuple[str, ...]:
    """このホストの IPv4 アドレスを列挙する（列挙順のまま）.

    ``socket.getaddrinfo(gethostname())`` は使わない（`/etc/hosts` の
    ``127.0.1.1`` を掴んで広告が到達不能になる）。
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


def _txt_properties(machine_id: str, machine_type: str | None) -> dict[str, str]:
    """広告する TXT レコード（未設定の項目はキー自体を載せない）."""
    properties = {TXT_KEY_ID: machine_id, TXT_KEY_API: str(API_VERSION)}
    if machine_type:
        properties[TXT_KEY_TYPE] = machine_type
    return properties


def build_service_info(
    *,
    machine_id: str,
    port: int,
    machine_type: str | None,
    addresses: Sequence[str],
    service_type: str = SERVICE_TYPE,
) -> ServiceInfo:
    """広告する ServiceInfo を組む（純関数）.

    ``server`` を明示するのが要点。省略すると zeroconf は instance 名で A レコードを
    publish し、avahi が自ホスト名を改名して SSH / Mainsail の ``.local`` 名が壊れる。
    frontend は名前解決せずアドレスから URL を組むので、この名前は誰も引かない。

    Args:
        machine_id: backend の自己申告 ID（instance 名と TXT の ``id``）
        port: backend WebAPI の port
        machine_type: マシン種別（None なら TXT に載せない）
        addresses: 広告する IPv4 アドレス（`select_advertise_addresses` の結果）
        service_type: DNS-SD のサービス型（テストはランダム型に閉じる）

    Returns:
        登録に渡す ServiceInfo
    """
    return ServiceInfo(
        type_=service_type,
        name=f"{machine_id}.{service_type}",
        port=port,
        server=f"{machine_id}-pcbasm.local.",
        properties=_txt_properties(machine_id, machine_type),
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
        machine_type: str | None,
        addresses: Sequence[str],
        service_type: str = SERVICE_TYPE,
        interfaces: Sequence[str] | None = None,
    ) -> None:
        """広告する内容を保持する（ソケットは `start` まで開かない）.

        Args:
            machine_id: backend の自己申告 ID
            port: backend WebAPI の port
            machine_type: マシン種別
            addresses: 広告する IPv4 アドレス
            service_type: DNS-SD のサービス型
            interfaces: 使うインターフェース（None は zeroconf 既定 = 全 IF。
                テストは ``["127.0.0.1"]`` でループバックに閉じる）
        """
        self._machine_id = machine_id
        self._port = port
        self._machine_type = machine_type
        self._addresses = tuple(addresses)
        self._service_type = service_type
        self._interfaces = tuple(interfaces) if interfaces is not None else None
        self._zeroconf: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None

    def _build_info(self) -> ServiceInfo:
        return build_service_info(
            machine_id=self._machine_id,
            port=self._port,
            machine_type=self._machine_type,
            addresses=self._addresses,
            service_type=self._service_type,
        )

    async def start(self) -> None:
        """広告を開始する（失敗しても例外は投げない）.

        マルチキャストが使えない環境では WARNING を出して広告なしで続行する
        （装置操作は mDNS に依存しない）。失敗後の `stop` は no-op。

        ``_build_info`` も try の内側に置く。machine.toml 由来の値（長すぎる
        ``machine_id`` / TXT 値）で ServiceInfo の組み立て自体が失敗しうるため、
        外に置くと lifespan で例外が抜けて起動不能になる（machine.toml を手で直す
        まで復旧しない）。
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
        # ValueError は TXT / instance 名が長すぎるとき（BadTypeInNameException は
        # ZeroconfError のサブクラス）
        except (OSError, ValueError, ZeroconfError) as exc:
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
                # 送信完了を待つのは goodbye だけ。消えないことが最大 75 分の実害に
                # なる唯一の経路であり、announce は待たなくても実害が無い
                # （次のクエリに応答できる）
                await (await zeroconf.async_unregister_service(info))
        except (OSError, ValueError, ZeroconfError) as exc:
            logger.warning("mDNS 広告を取り下げられません: %s", exc)
        finally:
            await zeroconf.async_close()
