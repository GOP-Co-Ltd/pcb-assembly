"""`webui.state.AppState` / `BusyError` の仕様テスト.

計画書「`src/webui/state.py`」節が契約:

- 初期化で webui_state.json から復元、不正値は default_machine へフォールバック
- select_machine / select_pcb は成功で永続化、busy 中は BusyError
- machine_lock は非ブロッキング取得、失敗時 BusyError(現 owner)
- focus_z は calibration JSON の z_position（ファイル欠落等は None）

Phase 2 追記（計画書 webui-phase2.md「src/webui/state.py」節）:

- frame_hub() は遅延構築 + キャッシュ。構築失敗は例外伝播
- rebuild_camera() / close() は hub を停止して参照破棄（未構築なら no-op）
- select_machine 成功時に hub を再構築する

Phase 3 追記（計画書 webui-phase3.md「src/webui/state.py」節）:

- acquire_machine / release_machine は machine_lock の取得・解放分離形。
  ジョブは request スレッドで取得し worker スレッドで解放するため、
  取得スレッドと別スレッドからの release を許す
- machine_lock の従来挙動は不変（acquire/release の上に再実装）
"""

import json
import shutil
import threading
from pathlib import Path

import attrs
import pytest

from pcbasm.hal import FrameHub
from webui.config_store import ConfigStore
from webui.settings import Settings
from webui.state import AppState, BusyError

from .conftest import TEST_FIXTURE_DIR


@pytest.fixture
def store(configs_root: Path) -> ConfigStore:
    return ConfigStore(configs_root)


@pytest.fixture
def state(webui_settings: Settings, store: ConfigStore) -> AppState:
    return AppState(webui_settings, store)


class TestMachineSelection:
    """マシン選択と永続化."""

    def test_initial_selection_is_default_machine(self, state: AppState):
        assert state.selected_machine == "kurousagi"

    def test_falls_back_to_first_machine_when_default_missing(self, tmp_path: Path):
        configs = tmp_path / "only-fixture-configs"
        shutil.copytree(TEST_FIXTURE_DIR, configs / "test-fixture")
        data_dir = tmp_path / "fallback-data"
        data_dir.mkdir()
        settings = Settings(
            configs_root=configs,
            data_dir=data_dir,
            pcb_browse_root=tmp_path,
            default_machine="kurousagi",
        )

        state = AppState(settings, ConfigStore(configs))

        assert state.selected_machine == "test-fixture"

    def test_select_machine_persists_across_instances(
        self, state: AppState, webui_settings: Settings, store: ConfigStore
    ):
        state.select_machine("test-fixture")

        assert state.selected_machine == "test-fixture"
        assert (webui_settings.data_dir / "webui_state.json").exists()
        restored = AppState(webui_settings, store)
        assert restored.selected_machine == "test-fixture"

    def test_select_unknown_machine_raises_value_error(self, state: AppState):
        with pytest.raises(ValueError):
            state.select_machine("no-such-machine")

    def test_corrupted_state_file_falls_back_to_default(
        self, webui_settings: Settings, store: ConfigStore
    ):
        (webui_settings.data_dir / "webui_state.json").write_text(
            "{ this is not json", encoding="utf-8"
        )

        state = AppState(webui_settings, store)

        assert state.selected_machine == "kurousagi"

    def test_state_file_with_unknown_machine_falls_back_to_default(
        self, webui_settings: Settings, store: ConfigStore
    ):
        (webui_settings.data_dir / "webui_state.json").write_text(
            json.dumps({"selected_machine": "ghost-machine"}), encoding="utf-8"
        )

        state = AppState(webui_settings, store)

        assert state.selected_machine == "kurousagi"


class TestPcbSelection:
    """PCB ファイル選択（pcb_browse_root からの相対パス）."""

    def test_select_pcb_stores_relative_path_and_persists(
        self, state: AppState, webui_settings: Settings, store: ConfigStore
    ):
        state.select_pcb(Path("boards/sample.kicad_pcb"))

        assert state.selected_pcb == Path("boards/sample.kicad_pcb")
        restored = AppState(webui_settings, store)
        assert restored.selected_pcb == Path("boards/sample.kicad_pcb")

    def test_initial_pcb_is_none(self, state: AppState):
        assert state.selected_pcb is None

    def test_select_pcb_outside_root_raises_value_error(self, state: AppState):
        with pytest.raises(ValueError):
            state.select_pcb(Path("../outside.kicad_pcb"))

    def test_select_pcb_with_wrong_extension_raises_value_error(self, state: AppState):
        with pytest.raises(ValueError):
            state.select_pcb(Path("boards/notes.txt"))

    def test_select_missing_pcb_raises_value_error(self, state: AppState):
        with pytest.raises(ValueError):
            state.select_pcb(Path("boards/ghost.kicad_pcb"))


