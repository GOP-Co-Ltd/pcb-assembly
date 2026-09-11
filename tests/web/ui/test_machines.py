"""`web.ui.machines` の仕様テスト.

計画書 docs/plans/web-api-ui-split.md「MR4」節の契約:

- `MachineEndpoint.label` は **サーバ側で組む表示文字列**
  （`f"{machine_id}: {host}"` = hostname と所在）。JS は `textContent` に流すだけ
- `MachineRegistry.list()` は登録順、`resolve()` は未知 id で `UnknownMachine`（→ 404）
- `load_machines_file` はファイル不在で空 tuple。frontend は登録 0 台でも起動して
  案内ページを出せることが要件なので、不在は異常ではない
"""

import tomllib
from pathlib import Path

import pytest

from web.ui.machines import (
    DEFAULT_BACKEND_PORT,
    MachineEndpoint,
    MachineRegistry,
    UnknownMachine,
    load_machines_file,
)

KUROUSAGI = MachineEndpoint(
    machine_id="kurousagi002",
    host="kurousagi002.local",
    port=8081,
    name="黒兎 2 号機",
)


def write_machines_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "machines.toml"
    path.write_text(body, encoding="utf-8")
    return path


class TestMachineEndpoint:
    """所在と表示名の組み立て."""

    def test_base_url_points_at_the_backend_api(self):
        assert KUROUSAGI.base_url == "http://kurousagi002.local:8081"

    def test_label_shows_the_hostname_and_host(self):
        assert KUROUSAGI.label == "kurousagi002: kurousagi002.local"


class TestMachineRegistry:
    """一覧と解決."""

    def test_list_keeps_registration_order(self):
        first = MachineEndpoint(machine_id="b", host="b.local", port=8081)
        second = MachineEndpoint(machine_id="a", host="a.local", port=8081)

        registry = MachineRegistry((first, second))

        assert registry.list() == (first, second)

    def test_resolve_returns_the_registered_endpoint(self):
        registry = MachineRegistry((KUROUSAGI,))

        assert registry.resolve("kurousagi002") is KUROUSAGI

    def test_resolve_raises_unknown_machine_for_unregistered_id(self):
        registry = MachineRegistry((KUROUSAGI,))
        empty = MachineRegistry()

        assert empty.list() == ()
        with pytest.raises(UnknownMachine, match="kurousagi003"):
            registry.resolve("kurousagi003")
        with pytest.raises(UnknownMachine, match="kurousagi002"):
            empty.resolve("kurousagi002")


class TestLoadMachinesFile:
    """`config/machines.toml` の読み込み（読むだけ。書き込み API は持たない）."""

    def test_missing_file_yields_no_machines(self, tmp_path: Path):
        """不在で例外にしない = frontend が単独で起動できる."""
        assert load_machines_file(tmp_path / "machines.toml") == ()

    def test_reads_entries_in_file_order(self, tmp_path: Path):
        path = write_machines_toml(
            tmp_path,
            """
            [[machine]]
            machine_id = "kurousagi002"
            host = "kurousagi002.local"
            port = 8081
            name = "黒兎 2 号機"
            machine_type = "paste"

            [[machine]]
            machine_id = "alpha"
            host = "alpha.local"
            port = 18081
            """,
        )

        assert load_machines_file(path) == (
            MachineEndpoint(
                machine_id="kurousagi002",
                host="kurousagi002.local",
                port=8081,
                name="黒兎 2 号機",
                machine_type="paste",
            ),
            MachineEndpoint(machine_id="alpha", host="alpha.local", port=18081),
        )

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        (
            pytest.param({}, 8081, id="既定は backend port"),
            pytest.param({"default_port": 19999}, 19999, id="呼び出し側が決める"),
        ),
    )
    def test_port_falls_back_to_the_caller_default(
        self, tmp_path: Path, kwargs: dict[str, int], expected: int
    ):
        """既定 port は呼び出し側（`Settings.default_backend_port`）が決められる."""
        path = write_machines_toml(
            tmp_path,
            '[[machine]]\nmachine_id = "alpha"\nhost = "alpha.local"\n',
        )

        (endpoint,) = load_machines_file(path, **kwargs)

        assert endpoint.port == expected
        assert DEFAULT_BACKEND_PORT == 8081

    def test_file_without_machine_table_yields_no_machines(self, tmp_path: Path):
        path = write_machines_toml(tmp_path, "# まだ登録が無い\n")

        assert load_machines_file(path) == ()

    @pytest.mark.parametrize(
        "body",
        (
            pytest.param('[[machine]]\nhost = "alpha.local"\n', id="machine_id 欠落"),
            pytest.param('[[machine]]\nmachine_id = "alpha"\n', id="host 欠落"),
            pytest.param(
                '[[machine]]\nmachine_id = ""\nhost = "alpha.local"\n',
                id="machine_id 空",
            ),
            pytest.param(
                '[[machine]]\nmachine_id = "alpha"\nhost = "alpha.local"\n'
                'port = "8081"\n',
                id="port が文字列",
            ),
            pytest.param(
                '[[machine]]\nmachine_id = "alpha"\nhost = "alpha.local"\nport = true\n',
                id="port が真偽値",
            ),
            pytest.param(
                '[[machine]]\nmachine_id = "alpha"\nhost = "alpha.local"\nname = 2\n',
                id="name が数値",
            ),
            pytest.param("machine = 5\n", id="machine が配列でない"),
        ),
    )
    def test_invalid_entry_raises_value_error(self, tmp_path: Path, body: str):
        """運用者が手で書くファイルなので、起動時に落として気付かせる."""
        path = write_machines_toml(tmp_path, body)

        with pytest.raises(ValueError):
            load_machines_file(path)

    def test_broken_toml_raises_decode_error(self, tmp_path: Path):
        path = write_machines_toml(
            tmp_path, '[[machine]]\nmachine_id = "unterminated\n'
        )

        with pytest.raises(tomllib.TOMLDecodeError):
            load_machines_file(path)


