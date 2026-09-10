"""`GET /api/machines`（frontend の唯一の自前 JSON）の仕様テスト.

契約（計画書 web-api-ui-split.md「MR5」節 + MR5 実装契約 D2）:

- `registry.list()` の同期ダンプ。**表示名（`label`）と「現在のマシンか」（`current`）は
  サーバが決める**（表示規則と選択判定を JS に複製しない）
- `pages.router` の `/{tab}` キャッチオールに**食われない**（登録順の回帰）。食われると
  ドロップダウン更新が HTML を受け取って一覧が固まる
- mDNS で発見したマシンも同じ形で出る（`source` で出自が判る）

backend は要らない（レジストリだけで答えるエンドポイント）ので、上流を持たない
frontend を組んで検証する。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.ui.app import create_app
from web.ui.machines import MachineEndpoint, MachineRegistry
from web.ui.settings import Settings

STATIC_ALPHA = MachineEndpoint(
    machine_id="alpha", host="alpha.local", port=8081, name="アルファ"
)

DISCOVERED_KUROUSAGI = MachineEndpoint(
    machine_id="kurousagi",
    host="192.168.100.201",
    port=8081,
    name="黒兎",
    machine_type="paste",
    source="mdns",
)


@pytest.fixture
def frontend(tmp_path: Path) -> FastAPI:
    """静的登録 1 台の frontend app（backend は持たない）."""
    return create_app(
        Settings(
            machines=(STATIC_ALPHA,),
            machines_file=tmp_path / "absent.toml",
            # 実 LAN を探索しない（このファイルは探索結果の反映先だけを見る）
            discovery_enabled=False,
        )
    )


@pytest.fixture
def registry(frontend: FastAPI) -> MachineRegistry:
    """探索結果の通知先（`MachineDiscovery.on_change` と同じ入口）."""
    return frontend.state.registry


@pytest.fixture
def client(frontend: FastAPI) -> Iterator[TestClient]:
    with TestClient(frontend) as test_client:
        yield test_client


class TestListMachines:
    """登録一覧のダンプ."""

    def test_returns_json_not_a_tab_page(self, client: TestClient):
        """`/{tab}` キャッチオールより先に登録されている（登録順の回帰）."""
        response = client.get("/api/machines")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert [machine["machine_id"] for machine in response.json()["machines"]] == [
            "alpha"
        ]

    def test_dumps_the_server_built_label(self, client: TestClient):
        (machine,) = client.get("/api/machines").json()["machines"]

        assert machine == {
            "machine_id": "alpha",
            "label": "alpha: alpha.local",
            "name": "アルファ",
            "host": "alpha.local",
            "port": 8081,
            "machine_type": None,
            "source": "static",
            "current": False,
        }

    def test_current_is_decided_by_the_server(self, client: TestClient):
        response = client.get("/api/machines", params={"current": "alpha"})

        (machine,) = response.json()["machines"]
        assert machine["current"] is True

    def test_unknown_current_marks_nothing(self, client: TestClient):
        response = client.get("/api/machines", params={"current": "nobody"})

        (machine,) = response.json()["machines"]
        assert machine["current"] is False

    def test_discovered_machines_appear_after_static_ones(
        self, client: TestClient, registry: MachineRegistry
    ):
        registry.set_discovered((DISCOVERED_KUROUSAGI,))

        machines = client.get("/api/machines").json()["machines"]

        assert [machine["machine_id"] for machine in machines] == [
            "alpha",
            "kurousagi",
        ]
        assert machines[1] == {
            "machine_id": "kurousagi",
            "label": "kurousagi: 192.168.100.201",
            "name": "黒兎",
            "host": "192.168.100.201",
            "port": 8081,
            "machine_type": "paste",
            "source": "mdns",
            "current": False,
        }
