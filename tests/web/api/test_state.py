"""`web.api.state.AppState` / `BusyError` の仕様テスト.

計画書「`src/webui/state.py`」節が契約:

- 初期化で webui_state.json から復元、不正値は既定値へフォールバック
- select_pcb は成功で永続化、busy 中は BusyError
- machine_lock は非ブロッキング取得、失敗時 BusyError(現 owner)
- focus_z は calibration JSON の z_position（ファイル欠落等は None）

Phase 2 追記（計画書 webui-phase2.md「src/webui/state.py」節）:

- frame_hub() は遅延構築 + キャッシュ
- rebuild_camera() / close() は hub を停止して参照破棄

Phase 3 追記（計画書 webui-phase3.md「src/webui/state.py」節）:

- acquire_machine / release_machine は machine_lock の取得・解放分離形。
  ジョブは request スレッドで取得し worker スレッドで解放するため、
  取得スレッドと別スレッドからの release を許す
- machine_lock の従来挙動は不変（acquire/release の上に再実装）

MR1 追記（計画書 web-api-ui-split.md「MR1」節）:

- merge_job_param_defaults はロック内で「読む → マージ → 永続化」を行い、
  マージ結果を返す（呼び出し側の手書き二重マージを不要にする）
- 永続化は atomic replace のため、同時保存でも webui_state.json は常に valid JSON
- _persist_lock は「dumps → 一時ファイル → replace」を直列化するため、同時
  merge でも保存済みジョブ名がファイルから巻き戻らない
- ロック順序は machine_lock → _persist_lock（装置ロック保持中でも merge は進む）
"""

import json
import threading
from pathlib import Path

import pytest

from pcbasm.hal import FrameHub
from web.api.config_store import ConfigStore
from web.api.settings import Settings
from web.api.state import AppState, BusyError


@pytest.fixture
def state(webui_settings: Settings, store: ConfigStore) -> AppState:
    """実カメラ設定なしの素の AppState（共有 fixture の fake camera 版を意図的に override）.

    本モジュールは選択・永続化・ロックの検証が主目的で、camera / FrameHub を 構築しないため close も不要。
    """
    return AppState(webui_settings, store)


class TestPcbSelection:
    """PCB ファイル選択（pcb_browse_root からの相対パス）."""

    @pytest.mark.parametrize(
        "content", [None, b"(kicad_pcb)"], ids=["select", "upload"]
    )
    def test_select_pcb_stores_relative_path_and_persists(
        self,
        state: AppState,
        webui_settings: Settings,
        store: ConfigStore,
        content: bytes | None,
    ):
        path = Path(
            "boards/sample.kicad_pcb" if content is None else "uploads/new.kicad_pcb"
        )
        state.select_pcb(path, content=content)

        assert state.selected_pcb == path
        if content is not None:
            assert (webui_settings.pcb_browse_root / path).read_bytes() == content
        assert (webui_settings.webui_data_dir / "webui_state.json").exists()
        assert not (webui_settings.data_dir / "webui_state.json").exists()
        restored = AppState(webui_settings, store)
        assert restored.selected_pcb == path

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

    @pytest.mark.parametrize("content", [None, b"(kicad_pcb)"])
    def test_select_pcb_outside_root_raises_value_error(
        self, state: AppState, content: bytes | None
    ):
        with pytest.raises(ValueError):
            state.select_pcb(Path("../outside.kicad_pcb"), content=content)

    @pytest.mark.parametrize("content", [None, b"(kicad_pcb)"])
    def test_select_pcb_with_wrong_extension_raises_value_error(
        self, state: AppState, content: bytes | None
    ):
        with pytest.raises(ValueError):
            state.select_pcb(Path("boards/notes.txt"), content=content)

    def test_select_missing_pcb_raises_value_error(self, state: AppState):
        with pytest.raises(ValueError):
            state.select_pcb(Path("boards/ghost.kicad_pcb"))

    def test_failed_upload_preserves_selection_and_releases_the_lock(
        self, state: AppState, pcb_root: Path
    ):
        selected = Path("boards/sample.kicad_pcb")
        state.select_pcb(selected)
        occupied = pcb_root / "occupied.kicad_pcb"
        occupied.mkdir()
        child = occupied / "keep.txt"
        child.write_bytes(b"keep")

        with pytest.raises(OSError):
            state.select_pcb(Path(occupied.name), content=b"replacement")

        assert state.selected_pcb == selected
        assert child.read_bytes() == b"keep"
        assert not list(pcb_root.glob(".occupied.kicad_pcb.*.tmp"))
        with state.machine_lock("after-failed-upload"):
            pass


