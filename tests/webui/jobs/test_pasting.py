"""`webui.jobs.pasting` の仕様テスト.

計画書 memory/agents/implementation-planner/webui-phase5.md「src/webui/jobs/pasting.py」節
+ spec §10 pasting 表が契約:

- catalog: pasting 6 ジョブ（paste_solder / height_plane / loading /
  flow_calibration / toolhead_offset / probe_gnd_down_adjust）の name /
  params（default・unit）/ requires_pcb / uses_machine / accepts_commands
- parse_loading_command: extrude / suck / finish の純粋パーサ。amount 欠落・
  非正・非数・未知 type は None
- LOADING_STAGE: ジョブ実装・テンプレート data 属性・loading_controls.js の
  3 箇所で一致させる契約値 "ローディング"（計画書 判断保留点 6）
- probe_gnd_down_adjust: prompt(number) ループ（負数は log + 再 prompt、初回
  default は machine.probe.down_distance）。終了時（FAILED / ABORTED 含む）は
  down(0) を best-effort 送信し、失敗 log は「ダウン距離」を含む（テスト契約）
- height_plane: 計測前に planned_points.png を artifacts へ生成し、
  diagnostics と /artifacts/ リンクを log してから confirm を挟む。
  confirm False → ABORTED / True → Klipper 不通（setup）で FAILED
- 装置ジョブの異常系: test-fixture（Klipper port 7126 = 接続拒否）で graceful
  FAILED + relax (M84) 失敗警告 + 排他ロック解放
- Apply 反映先 3 キーは config_store のホワイトリスト登録済み（計画書 前提）

実押出・SUCCEEDED 到達・Apply 反映は実機区分（`@mark_hardware`、ユーザー実行。
paste_solder / toolhead_offset の実機通しはプレビュー目視を伴うため WebUI 手動
E2E（計画書 §5 引き継ぎ 3・6）に委ねる）。

cv2 / Moonraker / matplotlib / time.sleep のモックは使わない
（skill `testing-strategy`）。Klipper 不通は test-fixture の実ポートへの
接続拒否で検証する。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import cv2
import pytest

from tests.helpers import mark_hardware
from webui.config_store import ConfigStore
from webui.jobs.catalog import JobCatalog, default_catalog
from webui.jobs.manager import JobManager, JobRecord, JobStatus
from webui.jobs.pasting import (
    LOADING_STAGE,
    Extrude,
    Finish,
    parse_loading_command,
    register_pasting_jobs,
)
from webui.preview import PreviewService
from webui.settings import Settings
from webui.state import AppState

from .conftest import WaitUntil

PASTING_JOBS = (
    "paste_solder",
    "height_plane",
    "loading",
    "flow_calibration",
    "toolhead_offset",
    "probe_gnd_down_adjust",
)


@pytest.fixture
def catalog() -> JobCatalog:
    """Pasting 6 ジョブのみ登録した catalog（jobs/conftest の manager が使う）."""
    catalog = JobCatalog()
    register_pasting_jobs(catalog)
    return catalog


def _answer_next_prompt(
    record: JobRecord,
    manager: JobManager,
    wait_until: WaitUntil,
    answer: object,
    answered: set[str],
    *,
    timeout: float = 60.0,
) -> None:
    """未応答の prompt を待って answer を返す（応答済み id は answered で管理）."""
    wait_until(
        lambda: (pending := record.pending_prompt) is not None
        and pending[0] not in answered,
        timeout=timeout,
    )
    pending = record.pending_prompt
    assert pending is not None
    manager.respond_prompt(pending[0], answer)
    answered.add(pending[0])


class TestCatalog:
    """default_catalog への pasting 6 ジョブ登録（計画書「ジョブ定義表」のピン）."""

    @pytest.fixture
    def default(self) -> JobCatalog:
        return default_catalog()

    def test_pasting_tab_has_exactly_phase5_jobs(self, default: JobCatalog):
        names = {definition.name for definition in default.list(tab="pasting")}

        assert names == set(PASTING_JOBS)

    def test_total_job_count_covers_all_tabs(self, default: JobCatalog):
        """Dev 5 + posctrl 4 + pasting 6 = 15（重複登録・登録漏れの検知）."""
        assert len(default.list()) == 15

    @pytest.mark.parametrize(
        ("name", "requires_pcb", "accepts_commands"),
        [
            ("paste_solder", True, True),
            ("height_plane", True, False),
            ("loading", False, True),
            ("flow_calibration", False, True),
            ("toolhead_offset", True, True),
            ("probe_gnd_down_adjust", False, False),
        ],
    )
    def test_job_flags(
        self,
        default: JobCatalog,
        name: str,
        requires_pcb: bool,
        accepts_commands: bool,
    ):
        definition = default.get(name)

        assert definition.requires_pcb is requires_pcb
        assert definition.uses_machine is True  # 全 pasting ジョブが装置を使う
        assert definition.accepts_commands is accepts_commands

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            (
                "paste_solder",
                {"tolerance": (0.1, "mm"), "amount": (0.1, "uL")},
            ),
            ("height_plane", {"tolerance": (0.1, "mm")}),
            ("loading", {"amount": (0.1, "uL")}),
            (
                "flow_calibration",
                {
                    "rotations": (30.0, "rev"),
                    "rate": (5.0, "rev/s"),
                    "accel": (10.0, "rev/s^2"),
                    "load_amount": (0.1, "uL"),
                },
            ),
            (
                "toolhead_offset",
                {
                    "tolerance": (0.1, "mm"),
                    "dispense_amount": (0.1, "uL"),
                    "loading_amount": (0.1, "uL"),
                    "lift_height": (5.0, "mm"),
                    "paste_diameter_min": (0.0, "mm"),
                    "paste_diameter_max": (2.0, "mm"),
                },
            ),
        ],
    )
    def test_float_param_defaults_and_units(
        self,
        default: JobCatalog,
        name: str,
        expected: dict[str, tuple[float, str]],
    ):
        params = {spec.name: spec for spec in default.get(name).params}
        float_params = {
            key: spec for key, spec in params.items() if spec.value_type == "float"
        }

        assert set(float_params) == set(expected)
        for key, (value, unit) in expected.items():
            assert float_params[key].default == value, key
            assert float_params[key].unit == unit, key

    def test_paste_solder_interactive_loading_is_bool_defaulting_false(
        self, default: JobCatalog
    ):
        params = {spec.name: spec for spec in default.get("paste_solder").params}

        assert set(params) == {"tolerance", "amount", "interactive_loading"}
        assert params["interactive_loading"].value_type == "bool"
        assert params["interactive_loading"].default is False

    def test_probe_gnd_down_adjust_has_no_params(self, default: JobCatalog):
        assert default.get("probe_gnd_down_adjust").params == ()


class TestParseLoadingCommand:
    """parse_loading_command（純粋関数）と LOADING_STAGE の契約値."""

    def test_loading_stage_is_pinned_for_template_and_js(self):
        """ジョブ実装・data-loading-stage 属性・JS の 3 箇所契約（判断保留点 6）."""
        assert LOADING_STAGE == "ローディング"

    def test_extrude_yields_positive_amount(self):
        assert parse_loading_command({"type": "extrude", "amount": 2.5}) == Extrude(2.5)

    def test_suck_yields_negative_amount(self):
        assert parse_loading_command({"type": "suck", "amount": 2.5}) == Extrude(-2.5)

    def test_finish_yields_finish(self):
        assert parse_loading_command({"type": "finish"}) == Finish()

    @pytest.mark.parametrize(
        "command",
        [
            {"type": "extrude"},  # amount 欠落
            {"type": "extrude", "amount": 0},  # 非正
            {"type": "extrude", "amount": -1.0},  # 負
            {"type": "extrude", "amount": "abc"},  # 非数
            {"type": "suck"},  # amount 欠落
            {"type": "suck", "amount": -0.5},  # 負
            {"type": "jog", "axis": "x", "dist": 0.1},  # 機械操作（後段判定へ）
            {"type": "bogus"},  # 未知 type
            {},  # キー無し
        ],
    )
    def test_invalid_command_yields_none(self, command: dict[str, object]):
        assert parse_loading_command(command) is None


class TestProbeGndDownAdjust:
    """probe_gnd_down_adjust のプロンプトフロー（装置なし・test-fixture）."""

    def test_negative_distance_reprompts_then_send_failure_is_graceful(
        self,
        manager: JobManager,
        state: AppState,
        wait_until: WaitUntil,
    ):
        """負数 → 再 prompt、正数 → down 送信失敗で FAILED + down(0) 失敗 log.

        - 初回 prompt は number、default = machine.probe.down_distance（2.0）
        - 終了時の down(0) best-effort 失敗 log は「ダウン距離」を含む（契約）
        - relax (M84) 失敗警告 + 排他ロック解放
        """
        record = manager.start("probe_gnd_down_adjust", {})
        answered: set[str] = set()

        wait_until(lambda: record.pending_prompt is not None)
        pending = record.pending_prompt
        assert pending is not None
        assert pending[1].kind == "number"
        assert pending[1].default == 2.0  # test-fixture の probe.down_distance

        # 負数は受理されず log + 再 prompt（新 id）
        _answer_next_prompt(record, manager, wait_until, -1.0, answered)
        _answer_next_prompt(record, manager, wait_until, 1.5, answered)
        wait_until(lambda: record.status.terminal, timeout=60.0)
        wait_until(lambda: state.busy_owner is None)

        assert len(answered) == 2
        assert record.status == JobStatus.FAILED
        assert record.error  # 接続エラーが error に載る
        log_text = "\n".join(record.log_lines)
        assert "ダウン距離" in log_text  # finally の down(0) 復帰失敗警告
        assert "M84" in log_text  # relax 失敗警告（manager 経由）
        with state.machine_lock("after-failed-job"):  # ロックは解放済み
            pass

    def test_abort_while_waiting_prompt_aborts_and_attempts_down_zero(
        self, manager: JobManager, state: AppState, wait_until: WaitUntil
    ):
        """Prompt 待ちの abort → ABORTED。finally の down(0) は abort でも実行."""
        record = manager.start("probe_gnd_down_adjust", {})
        wait_until(lambda: record.status == JobStatus.WAITING_INPUT)

        assert manager.request_abort() is True

        wait_until(lambda: record.status.terminal, timeout=60.0)
        wait_until(lambda: state.busy_owner is None)
        assert record.status == JobStatus.ABORTED
        assert record.apply_available is False
        assert "ダウン距離" in "\n".join(record.log_lines)


class TestHeightPlaneFrontFlow:
    """height_plane の計測前フロー（装置なし・FakeCamera + test-fixture）.

    fill_coverage は TOP 銅箔ゾーンを持たないため、サンプリング可能な led_blinker
    fixture（copper_pcb_path）を使う。
    """

    def test_planned_points_artifact_and_confirm_false_aborts(
        self,
        manager: JobManager,
        state: AppState,
        fake_camera_settings: Settings,
        copper_pcb_path: Path,
        wait_until: WaitUntil,
    ):
        """計画点 PNG 生成 + diagnostics / /artifacts/ log → confirm False で中止.

        - confirm 待ちの時点で artifacts_dir/planned_points.png が生成済みで
          cv2 で復号可能（実行中配信の前提。計画書「_run_height_plane」節 2-4）
        - prompt は confirm（default True）でメッセージに点数の確認を含む
        """
        state.select_pcb(copper_pcb_path)
        record = manager.start("height_plane", {})
        # matplotlib の初回描画があるため長めに待つ
        wait_until(lambda: record.pending_prompt is not None, timeout=60.0)

        pending = record.pending_prompt
        assert pending is not None
        prompt_id, spec = pending
        assert spec.kind == "confirm"
        assert spec.default is True
        assert "計測します" in spec.message

        png_path = (
            fake_camera_settings.data_dir / "webui" / record.id / "planned_points.png"
        )
        image = cv2.imread(str(png_path))
        assert image is not None
        assert image.size > 0

        log_text = "\n".join(record.log_lines)
        assert "/artifacts/" in log_text  # 計画点 PNG へのリンク行
        assert "planned_points.png" in log_text
        assert "min_clearance" in log_text  # sampling diagnostics の log

        manager.respond_prompt(prompt_id, False)
        wait_until(lambda: record.status.terminal, timeout=60.0)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.ABORTED
        assert record.apply_available is False

    def test_confirm_true_fails_gracefully_without_klipper(
        self,
        manager: JobManager,
        state: AppState,
        copper_pcb_path: Path,
        wait_until: WaitUntil,
    ):
        """Confirm True → セットアップで Klipper 不通 → FAILED + ロック解放."""
        state.select_pcb(copper_pcb_path)
        record = manager.start("height_plane", {})
        answered: set[str] = set()
        _answer_next_prompt(record, manager, wait_until, True, answered)
        wait_until(lambda: record.status.terminal, timeout=60.0)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.FAILED
        assert record.error
        assert "M84" in "\n".join(record.log_lines)
        with state.machine_lock("after-failed-job"):
            pass


class TestMachineJobsWithoutKlipper:
    """残り 4 ジョブの graceful FAILED（test-fixture: port 7126 = 接続拒否）.

    実動作（押出・塗布・SUCCEEDED 到達）は実機区分でカバーする分担（計画書 §4）。
    """

    @pytest.mark.parametrize(
        ("name", "needs_pcb"),
        [
            ("paste_solder", True),
            ("toolhead_offset", True),
            ("loading", False),
            ("flow_calibration", False),
        ],
    )
    def test_job_fails_gracefully_and_releases_lock(
        self,
        manager: JobManager,
        state: AppState,
        real_pcb_path: Path,
        wait_until: WaitUntil,
        name: str,
        needs_pcb: bool,
    ):
        if needs_pcb:
            state.select_pcb(real_pcb_path)
        record = manager.start(name, {})
        wait_until(lambda: record.status.terminal, timeout=60.0)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.FAILED
        assert record.error  # 接続エラーが error に載る
        assert "M84" in "\n".join(record.log_lines)  # relax 失敗警告
        with state.machine_lock("after-failed-job"):  # ロックは解放済み
            pass

    @pytest.mark.parametrize(
        "name", ["paste_solder", "height_plane", "toolhead_offset"]
    )
    def test_pcb_job_without_selection_raises_value_error(
        self, manager: JobManager, name: str
    ):
        """requires_pcb=True: PCB 未選択は開始前に ValueError（→ 400）."""
        with pytest.raises(ValueError):
            manager.start(name, {})


class TestApplyTargetsWhitelisted:
    """Apply 反映先 3 キーが config_store ホワイトリストに登録済みであること.

    計画書 前提:「paste_dispenser.rotations_per_ul /
    paste_dispenser.toolhead.x,y / probe.down_distance は MACHINE_FIELDS
    に登録済み」。SUCCEEDED は実機でしか 作れないため、書込経路の成立をここでピンする。
    """

    def test_pasting_apply_keys_write_to_machine_toml(
        self, store: ConfigStore, configs_root: Path
    ):
        store.write_machine_settings(
            "test-fixture",
            {
                "paste_dispenser.rotations_per_ul": 12.345678,
                "paste_dispenser.toolhead.x": -1.2345,
                "paste_dispenser.toolhead.y": 23.4567,
                "probe.down_distance": 1.234,
            },
        )

        toml_text = (configs_root / "test-fixture" / "machine.toml").read_text(
            encoding="utf-8"
        )
        assert "12.345678" in toml_text
        assert "-1.2345" in toml_text
        assert "23.4567" in toml_text
        assert "1.234" in toml_text


@pytest.fixture
def real_state(real_settings: Settings) -> Iterator[AppState]:
    """実機（実 Moonraker, kurousagi）向け AppState。`@mark_hardware` 専用."""
    state = AppState(real_settings, ConfigStore(real_settings.configs_root))
    yield state
    state.close()


@pytest.fixture
def real_manager(
    real_state: AppState, real_settings: Settings, catalog: JobCatalog
) -> Iterator[JobManager]:
    manager = JobManager(real_state, PreviewService(real_state), catalog, real_settings)
    yield manager
    manager.shutdown(timeout=60.0)


def _wait_loading_stage_and_settle(
    record: JobRecord, wait_until: WaitUntil, *, timeout: float = 120.0
) -> None:
    """ローディング段階入りを待ち、入口の滞留コマンド drain を確実に越える.

    計画書「_run_loading_loop」節: progress(LOADING_STAGE) の直後に滞留コマンドを drain
    するため、stage 表示直後の submit は破棄され得る。実機テストでは短い settle を挟んでから submit
    する（アサートには使わない）。
    """
    wait_until(lambda: record.progress_stage == LOADING_STAGE, timeout=timeout)
    time.sleep(1.0)


@mark_hardware
class TestPastingHardware:
    """実機通し（実 Moonraker、configs/kurousagi）。ユーザー実行.

    前提（計画書 §5「ユーザーへ引き継ぐ実機確認項目」1・2・4・5）:

    - Moonraker が localhost:7125 で稼働し、各軸がホーミング可能であること
    - ペーストディスペンサーが装着・ペースト充填可能であること
    - height_plane: data/testing/led_blinker の基板がステージにセットされ、
      実カメラがキャリブレーション済みであること
    - paste_solder / toolhead_offset の通しはプレビュー目視を伴うため WebUI
      手動 E2E（計画書 §5 引き継ぎ 3・6）で確認する
    """

    def test_loading_extrude_then_finish_succeeds(
        self, real_manager: JobManager, wait_until: WaitUntil
    ):
        """押出 → 終了で SUCCEEDED、summary に押出合計 [uL] が載る."""
        record = real_manager.start("loading", {"amount": 0.1})
        _wait_loading_stage_and_settle(record, wait_until)

        real_manager.submit_command({"type": "extrude", "amount": 0.05})
        real_manager.submit_command({"type": "finish"})
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "押出合計" in result.summary
        assert "uL" in result.summary
        assert result.apply is None  # loading に Apply はない

    def test_loading_accepts_machine_commands_in_loop(
        self, real_manager: JobManager, wait_until: WaitUntil
    ):
        """ローディング中もマシン操作（jog）を受け付ける（machine_commands 共有）."""
        record = real_manager.start("loading", {"amount": 0.1})
        _wait_loading_stage_and_settle(record, wait_until)

        real_manager.submit_command({"type": "jog", "axis": "z", "dist": 0.1})
        real_manager.submit_command({"type": "finish"})
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED

    def test_probe_gnd_down_adjust_confirm_yields_apply(
        self, real_manager: JobManager, wait_until: WaitUntil
    ):
        """距離入力 → 確定 confirm → SUCCEEDED + [probe].down_distance の Apply."""
        record = real_manager.start("probe_gnd_down_adjust", {})
        answered: set[str] = set()
        _answer_next_prompt(record, real_manager, wait_until, 0.5, answered)
        # 確定 confirm（default False）に True
        _answer_next_prompt(record, real_manager, wait_until, True, answered)
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "down_distance" in result.summary
        assert result.apply is not None
        assert result.apply.values == {"probe.down_distance": 0.5}

    def test_flow_calibration_full_run_yields_rotations_per_ul(
        self, real_manager: JobManager, wait_until: WaitUntil
    ):
        """充填 finish → タール confirm → 回転 → 質量/比重入力 → Apply payload.

        質量はダミー値（50mg）で応答する。Apply の妥当値確認は実運用で行う。
        """
        record = real_manager.start(
            "flow_calibration",
            {"rotations": 1.0, "rate": 1.0, "accel": 10.0, "load_amount": 0.05},
        )
        _wait_loading_stage_and_settle(record, wait_until)
        real_manager.submit_command({"type": "finish"})

        answered: set[str] = set()
        # タール confirm → True / 質量 (mg) → 50 / 比重 → 1.0
        _answer_next_prompt(
            record, real_manager, wait_until, True, answered, timeout=120.0
        )
        _answer_next_prompt(
            record, real_manager, wait_until, 50.0, answered, timeout=300.0
        )
        _answer_next_prompt(
            record, real_manager, wait_until, 1.0, answered, timeout=120.0
        )
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "rotations_per_ul" in result.summary
        assert result.apply is not None
        assert set(result.apply.values) == {"paste_dispenser.rotations_per_ul"}
        value = result.apply.values["paste_dispenser.rotations_per_ul"]
        assert isinstance(value, float)
        assert value > 0.0

    def test_flow_calibration_tare_confirm_false_aborts(
        self, real_manager: JobManager, wait_until: WaitUntil
    ):
        """タール confirm に「いいえ」→ 回転前に ABORTED（判断保留点 7 の中止口）."""
        record = real_manager.start(
            "flow_calibration",
            {"rotations": 1.0, "rate": 1.0, "accel": 10.0, "load_amount": 0.05},
        )
        _wait_loading_stage_and_settle(record, wait_until)
        real_manager.submit_command({"type": "finish"})

        answered: set[str] = set()
        _answer_next_prompt(
            record, real_manager, wait_until, False, answered, timeout=120.0
        )
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.ABORTED
        assert record.apply_available is False

    def test_height_plane_full_run_yields_heatmap_artifacts(
        self,
        real_manager: JobManager,
        real_state: AppState,
        real_settings: Settings,
        wait_until: WaitUntil,
    ):
        """Confirm True → 実計測 → planned_points + height_plane の PNG
        artifacts."""
        real_state.select_pcb(Path("data/testing/led_blinker/led_blinker.kicad_pcb"))
        record = real_manager.start("height_plane", {})
        answered: set[str] = set()
        _answer_next_prompt(record, real_manager, wait_until, True, answered)
        wait_until(lambda: record.status.terminal, timeout=900.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "点計測" in result.summary
        labels = {artifact.label for artifact in result.artifacts}
        assert labels == {"計測予定点", "ヒートマップ"}
        artifacts_root = real_settings.data_dir / "webui"
        for artifact in result.artifacts:
            assert artifact.kind == "image"
            image = cv2.imread(str(artifacts_root / artifact.path))
            assert image is not None
            assert image.size > 0
        assert result.apply is None  # height_plane に Apply はない
