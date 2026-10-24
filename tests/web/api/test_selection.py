"""`web.api.selection` の仕様テスト（実 pcbnew で読む）.

- BoardCache は (path, mtime, size) が変わらなければ読み直さず、変われば読み直す
- 初期化で webui_state.json から復元、不正値は既定値へフォールバック
- select は成功で保存、装置排他ロックが取れなければ BusyError
- 起動時に選択していた PCB が無ければ選択を解除し、notice で知らせる（解除は保存しない）
- merge_job_param_defaults はロック内で「読む → マージ → 保存」を行い、マージ結果を返す
- 保存は atomic replace のため、同時保存でも webui_state.json は常に valid JSON
- _persist_lock は「dumps → 一時ファイル → replace」を直列化するため、同時 merge でも保存済みジョブ名がファイルから巻き戻らない
- ロック順序は装置排他ロック → _persist_lock（装置ロック保持中でも merge は進む）
"""

import json
import shutil
import threading
from pathlib import Path

import pytest

from tests.web.api.conftest import COPPER_PCB_FIXTURE, REAL_PCB_FIXTURE
from web.api.env import Env
from web.api.lock import BusyError, MachineLock
from web.api.selection import BoardCache, Selection


@pytest.fixture
def lock() -> MachineLock:
    return MachineLock()


@pytest.fixture
def selection(webui_settings: Env, lock: MachineLock) -> Selection:
    return Selection(webui_settings, lock)


class TestBoardCache:
    def test_unchanged_file_is_not_read_again(self, tmp_path: Path):
        path = tmp_path / "board.kicad_pcb"
        shutil.copy(COPPER_PCB_FIXTURE, path)
        cache = BoardCache()

        assert cache.get(path) is cache.get(path)

    def test_file_replaced_under_the_same_name_is_read_again(self, tmp_path: Path):
        path = tmp_path / "board.kicad_pcb"
        shutil.copy(COPPER_PCB_FIXTURE, path)
        cache = BoardCache()
        before = cache.get(path)

        shutil.copy(REAL_PCB_FIXTURE, path)
        after = cache.get(path)

        assert {pad.designator for pad in before.pads} >= {"U1", "R1"}
        assert "CONCAVE_L" in {pad.designator for pad in after.pads}

    def test_forget_reads_again_even_if_the_file_is_unchanged(self, tmp_path: Path):
        path = tmp_path / "board.kicad_pcb"
        shutil.copy(COPPER_PCB_FIXTURE, path)
        cache = BoardCache()
        before = cache.get(path)

        cache.forget()

        assert cache.get(path) is not before
        assert cache.get(path) == before


class TestPcbSelection:
    """PCB ファイル選択（pcb_browse_root からの相対パス）."""

    @pytest.mark.parametrize(
        "content", [None, b"(kicad_pcb)"], ids=["select", "upload"]
    )
    def test_select_pcb_stores_relative_path_and_persists(
        self,
        selection: Selection,
        webui_settings: Env,
        content: bytes | None,
    ):
        path = Path(
            "boards/sample.kicad_pcb" if content is None else "uploads/new.kicad_pcb"
        )
        selection.select(path, content=content)

        assert selection.pcb == path
        if content is not None:
            assert (webui_settings.pcb_browse_root / path).read_bytes() == content
        assert (webui_settings.webui_data_dir / "webui_state.json").exists()
        assert not (webui_settings.data_dir / "webui_state.json").exists()
        restored = Selection(webui_settings, MachineLock())
        assert restored.pcb == path

    def test_corrupted_state_file_falls_back_to_default(self, webui_settings: Env):
        (webui_settings.webui_data_dir / "webui_state.json").parent.mkdir(
            parents=True, exist_ok=True
        )
        (webui_settings.webui_data_dir / "webui_state.json").write_text(
            "{ this is not json", encoding="utf-8"
        )

        selection = Selection(webui_settings, MachineLock())

        assert selection.pcb is None

    @pytest.mark.parametrize("content", [None, b"(kicad_pcb)"])
    def test_select_pcb_outside_root_raises_value_error(
        self, selection: Selection, content: bytes | None
    ):
        with pytest.raises(ValueError):
            selection.select(Path("../outside.kicad_pcb"), content=content)

    @pytest.mark.parametrize("content", [None, b"(kicad_pcb)"])
    def test_select_pcb_with_wrong_extension_raises_value_error(
        self, selection: Selection, content: bytes | None
    ):
        with pytest.raises(ValueError):
            selection.select(Path("boards/notes.txt"), content=content)

    def test_select_missing_pcb_raises_value_error(self, selection: Selection):
        with pytest.raises(ValueError):
            selection.select(Path("boards/ghost.kicad_pcb"))

    def test_failed_upload_preserves_selection_and_releases_the_lock(
        self, selection: Selection, lock: MachineLock, pcb_root: Path
    ):
        selected = Path("boards/sample.kicad_pcb")
        selection.select(selected)
        occupied = pcb_root / "occupied.kicad_pcb"
        occupied.mkdir()
        child = occupied / "keep.txt"
        child.write_bytes(b"keep")

        with pytest.raises(OSError):
            selection.select(Path(occupied.name), content=b"replacement")

        assert selection.pcb == selected
        assert child.read_bytes() == b"keep"
        assert not list(pcb_root.glob(".occupied.kicad_pcb.*.tmp"))
        with lock.hold("after-failed-upload"):
            pass

    def test_select_while_locked_raises_busy_error(
        self, selection: Selection, lock: MachineLock
    ):
        with lock.hold("job"):
            with pytest.raises(BusyError):
                selection.select(Path("boards/sample.kicad_pcb"))


