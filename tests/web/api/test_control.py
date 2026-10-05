"""`web.api.control` の操作権リースの仕様テスト.

計画書 web-api-ui-split.md「MR6 — 操作権リース + 閲覧モード」節が契約:

- `ClientIdentity.key` は生 id を出さない（HTTP 越しの検証は routers/test_control_api.py）
- `claim` は空きなら取得、他人が保持中なら `ControlDeniedError`（保持者名付き）
- `release` は冪等、`takeover` は誰でも通る（詰みからの脱出口）
- 自動解放はしない。保持者の WS が切れても、無操作が続いても保持したまま
  （解放は `release` / `takeover` と、プロセス再起動でリースが消えるときだけ）
- `on_change` はロック解放後に呼ばれる（`loop.call_soon_threadsafe` に入る）
- 同時 `claim` の成功は厳密に 1 本
"""

import threading
from collections.abc import Callable

import pytest

from web.api.control import (
    ClientIdentity,
    ControlDeniedError,
    ControlLease,
    LeaseInfo,
)

WORKERS = 8
ROUNDS = 300


def identity(name: str) -> ClientIdentity:
    """表示名から素の ClientIdentity を作る."""
    return ClientIdentity(session_id=f"session-{name}", display_name=name)


def race_claims(lease: ControlLease, prepare: Callable[[], None]) -> list[int]:
    """`WORKERS` 本の実スレッドで同時 claim を `ROUNDS` 回行う.

    Args:
        lease: 競合させる対象
        prepare: 各ラウンド開始時にリースを競合開始状態へ戻す処理

    Returns:
        ラウンドごとの claim 成功数
    """
    winners: list[str] = []
    wins_per_round: list[int] = []

    def begin_round() -> None:
        # barrier 通過時に 1 スレッドだけが実行する。前ラウンドの結果を確定
        # させてから、次ラウンドの初期状態を作る。
        if winners:
            wins_per_round.append(len(winners))
            winners.clear()
        prepare()

    barrier = threading.Barrier(WORKERS, action=begin_round)

    def worker(index: int) -> None:
        client = identity(f"client{index}")
        for _ in range(ROUNDS):
            barrier.wait()
            try:
                lease.claim(client)
            except ControlDeniedError:
                pass
            else:
                winners.append(client.key)
        # 最終ラウンドの結果も begin_round に確定させる
        barrier.wait()

    threads = [
        threading.Thread(target=worker, args=(index,), daemon=True)
        for index in range(WORKERS)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60.0)
    assert not any(thread.is_alive() for thread in threads)
    return wins_per_round


@pytest.fixture
def lease() -> ControlLease:
    return ControlLease()


class TestClaim:
    """操作権の取得."""

    def test_initial_lease_is_free(self, lease: ControlLease):
        assert lease.snapshot() == LeaseInfo(
            key=None, display_name=None, held=False, connections=0
        )

    def test_claim_takes_a_free_lease(self, lease: ControlLease):
        alice = identity("alice")

        info = lease.claim(alice)

        assert info.held
        assert info.key == alice.key
        assert info.display_name == "alice"
        assert lease.snapshot() == info

    def test_claim_by_other_client_is_denied_with_holder_name(
        self, lease: ControlLease
    ):
        lease.claim(identity("alice"))

        with pytest.raises(ControlDeniedError) as excinfo:
            lease.claim(identity("bob"))

        assert excinfo.value.holder == "alice"
        assert "alice" in str(excinfo.value)
        assert lease.snapshot().display_name == "alice"

    def test_reclaim_by_holder_succeeds_and_refreshes_display_name(
        self, lease: ControlLease
    ):
        lease.claim(ClientIdentity(session_id="s1", display_name="名前未設定"))

        info = lease.claim(ClientIdentity(session_id="s1", display_name="田中"))

        assert info.display_name == "田中"

    def test_claim_succeeds_after_holder_released(self, lease: ControlLease):
        alice = identity("alice")
        lease.claim(alice)
        lease.release(alice)

        assert lease.claim(identity("bob")).display_name == "bob"


class TestRelease:
    """操作権の解放（冪等）."""

    def test_release_frees_the_lease(self, lease: ControlLease):
        alice = identity("alice")
        lease.claim(alice)

        info = lease.release(alice)

        assert not info.held
        assert info.key is None

    def test_release_is_idempotent(self, lease: ControlLease):
        alice = identity("alice")
        lease.claim(alice)

        first = lease.release(alice)
        second = lease.release(alice)
        third = lease.release(alice)

        assert first == second == third

    def test_release_without_claim_is_noop(self, lease: ControlLease):
        assert not lease.release(identity("alice")).held

    def test_release_by_non_holder_keeps_the_holder(self, lease: ControlLease):
        lease.claim(identity("alice"))

        info = lease.release(identity("bob"))

        assert info.display_name == "alice"


