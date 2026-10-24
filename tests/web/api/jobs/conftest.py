"""`web.api.jobs` テスト共有フィクスチャ・共有ヘルパ.

計画書 webui-phase3.md「公開インターフェース案」が契約。JobManager は
実 AppState / 実 PreviewService / 実 MachineConfigFile と結合する（モックなし）。

worker スレッドとの同期は threading.Event ゲート付きの合成ジョブと
`wait_until` のポーリングで決定的に行う（sleep 固定値依存のアサート禁止）。
machine_config / state（fake camera 付き）fixture は tests/web/api/conftest.py にある。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator

import pytest

from pcbasm.hal import AudioPlayer
from pcbasm.pasting.overrides import BoardOverrides
from tests.support import polling
from web.api.env import Settings
from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.context import JobContext, JobResult
from web.api.jobs.manager import JobManager, JobRecord
from web.api.preview import PreviewService
from web.api.state import AppState

type WaitUntil = Callable[..., None]
type ManagerFactory = Callable[..., JobManager]


def answer_next_prompt(
    record: JobRecord,
    manager: JobManager,
    answer: object,
    answered: set[str],
    *,
    timeout: float = 60.0,
) -> None:
    """未応答の prompt を待って answer を返す（応答済み id は answered で管理）."""
    polling.wait_until(
        lambda: (pending := record.pending_prompt) is not None
        and pending[0] not in answered,
        timeout=timeout,
    )
    pending = record.pending_prompt
    assert pending is not None
    manager.respond_prompt(pending[0], answer)
    answered.add(pending[0])


def register_synthetic(
    catalog: JobCatalog,
    run: Callable[[JobContext], JobResult | None],
    *,
    name: str = "synthetic",
    label: str = "合成ジョブ",
    params: tuple[ParamSpec, ...] = (),
    requires_pcb: bool = False,
    uses_machine: bool = False,
    notify_on_completion: bool = False,
    accepts_commands: bool = False,
    persisted_params: tuple[str, ...] = (),
) -> None:
    """合成ジョブを catalog へ登録する（テスト専用の共通形）."""
    catalog.register(
        JobDefinition(
            name=name,
            label=label,
            run=run,
            params=params,
            requires_pcb=requires_pcb,
            uses_machine=uses_machine,
            notify_on_completion=notify_on_completion,
            accepts_commands=accepts_commands,
            persisted_params=persisted_params,
        )
    )


def register_gated(
    catalog: JobCatalog,
    *,
    name: str = "gated",
    uses_machine: bool = False,
    accepts_commands: bool = False,
    notify_on_completion: bool = False,
) -> threading.Event:
    """gate.set() で成功終了し、abort 要求は checkpoint で拾う合成ジョブを登録する."""
    gate = threading.Event()

    def run(ctx: JobContext) -> None:
        while not gate.wait(timeout=0.02):
            ctx.checkpoint()

    register_synthetic(
        catalog,
        run,
        name=name,
        uses_machine=uses_machine,
        accepts_commands=accepts_commands,
        notify_on_completion=notify_on_completion,
    )
    return gate


@pytest.fixture
def preview(state: AppState) -> PreviewService:
    return PreviewService(state)


@pytest.fixture
def catalog() -> JobCatalog:
    """合成ジョブ登録用の空 catalog."""
    return JobCatalog()


def make_overrides(settings: Settings) -> BoardOverrides:
    """Settings に対応する基板設定ストア（JobManager への DI 用）."""
    return BoardOverrides(settings.webui_data_dir)


@pytest.fixture
def overrides(fake_camera_settings: Settings) -> BoardOverrides:
    """Fake camera 設定に対応する基板設定ストア."""
    return make_overrides(fake_camera_settings)


@pytest.fixture
def make_manager(
    state: AppState,
    preview: PreviewService,
    fake_camera_settings: Settings,
    overrides: BoardOverrides,
) -> Iterator[ManagerFactory]:
    """JobManager のファクトリ。生成した manager はテスト終了時に shutdown する."""
    managers: list[JobManager] = []

    def _make(
        catalog: JobCatalog,
        *,
        log_capacity: int = 500,
        audio_player: AudioPlayer | None = None,
    ) -> JobManager:
        manager = JobManager(
            state,
            preview,
            catalog,
            fake_camera_settings,
            overrides,
            audio_player=audio_player,
            log_capacity=log_capacity,
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
    """条件成立までポーリングする共有ポーラ（tests.support.polling.wait_until）を返す."""
    return polling.wait_until
