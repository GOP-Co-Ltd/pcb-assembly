"""UI frontend が中継する backend マシンの登録と解決.

静的登録は ``config/machines.toml`` を読むだけで、frontend から書き込む API は
持たない（マシン構成はファイル、または mDNS 探索が真実）。
"""

from __future__ import annotations

import threading
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

import attrs

# host しか判っていないマシンに使う backend の既定 port（backend WebAPI の Settings.port）
DEFAULT_BACKEND_PORT = 8081


@attrs.frozen
class MachineEndpoint:
    """1 台の backend WebAPI の所在と表示名."""

    machine_id: str
    host: str
    port: int
    name: str | None = None
    machine_type: str | None = None
    source: Literal["static", "mdns"] = "static"

    @property
    def base_url(self) -> str:
        """Backend WebAPI のベース URL."""
        return f"http://{self.host}:{self.port}"

    @property
    def label(self) -> str:
        """マシン選択 UI に出す表示文字列.

        表示文字列はサーバ側で組む（クライアントで組むと表示規則が JS に散る）。
        ``machine_id`` と ``host`` を併記するのは、同名の機体や ``.local`` 名の
        取り違えを画面で見分けられるようにするため。
        """
        return f"{self.name or self.machine_id} ({self.machine_id}: {self.host})"


class UnknownMachine(LookupError):
    """未知の machine_id（→ 404）."""


@attrs.frozen
class _Snapshot:
    """一覧と id 引きを 1 つに束ねた不変スナップショット.

    tuple と dict を別々の属性に持つと、差し替えの途中を読んだリクエストが 「一覧には居るのに resolve
    できない」不整合を見てしまう。
    """

    endpoints: tuple[MachineEndpoint, ...]
    by_id: Mapping[str, MachineEndpoint]


def _merge(
    static: tuple[MachineEndpoint, ...], discovered: tuple[MachineEndpoint, ...]
) -> _Snapshot:
    """静的登録と mDNS 発見分をマージする.

    同一 ``machine_id`` は静的登録の ``host`` / ``port`` / ``name`` を優先し
    （``source`` も ``"static"`` のまま）、静的側が持たない ``name`` /
    ``machine_type`` だけ mDNS 側で埋める。順序は静的登録が先、その後に
    mDNS だけで見つかったマシン（発見順）。
    """
    by_discovered_id: dict[str, MachineEndpoint] = {}
    for endpoint in discovered:
        by_discovered_id.setdefault(endpoint.machine_id, endpoint)
    merged = [
        _fill_gaps(endpoint, by_discovered_id.get(endpoint.machine_id))
        for endpoint in static
    ]
    static_ids = {endpoint.machine_id for endpoint in static}
    merged.extend(
        endpoint
        for machine_id, endpoint in by_discovered_id.items()
        if machine_id not in static_ids
    )
    return _Snapshot(
        endpoints=tuple(merged),
        by_id={endpoint.machine_id: endpoint for endpoint in merged},
    )


def _fill_gaps(
    static: MachineEndpoint, discovered: MachineEndpoint | None
) -> MachineEndpoint:
    """静的登録が持たない表示情報だけを mDNS 側で埋める."""
    if discovered is None:
        return static
    return attrs.evolve(
        static,
        name=static.name or discovered.name,
        machine_type=static.machine_type or discovered.machine_type,
    )


class MachineRegistry:
    """既知の backend マシンの一覧（静的登録 + mDNS 発見分）.

    ``list`` / ``resolve`` はロックを取らずスナップショットを 1 属性から読む
    （`web.ui.pages` の同期パスが毎リクエスト呼ぶため、event loop を
    ロック待ちで止めない）。書き込みは `set_discovered` だけで、こちらは
    ロックの中で新しいスナップショットを作って 1 回代入する。
    """

    def __init__(self, endpoints: Iterable[MachineEndpoint] = ()) -> None:
        """Registry を初期化する.

        Args:
            endpoints: 静的登録のエンドポイント（この順序が一覧の先頭になる）
        """
        self._static = tuple(endpoints)
        self._lock = threading.Lock()
        self._snapshot = _merge(self._static, ())

    def list(self) -> tuple[MachineEndpoint, ...]:
        """静的登録順 → mDNS 発見順のマシン一覧."""
        return self._snapshot.endpoints

    def resolve(self, machine_id: str) -> MachineEndpoint:
        """machine_id からエンドポイントを引く.

        Args:
            machine_id: URL の ``/m/{machine_id}`` 部分

        Returns:
            対応するエンドポイント

        Raises:
            UnknownMachine: 未登録の machine_id
        """
        endpoint = self._snapshot.by_id.get(machine_id)
        if endpoint is None:
            raise UnknownMachine(f"未知の machine_id: {machine_id}")
        return endpoint

    def set_discovered(self, endpoints: Iterable[MachineEndpoint]) -> None:
        """発見したマシン（mDNS）の集合を差し替える（探索コールバック用）.

        Args:
            endpoints: 現在発見しているエンドポイント（発見順）
        """
        with self._lock:
            self._snapshot = _merge(self._static, tuple(endpoints))


def load_machines_file(
    path: Path, *, default_port: int = DEFAULT_BACKEND_PORT
) -> tuple[MachineEndpoint, ...]:
    """``machines.toml`` の ``[[machine]]`` を読む.

    ファイルが無い場合は空 tuple を返す（エラーにしない）。frontend は登録 0 台でも
    起動して案内ページを出せることが要件なので、ファイル不在は異常ではない。

    Args:
        path: machines.toml のパス
        default_port: ``port`` を省略したマシンに使う backend の port
            （``Settings.default_backend_port``）

    Returns:
        記述順の MachineEndpoint

    Raises:
        ValueError: ``machine`` が配列でない、要素がテーブルでない、``machine_id`` /
            ``host`` が欠けている、または値の型が不正な場合
        tomllib.TOMLDecodeError: TOML として壊れている場合
    """
    if not path.is_file():
        return ()
    with path.open("rb") as machines_toml:
        data = tomllib.load(machines_toml)
    entries = data.get("machine", [])
    if not isinstance(entries, list):
        raise ValueError(f"{path}: machine は [[machine]] の配列である必要があります")
    return tuple(
        _endpoint_from_entry(entry, f"{path}: machine[{index}]", default_port)
        for index, entry in enumerate(entries)
    )


def _endpoint_from_entry(
    entry: object, where: str, default_port: int
) -> MachineEndpoint:
    if not isinstance(entry, Mapping):
        raise ValueError(f"{where} はテーブルである必要があります: {entry!r}")
    port = entry.get("port", default_port)
    if not isinstance(port, int) or isinstance(port, bool):
        raise ValueError(f"{where}.port は整数である必要があります: {port!r}")
    return MachineEndpoint(
        machine_id=_required_str(entry, "machine_id", where),
        host=_required_str(entry, "host", where),
        port=port,
        name=_optional_str(entry, "name", where),
        machine_type=_optional_str(entry, "machine_type", where),
    )


def _required_str(entry: Mapping[str, object], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}.{key} は非空の文字列である必要があります: {value!r}")
    return value


def _optional_str(entry: Mapping[str, object], key: str, where: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    return _required_str(entry, key, where)
