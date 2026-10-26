"""`web.api.lock.MachineLock` / `BusyError` の仕様テスト.

- hold は非ブロッキングで取得し、取れなければ BusyError(現 owner)
- acquire / release は hold の取得・解放分離形。ジョブは request スレッドで取得し worker スレッドで解放するため、取得スレッドと別スレッドからの release を許す
"""

import threading

import pytest

from web.api.lock import BusyError, MachineLock


@pytest.fixture
def lock() -> MachineLock:
    return MachineLock()


class TestHold:
    """非ブロッキング排他ロック."""

    def test_hold_sets_and_clears_owner(self, lock: MachineLock):
        assert lock.owner is None

        with lock.hold("machine-control"):
            assert lock.owner == "machine-control"

        assert lock.owner is None

    def test_lock_conflict_raises_busy_error_with_current_owner(
        self, lock: MachineLock
    ):
        with lock.hold("job-a"):
            with pytest.raises(BusyError) as exc:
                with lock.hold("job-b"):
                    pass

        assert exc.value.owner == "job-a"

    def test_lock_is_reacquirable_after_release(self, lock: MachineLock):
        with lock.hold("first"):
            pass
        with lock.hold("second"):
            assert lock.owner == "second"


class TestAcquireRelease:
    """取得・解放分離形の排他ロック（ジョブは開始時に取得し、ワーカーの終了時に解放する）."""

    def test_acquire_sets_owner_and_release_clears_it(self, lock: MachineLock):
        lock.acquire("job:demo")

        assert lock.owner == "job:demo"

        lock.release()
        assert lock.owner is None

    def test_release_from_another_thread_unlocks(self, lock: MachineLock):
        # ジョブは request スレッドで取得し worker スレッドで解放する
        lock.acquire("job:demo")

        releaser = threading.Thread(target=lock.release)
        releaser.start()
        releaser.join(timeout=10.0)

        assert not releaser.is_alive()
        assert lock.owner is None
        # 解放後に再取得できる
        lock.acquire("job:next")
        lock.release()

    def test_acquire_while_held_raises_busy_error_with_owner(self, lock: MachineLock):
        lock.acquire("job:first")
        try:
            with pytest.raises(BusyError) as exc:
                lock.acquire("job:second")
            assert exc.value.owner == "job:first"
        finally:
            lock.release()

    def test_hold_conflicts_with_acquired_lock(self, lock: MachineLock):
        # hold は acquire/release の上にあり、同一ロックを共有する
        lock.acquire("job:demo")
        try:
            with pytest.raises(BusyError) as exc:
                with lock.hold("machine-control"):
                    pass
            assert exc.value.owner == "job:demo"
        finally:
            lock.release()
