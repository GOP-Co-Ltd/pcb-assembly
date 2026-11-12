"""`web.api.jobs` テストの fixture.

JobManager は実 Backend の装置・ロック・選択・カメラ・machine.toml と結合する。
worker スレッドとの同期は、threading.Event のゲート付きの合成ジョブと `tests.support.polling.wait_until` のポーリングで決定的に行う。
machine_config / camera_backend（fake camera 付き）fixture は tests/web/api/conftest.py にある。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pcbasm.hal.fake import FakeAudioPlayer, FakeMotionStage
from tests.support.web import (
    ManagerFactory,
    fake_motion_stage,
    job_manager,
    motion_devices,
)
from web.api.backend import Backend, open_backend
from web.api.env import Env
from web.api.jobs.runtime.catalog import JobCatalog
from web.api.jobs.runtime.manager import JobManager


@pytest.fixture
def catalog(camera_backend: Backend) -> JobCatalog:
    """``camera_backend`` の全ジョブの catalog（合成ジョブもここへ登録する）."""
    return camera_backend.catalog


@pytest.fixture
def make_manager(camera_backend: Backend) -> Iterator[ManagerFactory]:
    """``camera_backend`` を共有する JobManager のファクトリ.

    生成した manager はテスト終了時に shutdown する。
    """
    managers: list[JobManager] = []

    def _make(catalog: JobCatalog, *, log_capacity: int = 500) -> JobManager:
        manager = job_manager(camera_backend, catalog, log_capacity=log_capacity)
        managers.append(manager)
        return manager

    yield _make
    for manager in managers:
        manager.shutdown()


@pytest.fixture
def manager(make_manager: ManagerFactory, catalog: JobCatalog) -> JobManager:
    return make_manager(catalog)


@pytest.fixture
def motion_stage() -> FakeMotionStage:
    """新ファームの機体のステージ（原点は「未」）."""
    return fake_motion_stage()


@pytest.fixture
def motion_backend(
    webui_settings: Env, audio_player: FakeAudioPlayer, motion_stage: FakeMotionStage
) -> Iterator[Backend]:
    """XYZ を新ファームの ``motion_stage`` で動かす部品実装機の Backend."""
    backend = open_backend(
        webui_settings,
        devices=motion_devices(webui_settings, audio_player, motion_stage),
    )
    yield backend
    backend.close()


@pytest.fixture
def motion_manager(motion_backend: Backend) -> Iterator[JobManager]:
    """``motion_backend`` の装置で、そのカタログのジョブを走らせる JobManager."""
    manager = job_manager(motion_backend, motion_backend.catalog)
    yield manager
    manager.shutdown()
