"""`web.ui.machines` の仕様テスト.

計画書 docs/plans/web-api-ui-split.md「MR4」節の契約:

- `MachineEndpoint.label` は **サーバ側で組む表示文字列**
  （`f"{name or machine_id} ({machine_id}: {host})"`）。JS は `textContent` に流すだけ
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

    def test_label_shows_name_with_machine_id_and_host(self):
        assert KUROUSAGI.label == "黒兎 2 号機 (kurousagi002: kurousagi002.local)"

    def test_label_omits_the_port(self):
        """Port は運用者向けの識別に寄与しないので表示しない（host までで一意）."""
        assert "8081" not in KUROUSAGI.label

    def test_label_falls_back_to_machine_id_when_name_is_missing(self):
        endpoint = MachineEndpoint(machine_id="alpha", host="alpha.local", port=8081)

        assert endpoint.label == "alpha (alpha: alpha.local)"

    def test_defaults_to_static_source(self):
        """MR5 の mDNS 探索と区別できるように出自を持つ（既定は静的登録）."""
        assert KUROUSAGI.source == "static"
        assert KUROUSAGI.machine_type is None


class TestMachineRegistry:
    """一覧と解決."""

    def test_list_keeps_registration_order(self):
        first = MachineEndpoint(machine_id="b", host="b.local", port=8081)
        second = MachineEndpoint(machine_id="a", host="a.local", port=8081)

        registry = MachineRegistry((first, second))

        assert registry.list() == (first, second)

    def test_empty_registry_lists_nothing(self):
        assert MachineRegistry().list() == ()

    def test_resolve_returns_the_registered_endpoint(self):
        registry = MachineRegistry((KUROUSAGI,))

        assert registry.resolve("kurousagi002") is KUROUSAGI

    def test_resolve_raises_unknown_machine_for_unregistered_id(self):
        registry = MachineRegistry((KUROUSAGI,))

        with pytest.raises(UnknownMachine, match="kurousagi003"):
            registry.resolve("kurousagi003")

    def test_resolve_on_empty_registry_raises_unknown_machine(self):
        with pytest.raises(UnknownMachine):
            MachineRegistry().resolve("kurousagi002")


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

    def test_port_defaults_to_the_backend_port(self, tmp_path: Path):
        path = write_machines_toml(
            tmp_path,
            '[[machine]]\nmachine_id = "alpha"\nhost = "alpha.local"\n',
        )

        (endpoint,) = load_machines_file(path)

        assert endpoint.port == DEFAULT_BACKEND_PORT == 8081

    def test_port_can_be_defaulted_by_the_caller(self, tmp_path: Path):
        """既定 port は呼び出し側（`Settings.default_backend_port`）が決められる."""
        path = write_machines_toml(
            tmp_path,
            '[[machine]]\nmachine_id = "alpha"\nhost = "alpha.local"\n',
        )

        (endpoint,) = load_machines_file(path, default_port=19999)

        assert endpoint.port == 19999

    def test_file_without_machine_table_yields_no_machines(self, tmp_path: Path):
        path = write_machines_toml(tmp_path, "# まだ登録が無い\n")

        assert load_machines_file(path) == ()

    def test_loaded_machines_are_resolvable(self, tmp_path: Path):
        path = write_machines_toml(
            tmp_path,
            '[[machine]]\nmachine_id = "alpha"\nhost = "alpha.local"\n',
        )

        registry = MachineRegistry(load_machines_file(path))

        assert registry.resolve("alpha").base_url == "http://alpha.local:8081"

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
