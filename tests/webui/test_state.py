"""`webui.state.AppState` / `BusyError` の仕様テスト.

計画書「`src/webui/state.py`」節が契約:

- 初期化で webui_state.json から復元、不正値は既定値へフォールバック
- select_pcb は成功で永続化、busy 中は BusyError
- machine_lock は非ブロッキング取得、失敗時 BusyError(現 owner)
- focus_z は calibration JSON の z_position（ファイル欠落等は None）

Phase 2 追記（計画書 webui-phase2.md「src/webui/state.py」節）:

- frame_hub() は遅延構築 + キャッシュ。構築失敗は例外伝播
- rebuild_camera() / close() は hub を停止して参照破棄（未構築なら no-op）

Phase 3 追記（計画書 webui-phase3.md「src/webui/state.py」節）:

- acquire_machine / release_machine は machine_lock の取得・解放分離形。
  ジョブは request スレッドで取得し worker スレッドで解放するため、
  取得スレッドと別スレッドからの release を許す
- machine_lock の従来挙動は不変（acquire/release の上に再実装）

レンズ歪み補正計画（`~/.claude/plans/claude-pixels-mm-0-1mm-300-x-swirling-robin.md`
§4「補正なしへの degrade」）追記:

- frame_hub() は歪み補正器（Undistorter）を組んで FrameHub へ渡す。校正が
  不在・読めない・解像度不一致のいずれでも例外を出さず「補正なし」で映像を
  流し続ける（校正前に校正ジョブが動けることの担保）
"""

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import attrs
import pytest

from pcbasm.hal import FrameHub
from webui.config_store import ConfigStore
from webui.settings import Settings
from webui.state import AppState, BusyError


@pytest.fixture
def state(webui_settings: Settings, store: ConfigStore) -> AppState:
    """実カメラ設定なしの素の AppState（共有 fixture の fake camera 版を意図的に override）.

    本モジュールは選択・永続化・ロックの検証が主目的で、camera / FrameHub を 構築しないため close も不要。
    """
    return AppState(webui_settings, store)


class TestPcbSelection:
    """PCB ファイル選択（pcb_browse_root からの相対パス）."""

    def test_select_pcb_stores_relative_path_and_persists(
        self, state: AppState, webui_settings: Settings, store: ConfigStore
    ):
        state.select_pcb(Path("boards/sample.kicad_pcb"))

        assert state.selected_pcb == Path("boards/sample.kicad_pcb")
        assert (webui_settings.webui_data_dir / "webui_state.json").exists()
        assert not (webui_settings.data_dir / "webui_state.json").exists()
        restored = AppState(webui_settings, store)
        assert restored.selected_pcb == Path("boards/sample.kicad_pcb")

    def test_initial_pcb_is_none(self, state: AppState):
        assert state.selected_pcb is None

    def test_corrupted_state_file_falls_back_to_default(
        self, webui_settings: Settings, store: ConfigStore
    ):
        (webui_settings.webui_data_dir / "webui_state.json").parent.mkdir(
            parents=True, exist_ok=True
        )
        (webui_settings.webui_data_dir / "webui_state.json").write_text(
            "{ this is not json", encoding="utf-8"
        )

        state = AppState(webui_settings, store)

        assert state.selected_pcb is None

    def test_legacy_state_file_is_read_when_new_file_missing(
        self, webui_settings: Settings, store: ConfigStore
    ):
        (webui_settings.data_dir / "webui_state.json").write_text(
            json.dumps({"pcb_file": "boards/sample.kicad_pcb"}), encoding="utf-8"
        )

        state = AppState(webui_settings, store)

        assert state.selected_pcb == Path("boards/sample.kicad_pcb")

    def test_select_pcb_outside_root_raises_value_error(self, state: AppState):
        with pytest.raises(ValueError):
            state.select_pcb(Path("../outside.kicad_pcb"))

    def test_select_pcb_with_wrong_extension_raises_value_error(self, state: AppState):
        with pytest.raises(ValueError):
            state.select_pcb(Path("boards/notes.txt"))

    def test_select_missing_pcb_raises_value_error(self, state: AppState):
        with pytest.raises(ValueError):
            state.select_pcb(Path("boards/ghost.kicad_pcb"))