class TestMissingSelectionAtStartup:
    """起動時に選択していた PCB が無ければ選択を解除し、選び直すまで知らせる."""

    def _restart_after_removing(
        self, selection: Selection, webui_settings: Env, pcb: Path
    ) -> Selection:
        selection.select(pcb)
        (webui_settings.pcb_browse_root / pcb).unlink()
        return Selection(webui_settings, MachineLock())

    def test_missing_pcb_is_deselected_with_a_notice(
        self, selection: Selection, webui_settings: Env
    ):
        restarted = self._restart_after_removing(
            selection, webui_settings, Path("boards/sample.kicad_pcb")
        )

        assert restarted.pcb is None
        assert restarted.notice is not None
        assert "boards/sample.kicad_pcb" in restarted.notice

    def test_pcb_that_reappears_is_selected_again_on_next_startup(
        self, selection: Selection, webui_settings: Env
    ):
        pcb = Path("boards/sample.kicad_pcb")
        content = (webui_settings.pcb_browse_root / pcb).read_bytes()
        self._restart_after_removing(selection, webui_settings, pcb)
        (webui_settings.pcb_browse_root / pcb).write_bytes(content)

        restarted = Selection(webui_settings, MachineLock())

        assert restarted.pcb == pcb
        assert restarted.notice is None

    def test_selecting_again_clears_the_notice(
        self, selection: Selection, webui_settings: Env
    ):
        restarted = self._restart_after_removing(
            selection, webui_settings, Path("boards/sample.kicad_pcb")
        )

        restarted.select(Path("top.kicad_pcb"))

        assert restarted.notice is None

    def test_existing_pcb_stays_selected_without_notice(
        self, selection: Selection, webui_settings: Env
    ):
        selection.select(Path("boards/sample.kicad_pcb"))

        restarted = Selection(webui_settings, MachineLock())

        assert restarted.pcb == Path("boards/sample.kicad_pcb")
        assert restarted.notice is None


