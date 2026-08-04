"""Backend WebAPI の mDNS (DNS-SD) 広告と、探索側と共有するワイヤ定数.

frontend（`web.ui.discovery`）はここの ``SERVICE_TYPE`` と TXT キーを import して
探索する（ワイヤ定数の定義箇所を 1 つにするため）。``API_VERSION`` は
`web.api.models` の定数をそのまま広告に載せる（二重定義を作らない）。

広告は装置操作の前提ではない。マルチキャストが使えない環境（コンテナ・AP の
マルチキャスト抑制）では WARNING を出して広告なしで動き続ける。
"""

from __future__ import annotations

import asyncio
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
TXT_KEY_NAME = "name"
TXT_KEY_TYPE = "type"
TXT_KEY_API = "api"

# 広告に載せないアドレスの prefix（loopback と APIPA のリンクローカル）。
# 他ホストから到達できないアドレスを載せると frontend が到達不能な URL を組む
_EXCLUDED_PREFIXES = ("127.", "169.254.")

# TXT の 1 エントリ（"key=value"）は 1 バイトの長さ前置で符号化されるため 255 bytes
# まで。超えると zeroconf が ValueError を投げ、広告の登録・更新ごと失敗する
_MAX_TXT_ENTRY_BYTES = 255


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


def _clamp_txt_value(key: str, value: str) -> str:
    """TXT の 1 エントリに収まるよう UTF-8 の文字境界で値を切る.

    ``machine_name`` は自由入力（``PUT /api/settings/machine``）なので、長い名前
    （日本語 100 文字 = 300 bytes）がそのまま TXT に載ると zeroconf が ValueError を
    投げ、広告の登録に失敗する。表示名が切れても広告そのものは生かす。

    Args:
        key: TXT のキー（エントリ長は ``key=value`` で数える）
        value: 載せたい値

    Returns:
        エントリ長 255 bytes に収まる値（切る必要が無ければそのまま）
    """
    limit = _MAX_TXT_ENTRY_BYTES - len(key.encode()) - 1
    encoded = value.encode()
    if len(encoded) <= limit:
        return value
    # 文字境界を跨いで切れた末尾のバイトは errors="ignore" が落とす
    clamped = encoded[:limit].decode(errors="ignore")
    logger.info(
        "mDNS TXT の %s を %d bytes に切り詰めました（元は %d bytes）",
        key,
        limit,
        len(encoded),
    )
    return clamped


def _txt_properties(
    machine_id: str, name: str | None, machine_type: str | None
) -> dict[str, str]:
    """広告する TXT レコード（未設定の項目はキー自体を載せない）."""
    properties = {TXT_KEY_ID: machine_id, TXT_KEY_API: str(API_VERSION)}
    if name:
        properties[TXT_KEY_NAME] = _clamp_txt_value(TXT_KEY_NAME, name)
    if machine_type:
        properties[TXT_KEY_TYPE] = machine_type
    return properties


def build_service_info(
    *,
    machine_id: str,
    port: int,
    name: str | None,
    machine_type: str | None,
    addresses: Sequence[str],
    service_type: str = SERVICE_TYPE,
    instance: str | None = None,
) -> ServiceInfo:
    """広告する ServiceInfo を組む（純関数）.

    ``server`` を明示するのが要点。省略すると zeroconf は instance 名で A レコードを
    publish し、avahi が自ホスト名を改名して SSH / Mainsail の ``.local`` 名が壊れる。
    frontend は名前解決せずアドレスから URL を組むので、この名前は誰も引かない。

    Args:
        machine_id: backend の自己申告 ID（instance 名と TXT の ``id``）
        port: backend WebAPI の port
        name: マシンの表示名（None なら TXT に載せない）
        machine_type: マシン種別（None なら TXT に載せない）
        addresses: 広告する IPv4 アドレス（`select_advertise_addresses` の結果）
        service_type: DNS-SD のサービス型（テストはランダム型に閉じる）
        instance: instance 名（None なら ``{machine_id}.{service_type}``）。
            ``allow_name_change=True`` で改名された広告を更新するときに渡す

    Returns:
        登録・更新に渡す ServiceInfo
    """
    return ServiceInfo(
        type_=service_type,
        name=instance or f"{machine_id}.{service_type}",
        port=port,
        server=f"{machine_id}-pcbasm.local.",
        properties=_txt_properties(machine_id, name, machine_type),
        parsed_addresses=list(addresses),
    )