class TestJobParamDefaults:
    """ジョブフォーム既定値の永続化."""

    def test_merge_job_param_defaults_persists_across_instances(
        self, state: AppState, webui_settings: Settings, store: ConfigStore
    ):
        state.merge_job_param_defaults(
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

    def test_merge_keeps_untouched_keys_and_returns_merged(self, state: AppState):
        state.merge_job_param_defaults("flow_calibration", {"rotations": 60.0})

        merged = state.merge_job_param_defaults(
            "flow_calibration", {"rate": 1.5, "rotations": 70.0}
        )

        assert merged == {"rotations": 70.0, "rate": 1.5}
        assert state.job_param_defaults("flow_calibration") == merged

    def test_merge_is_scoped_per_job(self, state: AppState):
        state.merge_job_param_defaults("flow_calibration", {"rotations": 60.0})

        state.merge_job_param_defaults("height_plane", {"grid": 3})

        assert state.job_param_defaults("flow_calibration") == {"rotations": 60.0}
        assert state.job_param_defaults("height_plane") == {"grid": 3}

    def test_returned_dict_is_a_copy(self, state: AppState):
        merged = state.merge_job_param_defaults("flow_calibration", {"rotations": 60.0})

        merged["rotations"] = 999.0

        assert state.job_param_defaults("flow_calibration") == {"rotations": 60.0}

    def test_concurrent_merges_never_roll_back_the_state_file(
        self, state: AppState, webui_settings: Settings
    ):
        """別ジョブ名の同時 merge で、保存済みジョブ名がファイルから消えない.

        ``_persist_lock`` が無いと「A が JSON を組む → B が組んで書く →
        A が古い snapshot で replace」の順序が起きて、B が保存した内容が
        ファイルから巻き戻る。メモリ上は両方残るので、検出には
        書き込み中のファイルを観測する必要がある。
        """
        path = webui_settings.webui_data_dir / "webui_state.json"
        rounds = 200
        state.merge_job_param_defaults("seed", {"value": 0})
        barrier = threading.Barrier(3)
        stop = threading.Event()
        errors: list[BaseException] = []
        rollbacks: list[set[str]] = []
        reads: list[int] = []

        def merge(job_prefix: str) -> None:
            try:
                barrier.wait(timeout=10.0)
                for index in range(rounds):
                    state.merge_job_param_defaults(
                        f"{job_prefix}{index}", {"value": index}
                    )
            except BaseException as exc:
                errors.append(exc)

        def watch_persisted_keys() -> None:
            seen: set[str] = set()
            try:
                barrier.wait(timeout=10.0)
                while not stop.is_set():
                    doc = json.loads(path.read_text(encoding="utf-8"))
                    keys = set(doc["job_param_defaults"])
                    reads.append(len(keys))
                    if lost := seen - keys:
                        rollbacks.append(lost)
                    seen |= keys
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=merge, args=("alpha",)),
            threading.Thread(target=merge, args=("bravo",)),
            threading.Thread(target=watch_persisted_keys),
        ]
        for thread in threads:
            thread.start()
        for thread in threads[:2]:
            thread.join(timeout=30.0)
        stop.set()
        threads[2].join(timeout=10.0)

        assert errors == []
        for thread in threads:
            assert not thread.is_alive()
        # 監視スレッドが 1 度も読めていない vacuous pass を潰す
        assert reads
        assert rollbacks == []

    def test_merge_does_not_block_while_machine_lock_is_held(self, state: AppState):
        """ロック順序 machine_lock → _persist_lock の回帰（merge は装置ロックを待たない）."""
        done = threading.Event()
        errors: list[BaseException] = []

        def merge() -> None:
            try:
                state.merge_job_param_defaults("flow_calibration", {"rotations": 60.0})
            except BaseException as exc:
                errors.append(exc)
            done.set()

        with state.machine_lock("job-a"):
            thread = threading.Thread(target=merge)
            thread.start()
            assert done.wait(timeout=10.0)
            thread.join(timeout=10.0)

        assert errors == []
        assert not thread.is_alive()
        assert state.job_param_defaults("flow_calibration") == {"rotations": 60.0}


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
