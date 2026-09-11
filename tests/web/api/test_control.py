"""`web.api.control` の操作権リースの仕様テスト.

計画書 web-api-ui-split.md「MR6 — 操作権リース + 閲覧モード」節が契約:

- `ClientIdentity.key` は生 id を出さない（HTTP 越しの検証は routers/test_control_api.py）
- `claim` は空きなら取得、他人が保持中なら `ControlDeniedError`（保持者名付き）
- `release` は冪等、`takeover` は誰でも通る（詰みからの脱出口）
- liveness は WS 在線。保持者の接続数が 1 以上なら切断猶予で失効しない
- 無操作失効は `busy()` が True の間は起きない（長時間ジョブ中の失効防止）
- `on_change` はロック解放後に呼ばれる（`loop.call_soon_threadsafe` に入る）
- 同時 `claim` の成功は厳密に 1 本

時計は注入した偽時計を進めるだけで、実時刻は待たない。
"""

import threading
import time
from collections.abc import Callable

import pytest

from web.api.control import (
    ClientIdentity,
    ControlDeniedError,
    ControlLease,
    LeaseInfo,
)

GRACE = 30.0
IDLE = 600.0
WORKERS = 8
ROUNDS = 300


class FakeClock:
    """手動で進める単調時計（``time.monotonic`` の代わりに注入する）."""

    def __init__(self, now: float = 1000.0) -> None:
        self._now = now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class BusyFlag:
    """ジョブ実行中判定（``state.busy_owner is not None`` の代わり）."""

    def __init__(self, busy: bool = False) -> None:
        self.busy = busy

    def __call__(self) -> bool:
        return self.busy


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
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def busy() -> BusyFlag:
    return BusyFlag()


@pytest.fixture
def lease(clock: FakeClock, busy: BusyFlag) -> ControlLease:
    return ControlLease(
        clock=clock,
        busy=busy,
        disconnect_grace=GRACE,
        idle_timeout=IDLE,
    )


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


class TestWebsocketPresence:
    """WS 在線による liveness（保持者が接続している間は切断猶予で失効しない）."""

    def test_connected_holder_survives_disconnect_grace(
        self, lease: ControlLease, clock: FakeClock
    ):
        alice = identity("alice")
        lease.claim(alice)
        lease.connect(alice)

        clock.advance(GRACE * 10)

        info = lease.snapshot()
        assert info.display_name == "alice"
        assert info.connections == 1

    def test_lease_survives_when_one_of_two_connections_drops(
        self, lease: ControlLease, clock: FakeClock
    ):
        alice = identity("alice")
        lease.claim(alice)
        lease.connect(alice)
        lease.connect(alice)

        lease.disconnect(alice)
        clock.advance(GRACE + 1)

        info = lease.snapshot()
        assert info.display_name == "alice"
        assert info.connections == 1

    def test_lease_expires_after_grace_once_all_connections_drop(
        self, lease: ControlLease, clock: FakeClock
    ):
        alice = identity("alice")
        lease.claim(alice)
        lease.connect(alice)
        lease.disconnect(alice)

        clock.advance(GRACE - 1)
        assert lease.snapshot().display_name == "alice"

        clock.advance(2)
        assert not lease.snapshot().held

    def test_holder_that_never_connected_expires_after_grace(
        self, lease: ControlLease, clock: FakeClock
    ):
        lease.claim(identity("alice"))

        clock.advance(GRACE + 1)

        assert not lease.snapshot().held

    def test_reconnect_within_grace_keeps_the_lease(
        self, lease: ControlLease, clock: FakeClock
    ):
        alice = identity("alice")
        lease.claim(alice)
        lease.connect(alice)
        lease.disconnect(alice)

        clock.advance(GRACE - 1)
        lease.connect(alice)
        clock.advance(GRACE * 10)

        assert lease.snapshot().display_name == "alice"

    @pytest.mark.parametrize(
        ("holder_connected", "other_disconnects", "expected_holder"),
        [
            # 他人が接続していても、保持者が未接続なら猶予で失効する
            (False, False, None),
            # 他人が切断しても、保持者が接続していればリースは残る
            (True, True, "alice"),
        ],
    )
    def test_other_clients_presence_does_not_decide_the_lease(
        self,
        lease: ControlLease,
        clock: FakeClock,
        holder_connected: bool,
        other_disconnects: bool,
        expected_holder: str | None,
    ):
        alice = identity("alice")
        bob = identity("bob")
        lease.claim(alice)
        if holder_connected:
            lease.connect(alice)
        lease.connect(bob)
        if other_disconnects:
            lease.disconnect(bob)

        clock.advance(GRACE + 1)

        snapshot = lease.snapshot()
        assert (snapshot.display_name if snapshot.held else None) == expected_holder

    def test_new_client_can_claim_after_expiry(
        self, lease: ControlLease, clock: FakeClock
    ):
        lease.claim(identity("alice"))
        clock.advance(GRACE + 1)

        assert lease.claim(identity("bob")).display_name == "bob"