# mDNS で発見した黒兎（広告のアドレスがそのまま host になる）
DISCOVERED_KUROUSAGI = MachineEndpoint(
    machine_id="kurousagi",
    host="192.168.100.201",
    port=8081,
    name="黒兎",
    machine_type="paste",
    source="mdns",
)


class TestSetDiscovered:
    """静的登録と mDNS 発見分のマージ規則（MR5）.

    静的登録は運用者が書いた設定なので、同じ machine_id を mDNS で見つけても 所在（host /
    port）と表示名を上書きしない。上書きすると、DHCP で変わった アドレスや別 IF
    のアドレスに勝手に切り替わって「設定したのに違う機体を 叩く」事故になる。
    """

    def test_discovered_only_machines_are_appended_after_static_ones(self):
        static = MachineEndpoint(machine_id="alpha", host="alpha.local", port=8081)
        registry = MachineRegistry((static,))

        registry.set_discovered((DISCOVERED_KUROUSAGI,))

        assert registry.list() == (static, DISCOVERED_KUROUSAGI)

    def test_discovered_machines_keep_discovery_order(self):
        first = MachineEndpoint(
            machine_id="b", host="10.0.0.2", port=8081, source="mdns"
        )
        second = MachineEndpoint(
            machine_id="a", host="10.0.0.1", port=8081, source="mdns"
        )
        registry = MachineRegistry()

        registry.set_discovered((first, second))

        assert registry.list() == (first, second)

    def test_static_host_port_and_name_win_for_the_same_machine_id(self):
        static = MachineEndpoint(
            machine_id="kurousagi",
            host="kurousagi.local",
            port=9000,
            name="静的な黒兎",
        )
        registry = MachineRegistry((static,))

        registry.set_discovered((DISCOVERED_KUROUSAGI,))

        (merged,) = registry.list()
        assert (merged.host, merged.port, merged.name) == (
            "kurousagi.local",
            9000,
            "静的な黒兎",
        )
        # 出自は静的登録のまま（mDNS で見えたかどうかで表示が揺れない）
        assert merged.source == "static"

    def test_missing_name_and_machine_type_are_filled_from_mdns(self):
        static = MachineEndpoint(
            machine_id="kurousagi", host="kurousagi.local", port=8081
        )
        registry = MachineRegistry((static,))

        registry.set_discovered((DISCOVERED_KUROUSAGI,))

        (merged,) = registry.list()
        assert merged.name == "黒兎"
        assert merged.machine_type == "paste"
        assert merged.host == "kurousagi.local"

    def test_resolve_finds_a_discovered_only_machine(self):
        registry = MachineRegistry()

        registry.set_discovered((DISCOVERED_KUROUSAGI,))

        assert registry.resolve("kurousagi") is DISCOVERED_KUROUSAGI

    def test_disappeared_machines_are_dropped_on_the_next_notification(self):
        registry = MachineRegistry()
        registry.set_discovered((DISCOVERED_KUROUSAGI,))

        registry.set_discovered(())

        assert registry.list() == ()
        with pytest.raises(UnknownMachine):
            registry.resolve("kurousagi")

    def test_duplicate_machine_ids_from_mdns_keep_the_first(self):
        """同じ機体が複数アドレスで見えても一覧には 1 行だけ出す."""
        other_address = MachineEndpoint(
            machine_id="kurousagi", host="10.0.0.9", port=8081, source="mdns"
        )
        registry = MachineRegistry()

        registry.set_discovered((DISCOVERED_KUROUSAGI, other_address))

        assert registry.list() == (DISCOVERED_KUROUSAGI,)

    def test_static_registrations_survive_repeated_notifications(self):
        static = MachineEndpoint(machine_id="alpha", host="alpha.local", port=8081)
        registry = MachineRegistry((static,))

        registry.set_discovered((DISCOVERED_KUROUSAGI,))
        registry.set_discovered(())

        assert registry.list() == (static,)
