"""UI frontend が中継する backend マシンの登録と解決.

静的登録は ``config/machines.toml`` を読むだけで、frontend から書き込む API は
持たない（マシン構成はファイル、または MR5 で足す mDNS 探索が真実）。
"""

from __future__ import annotations

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


class MachineRegistry:
    """既知の backend マシンの一覧（読み取り専用）.

    MR5 が mDNS 探索分のマージ規則をここへ足すが、``list`` / ``resolve`` の契約は
    変えない。
    """

    def __init__(self, endpoints: Iterable[MachineEndpoint] = ()) -> None:
        """Registry を初期化する.

        Args:
            endpoints: 登録するエンドポイント（この順序が一覧の順序になる）
        """
        self._endpoints = tuple(endpoints)
        self._by_id = {endpoint.machine_id: endpoint for endpoint in self._endpoints}

    def list(self) -> tuple[MachineEndpoint, ...]:
        """登録順のマシン一覧."""
        return self._endpoints

    def resolve(self, machine_id: str) -> MachineEndpoint:
        """machine_id からエンドポイントを引く.

        Args:
            machine_id: URL の ``/m/{machine_id}`` 部分

        Returns:
            対応するエンドポイント

        Raises:
            UnknownMachine: 未登録の machine_id
        """
        endpoint = self._by_id.get(machine_id)
        if endpoint is None:
            raise UnknownMachine(f"未知の machine_id: {machine_id}")
        return endpoint


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