class TestIdleTimeout:
    """無操作失効（ジョブ実行中は失効しない）."""

    def test_connected_but_idle_lease_expires(
        self, lease: ControlLease, clock: FakeClock
    ):
        alice = identity("alice")
        lease.claim(alice)
        lease.connect(alice)

        clock.advance(IDLE + 1)

        assert not lease.snapshot().held

    def test_busy_machine_keeps_the_lease_past_idle_timeout(
        self, lease: ControlLease, clock: FakeClock, busy: BusyFlag
    ):
        alice = identity("alice")
        lease.claim(alice)
        lease.connect(alice)
        busy.busy = True

        clock.advance(IDLE * 10)

        assert lease.snapshot().display_name == "alice"

    def test_lease_expires_once_the_job_finishes(
        self, lease: ControlLease, clock: FakeClock, busy: BusyFlag
    ):
        alice = identity("alice")
        lease.claim(alice)
        lease.connect(alice)
        busy.busy = True
        clock.advance(IDLE * 10)
        assert lease.snapshot().held

        busy.busy = False

        assert not lease.snapshot().held

    def test_claim_by_holder_refreshes_idle_timer(
        self, lease: ControlLease, clock: FakeClock
    ):
        alice = identity("alice")
        lease.claim(alice)
        lease.connect(alice)

        clock.advance(IDLE - 1)
        lease.claim(alice)
        clock.advance(IDLE - 1)

        assert lease.snapshot().display_name == "alice"


class TestOnChange:
    """保持者変更の通知（ロック保持中には呼ばない）."""

    def test_on_change_fires_for_claim_release_takeover_and_expiry(
        self, clock: FakeClock, busy: BusyFlag
    ):
        calls: list[None] = []
        lease = ControlLease(
            clock=clock,
            busy=busy,
            on_change=lambda: calls.append(None),
            disconnect_grace=GRACE,
            idle_timeout=IDLE,
        )
        alice = identity("alice")

        lease.claim(alice)
        assert len(calls) == 1
        lease.takeover(identity("bob"))
        assert len(calls) == 2
        lease.release(identity("bob"))
        assert len(calls) == 3
        lease.claim(alice)
        assert len(calls) == 4

        clock.advance(GRACE + 1)
        lease.snapshot()
        assert len(calls) == 5

    def test_on_change_does_not_fire_for_reclaim_by_holder(
        self, clock: FakeClock, busy: BusyFlag
    ):
        calls: list[None] = []
        lease = ControlLease(
            clock=clock, busy=busy, on_change=lambda: calls.append(None)
        )
        alice = identity("alice")
        lease.claim(alice)

        lease.claim(alice)
        lease.claim(alice)
        lease.snapshot()

        assert len(calls) == 1

    def test_on_change_does_not_fire_for_denied_claim(
        self, clock: FakeClock, busy: BusyFlag
    ):
        calls: list[None] = []
        lease = ControlLease(
            clock=clock, busy=busy, on_change=lambda: calls.append(None)
        )
        lease.claim(identity("alice"))

        with pytest.raises(ControlDeniedError):
            lease.claim(identity("bob"))

        assert len(calls) == 1

    def test_on_change_is_called_outside_the_lock(
        self, clock: FakeClock, busy: BusyFlag
    ):
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

        lease = ControlLease(clock=clock, busy=busy, on_change=on_change)

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

    def test_only_one_of_eight_threads_takes_over_an_expired_lease(
        self, clock: FakeClock
    ):
        """失効判定と再取得が 1 つのクリティカルセクションで起きること.

        失効判定内の `busy()` が GIL を明け渡す間に排他が無いと全員が取得する。
        """

        def busy() -> bool:
            time.sleep(0.0005)
            return False

        lease = ControlLease(
            clock=clock, busy=busy, disconnect_grace=GRACE, idle_timeout=IDLE
        )
        stale = identity("stale")

        def prepare() -> None:
            # 在線したまま無操作で失効した保持者を置く（切断猶予ではなく
            # 無操作失効の経路を通し、claim 側で busy() を呼ばせる）
            lease.connect(stale)
            lease.takeover(stale)
            clock.advance(IDLE + 1)

        assert race_claims(lease, prepare) == [1] * ROUNDS
