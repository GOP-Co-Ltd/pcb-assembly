"""`webui.state.AppState` / `BusyError` の仕様テスト.

計画書「`src/webui/state.py`」節が契約:

- 初期化で webui_state.json から復元、不正値は default_machine へフォールバック
- select_machine / select_pcb は成功で永続化、busy 中は BusyError
- machine_lock は非ブロッキング取得、失敗時 BusyError(現 owner)
- focus_z は calibration JSON の z_position（ファイル欠落等は None）
"""

import json
import shutil
from pathlib import Path

import pytest

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
            printer_cfg_link=tmp_path / "printer.cfg",
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
