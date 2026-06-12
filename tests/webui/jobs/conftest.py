"""`webui.jobs` テスト共有フィクスチャ.

計画書 webui-phase3.md「公開インターフェース案」が契約。JobManager は
実 AppState / 実 PreviewService / 実 ConfigStore と結合する（モックなし）。

worker スレッドとの同期は threading.Event ゲート付きの合成ジョブと
`wait_until` のポーリングで決定的に行う（sleep 固定値依存のアサート禁止）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from webui.config_store import ConfigStore
from webui.jobs.catalog import JobCatalog
from webui.jobs.manager import JobManager
from webui.preview import PreviewService
from webui.settings import Settings
from webui.state import AppState

type WaitUntil = Callable[..., None]
type ManagerFactory = Callable[..., JobManager]


@pytest.fixture
def store(configs_root: Path) -> ConfigStore:
    return ConfigStore(configs_root)


@pytest.fixture
def state(fake_camera_settings: Settings, store: ConfigStore) -> Iterator[AppState]:
    """FixedImageCamera を使う実 AppState（frame 経路の検証に必要）."""
    state = AppState(fake_camera_settings, store)
    yield state
    state.close()


@pytest.fixture
def preview(state: AppState) -> PreviewService:
    return PreviewService(state)


@pytest.fixture
def catalog() -> JobCatalog:
    """合成ジョブ登録用の空 catalog."""
    return JobCatalog()


@pytest.fixture
def make_manager(
    state: AppState, preview: PreviewService, fake_camera_settings: Settings
) -> Iterator[ManagerFactory]:
    """JobManager のファクトリ。生成した manager はテスト終了時に shutdown する."""
    managers: list[JobManager] = []

    def _make(catalog: JobCatalog, *, log_capacity: int = 500) -> JobManager:
        manager = JobManager(
            state, preview, catalog, fake_camera_settings, log_capacity=log_capacity
        )
        managers.append(manager)
        return manager

    yield _make
    for manager in managers:
        manager.shutdown()


@pytest.fixture
def manager(make_manager: ManagerFactory, catalog: JobCatalog) -> JobManager:
    return make_manager(catalog)


@pytest.fixture
def wait_until() -> WaitUntil:
    """条件が成立するまでポーリングする（タイミングのアサートはしない）."""

    def _wait(
        predicate: Callable[[], bool],
        *,
        timeout: float = 10.0,
        interval: float = 0.02,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(interval)
        pytest.fail(f"{timeout}s 以内に条件が成立しませんでした")

    return _wait