class TestMachineLock:
    """非ブロッキング排他ロック."""

    def test_lock_sets_and_clears_busy_owner(self, state: AppState):
        assert state.busy_owner is None

        with state.machine_lock("machine-control"):
            assert state.busy_owner == "machine-control"

        assert state.busy_owner is None

    def test_lock_conflict_raises_busy_error_with_current_owner(self, state: AppState):
        with state.machine_lock("job-a"):
            with pytest.raises(BusyError) as exc:
                with state.machine_lock("job-b"):
                    pass

        assert exc.value.owner == "job-a"

    def test_lock_is_reacquirable_after_release(self, state: AppState):
        with state.machine_lock("first"):
            pass
        with state.machine_lock("second"):
            assert state.busy_owner == "second"

    def test_select_machine_while_locked_raises_busy_error(self, state: AppState):
        with state.machine_lock("job"):
            with pytest.raises(BusyError):
                state.select_machine("test-fixture")

    def test_select_pcb_while_locked_raises_busy_error(self, state: AppState):
        with state.machine_lock("job"):
            with pytest.raises(BusyError):
                state.select_pcb(Path("boards/sample.kicad_pcb"))


class TestAcquireReleaseMachine:
    """取得・解放分離形の排他ロック（Phase 3）."""

    def test_acquire_sets_busy_owner_and_release_clears_it(self, state: AppState):
        state.acquire_machine("job:demo")

        assert state.busy_owner == "job:demo"

        state.release_machine()
        assert state.busy_owner is None

    def test_release_from_another_thread_unlocks(self, state: AppState):
        # ジョブは request スレッドで取得し worker スレッドで解放する
        state.acquire_machine("job:demo")

        releaser = threading.Thread(target=state.release_machine)
        releaser.start()
        releaser.join(timeout=10.0)

        assert not releaser.is_alive()
        assert state.busy_owner is None
        # 解放後に再取得できる
        state.acquire_machine("job:next")
        state.release_machine()

    def test_acquire_while_held_raises_busy_error_with_owner(self, state: AppState):
        state.acquire_machine("job:first")
        try:
            with pytest.raises(BusyError) as exc:
                state.acquire_machine("job:second")
            assert exc.value.owner == "job:first"
        finally:
            state.release_machine()

    def test_machine_lock_conflicts_with_acquired_machine(self, state: AppState):
        # machine_lock は acquire/release の上に再実装され、同一ロックを共有する
        state.acquire_machine("job:demo")
        try:
            with pytest.raises(BusyError) as exc:
                with state.machine_lock("machine-control"):
                    pass
            assert exc.value.owner == "job:demo"
        finally:
            state.release_machine()


class TestMachineConfig:
    """選択マシンの設定アクセス."""

    def test_machine_loads_selected_machine_config(self, state: AppState):
        assert state.machine().klipper.port == 7126

    def test_focus_z_returns_calibration_z_position(self, state: AppState):
        assert state.focus_z() == -25.0

    def test_focus_z_is_none_when_calibration_file_missing(
        self, state: AppState, configs_root: Path
    ):
        (configs_root / "kurousagi" / "ov9281_test_fixture.json").unlink()

        assert state.focus_z() is None

    def test_focus_z_is_none_when_z_position_absent(
        self, state: AppState, configs_root: Path
    ):
        path = configs_root / "kurousagi" / "ov9281_test_fixture.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["z_position"] = None
        path.write_text(json.dumps(data), encoding="utf-8")

        assert state.focus_z() is None


class TestCameraLifecycle:
    """FrameHub の遅延構築・再構築・後始末（Phase 2）."""

    @pytest.fixture
    def camera_state(
        self, fake_camera_settings: Settings, store: ConfigStore
    ) -> AppState:
        return AppState(fake_camera_settings, store)

    def test_frame_hub_is_constructed_lazily_and_cached(self, camera_state: AppState):
        hub = camera_state.frame_hub()

        assert isinstance(hub, FrameHub)
        assert camera_state.frame_hub() is hub

    def test_rebuild_camera_stops_hub_and_recreates(self, camera_state: AppState):
        hub = camera_state.frame_hub()
        hub.start()

        camera_state.rebuild_camera()

        assert not hub.running
        assert camera_state.frame_hub() is not hub

    def test_select_machine_rebuilds_hub(self, camera_state: AppState):
        hub = camera_state.frame_hub()

        camera_state.select_machine("test-fixture")

        assert camera_state.frame_hub() is not hub

    def test_close_stops_hub(self, camera_state: AppState):
        hub = camera_state.frame_hub()
        hub.start()

        camera_state.close()

        assert not hub.running

    def test_rebuild_and_close_before_construction_are_noop(
        self, camera_state: AppState
    ):
        # 未構築での呼び出しは例外なく完了する（冪等）
        camera_state.rebuild_camera()
        camera_state.close()

    def test_frame_hub_propagates_camera_construction_failure(
        self, fake_camera_settings: Settings, store: ConfigStore, tmp_path: Path
    ):
        settings = attrs.evolve(
            fake_camera_settings, fake_camera_image=tmp_path / "missing.png"
        )
        state = AppState(settings, store)

        with pytest.raises(FileNotFoundError):
            state.frame_hub()