class TestJobParamDefaults:
    """ジョブフォーム既定値の永続化."""

    def test_save_job_param_defaults_persists_across_instances(
        self, state: AppState, webui_settings: Settings, store: ConfigStore
    ):
        state.save_job_param_defaults(
            "flow_calibration",
            {"rotations": 60.0, "rate": 1.5, "accel": 20.0, "count": 4},
        )

        restored = AppState(webui_settings, store)

        assert restored.job_param_defaults("flow_calibration") == {
            "rotations": 60.0,
            "rate": 1.5,
            "accel": 20.0,
            "count": 4,
        }

    def test_unknown_job_param_defaults_are_empty(self, state: AppState):
        assert state.job_param_defaults("no-such-job") == {}

    def test_state_file_with_invalid_job_param_defaults_ignores_bad_values(
        self, webui_settings: Settings, store: ConfigStore
    ):
        (webui_settings.webui_data_dir / "webui_state.json").parent.mkdir(
            parents=True, exist_ok=True
        )
        (webui_settings.webui_data_dir / "webui_state.json").write_text(
            json.dumps(
                {
                    "job_param_defaults": {
                        "flow_calibration": {
                            "rotations": 50.0,
                            "rate": {"bad": "value"},
                            "count": 3,
                        },
                        "bad-job": "not a dict",
                    },
                }
            ),
            encoding="utf-8",
        )

        state = AppState(webui_settings, store)

        assert state.job_param_defaults("flow_calibration") == {
            "rotations": 50.0,
            "count": 3,
        }
        assert state.job_param_defaults("bad-job") == {}


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
    """`config/` ディレクトリの設定アクセス."""

    def test_machine_loads_config_dir_machine(self, state: AppState):
        assert state.machine().klipper.port == 7126

    def test_focus_z_returns_calibration_z_position(self, state: AppState):
        assert state.focus_z() == -25.0

    def test_focus_z_is_none_when_calibration_file_missing(
        self, state: AppState, config_dir: Path
    ):
        (config_dir / "ov9281_test_fixture.json").unlink()

        assert state.focus_z() is None

    def test_focus_z_is_none_when_z_position_absent(
        self, state: AppState, config_dir: Path
    ):
        path = config_dir / "ov9281_test_fixture.json"
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


class TestUndistortionDegrade:
    """歪み補正が組めないときに映像経路が生き残ること（レンズ歪み補正計画 §4）.

    `frame_hub()` は `load_undistorter(calibration_file,
    camera.resolution.size)` の結果を FrameHub へ渡す。この関数は作れなければ warning 1
    行を出して None （＝補正なし）を返す**単一の degrade 点**であり、ここで例外を漏らすと 「旧 JSON が読めない →
    映像が出ない → 校正ジョブを実行できない」という デッドロックになる。校正 JSON は破壊的変更で旧形式が読めなくなるため、
    この経路は必須要件。
    """

    CALIBRATION_FILE = "ov9281_test_fixture.json"

    @pytest.fixture
    def camera_state(
        self, fake_camera_settings: Settings, store: ConfigStore
    ) -> Iterator[AppState]:
        state = AppState(fake_camera_settings, store)
        yield state
        state.close()

    @staticmethod
    def _assert_frames_flow(state: AppState) -> None:
        """Hub を起動して 1 フレーム取得できることを確かめる（補正の有無に関わらず）."""
        hub = state.frame_hub()
        source = hub.subscribe()
        hub.start()
        try:
            frame = source.capture()
        finally:
            hub.stop()

        assert frame.size == source.resolution.size

    def test_frames_flow_when_the_calibration_file_is_missing(
        self, camera_state: AppState, config_dir: Path
    ):
        """校正前（JSON 不在）でも映像が出る = 校正ジョブを実行できる."""
        (config_dir / self.CALIBRATION_FILE).unlink()

        self._assert_frames_flow(camera_state)

    def test_frames_flow_when_the_calibration_file_is_unreadable(
        self, camera_state: AppState, config_dir: Path
    ):
        """壊れた JSON・旧スキーマでも映像が出る（再校正への導線を残す）."""
        (config_dir / self.CALIBRATION_FILE).write_text(
            json.dumps({"pixel_per_mm": 40.0, "resolution": [1280, 720]}),
            encoding="utf-8",
        )

        self._assert_frames_flow(camera_state)

    def test_frames_flow_when_the_calibration_resolution_mismatches(
        self, checkerboard_camera_settings: Settings, store: ConfigStore
    ):
        """解像度不一致でも映像が出る（K のリスケールで救わず補正なしへ落とす）.

        checkerboard.png は 400x400 で、fixture の校正は 1280x720。
        """
        state = AppState(checkerboard_camera_settings, store)
        try:
            self._assert_frames_flow(state)
        finally:
            state.close()

    def test_frames_flow_with_a_matching_calibration(self, camera_state: AppState):
        """解像度が一致する校正（歪み全ゼロ = 恒等写像）でも映像が出る.

        fake_camera.png は 1280x720 で fixture の校正と一致するため、 こちらは実際に
        Undistorter を通る経路になる（対照）。
        """
        self._assert_frames_flow(camera_state)