class TestTakeover:
    """奪取（誰でも通る詰みからの脱出口）."""

    def test_takeover_replaces_the_holder(self, lease: ControlLease):
        lease.claim(identity("alice"))

        info = lease.takeover(identity("bob"))

        assert info.display_name == "bob"
        assert lease.snapshot().key == identity("bob").key

        # 空きリースへの奪取も同じく通る
        lease.release(identity("bob"))
        assert lease.takeover(identity("bob")).display_name == "bob"

    def test_holder_can_claim_again_after_being_taken_over(self, lease: ControlLease):
        alice = identity("alice")
        lease.claim(alice)
        lease.takeover(identity("bob"))

        with pytest.raises(ControlDeniedError):
            lease.claim(alice)


class TestNoAutoRelease:
    """自動解放しない（WS の在線は接続数の表示にだけ使う）."""

    def test_connections_are_counted_for_the_holder(self, lease: ControlLease):
        alice = identity("alice")
        lease.claim(alice)
        lease.connect(alice)
        lease.connect(alice)
        lease.disconnect(alice)

        info = lease.snapshot()
        assert info.display_name == "alice"
        assert info.connections == 1

    def test_lease_is_kept_after_all_holder_connections_drop(self, lease: ControlLease):
        alice = identity("alice")
        lease.claim(alice)
        lease.connect(alice)
        lease.disconnect(alice)

        info = lease.snapshot()
        assert info.display_name == "alice"
        assert info.connections == 0

    def test_holder_that_never_connected_keeps_the_lease(self, lease: ControlLease):
        lease.claim(identity("alice"))

        assert lease.snapshot().display_name == "alice"

    def test_other_client_cannot_claim_a_disconnected_holders_lease(
        self, lease: ControlLease
    ):
        alice = identity("alice")
        lease.claim(alice)
        lease.connect(alice)
        lease.disconnect(alice)

        with pytest.raises(ControlDeniedError):
            lease.claim(identity("bob"))
        assert lease.takeover(identity("bob")).display_name == "bob"


class TestOnChange:
    """保持者変更の通知（ロック保持中には呼ばない）."""

    def test_on_change_fires_for_claim_release_and_takeover(self):
        calls: list[None] = []
        lease = ControlLease(on_change=lambda: calls.append(None))
        alice = identity("alice")

        lease.claim(alice)
        assert len(calls) == 1
        lease.takeover(identity("bob"))
        assert len(calls) == 2
        lease.release(identity("bob"))
        assert len(calls) == 3
        lease.claim(alice)
        assert len(calls) == 4

        # 切断は保持者を変えないので通知しない
        lease.connect(alice)
        lease.disconnect(alice)
        lease.snapshot()
        assert len(calls) == 4

    def test_on_change_does_not_fire_for_reclaim_by_holder(self):
        calls: list[None] = []
        lease = ControlLease(on_change=lambda: calls.append(None))
        alice = identity("alice")
        lease.claim(alice)

        lease.claim(alice)
        lease.claim(alice)
        lease.snapshot()

        assert len(calls) == 1

    def test_on_change_does_not_fire_for_denied_claim(self):
        calls: list[None] = []
        lease = ControlLease(on_change=lambda: calls.append(None))
        lease.claim(identity("alice"))

        with pytest.raises(ControlDeniedError):
            lease.claim(identity("bob"))

        assert len(calls) == 1

    def test_on_change_is_called_outside_the_lock(self):
        """コールバック中に別スレッドから公開 API を呼べる = ロックは解放済み.

        `on_change` は `JobManager._publish` →
        `loop.call_soon_threadsafe` に入るため、ロック保持中に呼ぶと危ない。
        """
        observed: list[LeaseInfo] = []

        def on_change() -> None:
            worker = threading.Thread(
                target=lambda: observed.append(lease.snapshot()), daemon=True
            )
            worker.start()
            worker.join(timeout=5.0)
            assert not worker.is_alive(), "on_change がロック保持中に呼ばれている"

        lease = ControlLease(on_change=on_change)

        lease.claim(identity("alice"))

        assert [info.display_name for info in observed] == ["alice"]


class TestConcurrentClaim:
    """実スレッドでの同時取得（成功は厳密に 1 本）."""

    def test_only_one_of_eight_threads_claims_a_free_lease(self, lease: ControlLease):
        reset = identity("reset")

        def prepare() -> None:
            lease.takeover(reset)
            lease.release(reset)

        assert race_claims(lease, prepare) == [1] * ROUNDS