class ServiceAdvertiser:
    """自機の backend WebAPI を mDNS で広告する.

    `start` / `stop` は lifespan から await し、`update` は ``PUT
    /api/settings/machine`` のハンドラ（threadpool 実行の同期関数）から呼ぶ。
    """

    def __init__(
        self,
        *,
        machine_id: str,
        port: int,
        name: str | None,
        machine_type: str | None,
        addresses: Sequence[str],
        service_type: str = SERVICE_TYPE,
        interfaces: Sequence[str] | None = None,
    ) -> None:
        """広告する内容を保持する（ソケットは `start` まで開かない）.

        Args:
            machine_id: backend の自己申告 ID
            port: backend WebAPI の port
            name: マシンの表示名（`update` で差し替える）
            machine_type: マシン種別
            addresses: 広告する IPv4 アドレス
            service_type: DNS-SD のサービス型
            interfaces: 使うインターフェース（None は zeroconf 既定 = 全 IF。
                テストは ``["127.0.0.1"]`` でループバックに閉じる）
        """
        self._machine_id = machine_id
        self._port = port
        self._name = name
        self._machine_type = machine_type
        self._addresses = tuple(addresses)
        self._service_type = service_type
        self._interfaces = tuple(interfaces) if interfaces is not None else None
        self._zeroconf: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # 再登録タスクの参照を保持する（保持しないと GC されて更新が消える）
        self._tasks: set[asyncio.Task[None]] = set()

    def _build_info(self) -> ServiceInfo:
        return build_service_info(
            machine_id=self._machine_id,
            port=self._port,
            name=self._name,
            machine_type=self._machine_type,
            addresses=self._addresses,
            service_type=self._service_type,
            # 改名された広告を上書きしないよう、登録済みの instance 名を使い回す
            instance=self._info.name if self._info is not None else None,
        )

    async def start(self) -> None:
        """広告を開始する（失敗しても例外は投げない）.

        マルチキャストが使えない環境では WARNING を出して広告なしで続行する
        （装置操作は mDNS に依存しない）。失敗後の `update` / `stop` は no-op。

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
        self._loop = asyncio.get_running_loop()
        logger.info("mDNS で広告を開始しました: %s", info.name)

    def update(self, name: str | None) -> None:
        """広告の表示名を差し替える（同期・スレッド安全）.

        ``PUT /api/settings/machine`` のハンドラは同期関数（threadpool 実行）なので
        await できない。event loop へ再登録タスクを積むだけにする。未 start /
        stop 済みなら no-op。

        Args:
            name: 新しい表示名（None なら TXT から落とす）
        """
        self._name = name
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._schedule_update)

    def _schedule_update(self) -> None:
        task = asyncio.create_task(self._republish())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _republish(self) -> None:
        zeroconf = self._zeroconf
        if zeroconf is None:
            return
        # `start` と同じ理由で組み立ても try の内側（例外は Task に残るだけで
        # 誰も retrieve しないため、ここで飲まないと再登録が無音で死ぬ）
        try:
            info = self._build_info()
            # `start` と同じく送信タスクは await しない
            await zeroconf.async_update_service(info)
        except (OSError, ValueError, ZeroconfError) as exc:
            logger.warning("mDNS 広告を更新できません: %s: %s", type(exc).__name__, exc)
            return
        self._info = info

    async def stop(self) -> None:
        """広告を取り下げてソケットを閉じる（未 start なら no-op）.

        ``async_unregister_service`` は goodbye（TTL 0）の送信タスクを返すだけなので、
        それを await してからソケットを閉じる。await せずに閉じると送信がキャンセル
        され、正常終了した機体が探索側の一覧に最大 75 分（PTR の other-TTL）残る。
        """
        zeroconf, info = self._zeroconf, self._info
        self._zeroconf = None
        self._info = None
        self._loop = None
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