class TestJobParamDefaults:
    """ジョブフォーム既定値の永続化."""

    def test_merge_job_param_defaults_persists_across_instances(
        self,
        selection: Selection,
        webui_settings: Env,
    ):
        selection.merge_job_param_defaults(
            "flow_calibration",
            {"rotations": 60.0, "rate": 1.5, "accel": 20.0, "count": 4},
        )

        restored = Selection(webui_settings, MachineLock())

        assert restored.job_param_defaults("flow_calibration") == {
            "rotations": 60.0,
            "rate": 1.5,
            "accel": 20.0,
            "count": 4,
        }

    def test_unknown_job_param_defaults_are_empty(self, selection: Selection):
        assert selection.job_param_defaults("no-such-job") == {}

    def test_state_file_with_invalid_job_param_defaults_ignores_bad_values(
        self, webui_settings: Env
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

        selection = Selection(webui_settings, MachineLock())

        assert selection.job_param_defaults("flow_calibration") == {
            "rotations": 50.0,
            "count": 3,
        }
        assert selection.job_param_defaults("bad-job") == {}

    def test_merge_keeps_untouched_keys_and_returns_merged(self, selection: Selection):
        selection.merge_job_param_defaults("flow_calibration", {"rotations": 60.0})

        merged = selection.merge_job_param_defaults(
            "flow_calibration", {"rate": 1.5, "rotations": 70.0}
        )

        assert merged == {"rotations": 70.0, "rate": 1.5}
        assert selection.job_param_defaults("flow_calibration") == merged

    def test_merge_is_scoped_per_job(self, selection: Selection):
        selection.merge_job_param_defaults("flow_calibration", {"rotations": 60.0})

        selection.merge_job_param_defaults("height_plane", {"grid": 3})

        assert selection.job_param_defaults("flow_calibration") == {"rotations": 60.0}
        assert selection.job_param_defaults("height_plane") == {"grid": 3}

    def test_returned_dict_is_a_copy(self, selection: Selection):
        merged = selection.merge_job_param_defaults(
            "flow_calibration", {"rotations": 60.0}
        )

        merged["rotations"] = 999.0

        assert selection.job_param_defaults("flow_calibration") == {"rotations": 60.0}

    def test_concurrent_merges_never_roll_back_the_state_file(
        self, selection: Selection, webui_settings: Env
    ):
        """別ジョブ名の同時 merge で、保存済みジョブ名がファイルから消えない.

        ``_persist_lock`` が無いと「A が JSON を組む → B が組んで書く →
        A が古い snapshot で replace」の順序が起きて、B が保存した内容が
        ファイルから巻き戻る。メモリ上は両方残るので、検出には
        書き込み中のファイルを観測する必要がある。
        """
        path = webui_settings.webui_data_dir / "webui_state.json"
        rounds = 200
        selection.merge_job_param_defaults("seed", {"value": 0})
        barrier = threading.Barrier(3)
        stop = threading.Event()
        errors: list[BaseException] = []
        rollbacks: list[set[str]] = []
        reads: list[int] = []

        def merge(job_prefix: str) -> None:
            try:
                barrier.wait(timeout=10.0)
                for index in range(rounds):
                    selection.merge_job_param_defaults(
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

    def test_merge_does_not_block_while_machine_lock_is_held(
        self, selection: Selection, lock: MachineLock
    ):
        """ロック順序 装置排他ロック → _persist_lock の回帰（merge は装置ロックを待たない）."""
        done = threading.Event()
        errors: list[BaseException] = []

        def merge() -> None:
            try:
                selection.merge_job_param_defaults(
                    "flow_calibration", {"rotations": 60.0}
                )
            except BaseException as exc:
                errors.append(exc)
            done.set()

        with lock.hold("job-a"):
            thread = threading.Thread(target=merge)
            thread.start()
            assert done.wait(timeout=10.0)
            thread.join(timeout=10.0)

        assert errors == []
        assert not thread.is_alive()
        assert selection.job_param_defaults("flow_calibration") == {"rotations": 60.0}


class TestPersistenceFailure:
    """保存できなかった変更を公開せず、同じ操作を再試行できる。"""

    @pytest.mark.parametrize("operation", ["select-pcb", "merge-defaults"])
    def test_failed_write_keeps_previous_state_until_retry_succeeds(
        self,
        selection: Selection,
        lock: MachineLock,
        webui_settings: Env,
        operation: str,
    ):
        original_pcb = Path("boards/sample.kicad_pcb")
        selection.select(original_pcb)
        selection.merge_job_param_defaults("flow_calibration", {"rotations": 60.0})
        path = webui_settings.webui_data_dir / "webui_state.json"
        saved = path.read_bytes()
        path.unlink()
        path.mkdir()

        def update() -> None:
            if operation == "select-pcb":
                selection.select(Path("top.kicad_pcb"))
            else:
                selection.merge_job_param_defaults("flow_calibration", {"rate": 1.5})

        with pytest.raises(OSError):
            update()

        assert selection.pcb == original_pcb
        assert selection.job_param_defaults("flow_calibration") == {"rotations": 60.0}
        assert lock.owner is None

        path.rmdir()
        path.write_bytes(saved)
        restored = Selection(webui_settings, MachineLock())
        assert restored.pcb == selection.pcb
        assert restored.job_param_defaults(
            "flow_calibration"
        ) == selection.job_param_defaults("flow_calibration")

        update()

        expected_pcb = (
            Path("top.kicad_pcb") if operation == "select-pcb" else original_pcb
        )
        expected_defaults = {"rotations": 60.0}
        if operation == "merge-defaults":
            expected_defaults["rate"] = 1.5
        restored = Selection(webui_settings, MachineLock())
        for current in (selection, restored):
            assert current.pcb == expected_pcb
            assert current.job_param_defaults("flow_calibration") == expected_defaults
