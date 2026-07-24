"""`webui.jobs.pasting` の仕様テスト.

計画書 memory/agents/implementation-planner/webui-phase5.md「src/webui/jobs/pasting.py」節
+ spec §10 pasting 表が契約:

- catalog: pasting 6 ジョブ（paste_solder / height_plane / loading /
  dispense_calibration / generate_rect_pcb / toolhead_offset）の name /
  params（default・unit）/ requires_pcb / uses_machine / accepts_commands
- parse_loading_command: extrude / suck / finish の純粋パーサ。ローディング用
  type の値不正（欠落・非正・非数）は InvalidLoadingCommand(reason)、
  未知 type は None（機械操作の後段判定へ）
- height_plane: 計測前に planned_points.png を artifacts へ生成し、
  diagnostics と /artifacts/ リンクを log してから confirm を挟む。
  confirm False → ABORTED / True → Klipper 不通（setup）で FAILED
- 装置ジョブの異常系: test-fixture（Klipper port 7126 = 接続拒否）で graceful
  FAILED + PRESENT / relax (M84) 失敗警告 + 排他ロック解放
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
from pathlib import Path

import cv2
import pytest

from pcbasm.hal import XYZStage
from pcbasm.pcb import PcbFile
from tests.helpers import mark_hardware
from tests.webui.conftest import decode_jpeg, jpeg_payload
from webui.config_store import ConfigStore
from webui.jobs.catalog import JobCatalog, default_catalog
from webui.jobs.machine_commands import create_command_klipper
from webui.jobs.manager import JobManager, JobRecord, JobStatus
from webui.jobs.pasting import (
    LOADING_STAGE,
    Extrude,
    Finish,
    InvalidLoadingCommand,
    Rotate,
    parse_loading_command,
    parse_run_calib_command,
    register_pasting_jobs,
)
from webui.preview import PreviewService
from webui.settings import Settings
from webui.state import AppState

from .conftest import WaitUntil, answer_next_prompt

PASTING_JOBS = (
    "paste_solder",
    "height_plane",
    "loading",
    "dispense_calibration",
    "generate_rect_pcb",
    "toolhead_offset",
)


@pytest.fixture
def catalog() -> JobCatalog:
    """Pasting 7 ジョブのみ登録した catalog（jobs/conftest の manager が使う）."""
    catalog = JobCatalog()
    register_pasting_jobs(catalog)
    return catalog


def _preview_frame(preview: PreviewService):
    """プレビュー MJPEG の 1 フレームを公開経路から取得する."""
    stream = preview.mjpeg_stream("none")
    try:
        frame = decode_jpeg(jpeg_payload(next(stream)))
    finally:
        stream.close()
    assert frame is not None
    return frame


class TestCatalog:
    """default_catalog への pasting ジョブ登録（計画書「ジョブ定義表」のピン）."""

    @pytest.fixture
    def default(self) -> JobCatalog:
        return default_catalog()

    def test_pasting_tab_has_exactly_phase5_jobs(self, default: JobCatalog):
        names = {definition.name for definition in default.list(tab="pasting")}

        assert names == set(PASTING_JOBS)

    @pytest.mark.parametrize(
        ("name", "requires_pcb", "uses_machine", "accepts_commands"),
        [
            ("paste_solder", True, True, True),
            ("height_plane", True, True, False),
            ("loading", False, True, True),
            ("dispense_calibration", False, True, True),
            ("generate_rect_pcb", False, False, False),
            ("toolhead_offset", True, True, True),
        ],
    )
    def test_job_flags(
        self,
        default: JobCatalog,
        name: str,
        requires_pcb: bool,
        uses_machine: bool,
        accepts_commands: bool,
    ):
        definition = default.get(name)

        assert definition.requires_pcb is requires_pcb
        assert definition.uses_machine is uses_machine
        assert definition.accepts_commands is accepts_commands

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            (
                "paste_solder",
                {"tolerance": (0.1, "mm"), "amount": (0.1, "uL")},
            ),
            ("height_plane", {"tolerance": (0.1, "mm")}),
            (
                "loading",
                {
                    "amount": (0.1, "uL"),
                    "rotations": (5.0, "rev"),
                    "rate": (0.5, "rev/s"),
                    "accel": (0.5, "rev/s^2"),
                    "retract_rotations": (0.0, "rev"),
                },
            ),
            (
                "dispense_calibration",
                {
                    "board_width": (40.0, "mm"),
                    "board_height": (40.0, "mm"),
                    "tolerance": (0.1, "mm"),
                    "line_length": (10.0, "mm"),
                    "line_amount": (0.5, "uL"),
                    "row_pitch": (3.0, "mm"),
                    "removal_z_offset": (0.0, "mm"),
                    "rate_min": (0.5, "uL/s"),
                    "rate_max": (5.0, "uL/s"),
                    "speed_min": (1.0, "mm/s"),
                    "speed_max": (10.0, "mm/s"),
                },
            ),
            ("generate_rect_pcb", {"width": (40.0, "mm"), "height": (40.0, "mm")}),
            (
                "toolhead_offset",
                {
                    "tolerance": (0.1, "mm"),
                    "dispense_amount": (0.1, "uL"),
                    "loading_amount": (0.1, "uL"),
                    "lift_height": (5.0, "mm"),
                    "paste_diameter_min": (0.0, "mm"),
                    "paste_diameter_max": (2.0, "mm"),
                    "point_spacing": (5.0, "mm"),
                    "edge_margin": (5.0, "mm"),
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
            key: spec
            for key, spec in params.items()
            if spec.value_type == "float" and not spec.optional
        }

        assert set(float_params) == set(expected)
        for key, (value, unit) in expected.items():
            assert float_params[key].default == value, key
            assert float_params[key].unit == unit, key

    @pytest.mark.parametrize(
        ("name", "default_value"),
        [
            ("line_count", 10),
            ("rate_divisions", 6),
            ("speed_divisions", 6),
        ],
    )
    def test_dispense_calibration_int_params(
        self, default: JobCatalog, name: str, default_value: int
    ):
        params = {
            spec.name: spec for spec in default.get("dispense_calibration").params
        }

        assert params[name].value_type == "int"
        assert params[name].default == default_value

    def test_toolhead_offset_point_count_defaults_to_ten_with_minimum_five(
        self, default: JobCatalog
    ):
        params = {spec.name: spec for spec in default.get("toolhead_offset").params}

        assert params["point_count"].value_type == "int"
        assert params["point_count"].default == 10
        assert params["point_count"].minimum == 5

    def test_toolhead_offset_rejects_point_count_below_five(self, default: JobCatalog):
        definition = default.get("toolhead_offset")

        with pytest.raises(ValueError) as exc_info:
            default.validate_params(definition, {"point_count": 4})

        assert "point_count" in str(exc_info.value)

    def test_toolhead_offset_persists_all_params(self, default: JobCatalog):
        definition = default.get("toolhead_offset")

        assert len(definition.params) == 9
        assert set(definition.persisted_params) == {
            "tolerance",
            "dispense_amount",
            "loading_amount",
            "lift_height",
            "paste_diameter_min",
            "paste_diameter_max",
            "point_count",
            "point_spacing",
            "edge_margin",
        }
        assert set(definition.persisted_params) == {
            spec.name for spec in definition.params
        }

    def test_dispense_calibration_persists_all_calibration_params(
        self, default: JobCatalog
    ):
        definition = default.get("dispense_calibration")

        # 土台/線共通/②/③ の入力パラメータを次回フォーム既定値として保存する
        # （比重は machine.toml 参照のためフォーム入力から除外済み）
        assert definition.persisted_params == (
            "board_width",
            "board_height",
            "tolerance",
            "line_length",
            "line_count",
            "line_amount",
            "row_pitch",
            "removal_z_offset",
            "rate_min",
            "rate_max",
            "rate_divisions",
            "speed_min",
            "speed_max",
            "speed_divisions",
        )

    def test_dispense_calibration_runtime_editable_split(self, default: JobCatalog):
        definition = default.get("dispense_calibration")
        params = {spec.name: spec for spec in definition.params}

        # 銅板は開始時に 1 回生成するため board 寸法と許容誤差はキャリブ後固定。
        for fixed in ("board_width", "board_height", "tolerance"):
            assert params[fixed].runtime_editable is False

        # 線設定・計量退避・②③ のスケジュールは実行中に変更可。
        assert definition.runtime_params == (
            "line_length",
            "line_count",
            "line_amount",
            "row_pitch",
            "removal_z_offset",
            "rate_min",
            "rate_max",
            "rate_divisions",
            "speed_min",
            "speed_max",
            "speed_divisions",
        )

    def test_dispense_calibration_removal_z_offset_default(self, default: JobCatalog):
        params = {
            spec.name: spec for spec in default.get("dispense_calibration").params
        }

        offset = params["removal_z_offset"]
        assert offset.value_type == "float"
        assert offset.default == 0.0
        assert offset.runtime_editable is True

    def test_loading_persists_volume_and_rotation_params(self, default: JobCatalog):
        definition = default.get("loading")

        assert definition.persisted_params == (
            "amount",
            "rotations",
            "rate",
            "accel",
            "retract_rotations",
        )

    def test_loading_position_axes_are_optional_and_not_persisted(
        self, default: JobCatalog
    ):
        definition = default.get("loading")
        params = {spec.name: spec for spec in definition.params}

        for name in ("position_x", "position_y", "position_z"):
            assert params[name].value_type == "float"
            assert params[name].unit == "mm"
            assert params[name].optional is True
            assert name not in definition.persisted_params

        validated = default.validate_params(
            definition, {"position_x": 10, "position_z": 2.5}
        )
        assert validated["position_x"] == 10.0
        assert "position_y" not in validated
        assert validated["position_z"] == 2.5

    def test_paste_solder_interactive_loading_is_bool_defaulting_false(
        self, default: JobCatalog
    ):
        params = {spec.name: spec for spec in default.get("paste_solder").params}

        assert set(params) == {"tolerance", "amount", "interactive_loading"}
        assert params["interactive_loading"].value_type == "bool"
        assert params["interactive_loading"].default is False


class TestGenerateRectPcb:
    """generate_rect_pcb（実 pcbnew・装置非使用）。"""

    def test_output_is_readable_outline_only_pcb(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        record = manager.start("generate_rect_pcb", {"width": 50.0, "height": 20.0})
        # 実 pcbnew 読込は Raspberry Pi では数十秒かかり得る
        wait_until(lambda: record.status.terminal, timeout=120.0)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.result is not None
        outputs = [a for a in record.result.artifacts if a.path.endswith(".kicad_pcb")]
        assert len(outputs) == 1

        pcb = PcbFile(fake_camera_settings.webui_data_dir / outputs[0].path)
        assert pcb.outline.width == pytest.approx(50.0, abs=0.1)
        assert pcb.outline.height == pytest.approx(20.0, abs=0.1)
        assert len(pcb.pads) == 0
        assert len(pcb.copper) == 0


class TestParseLoadingCommand:
    """parse_loading_command（純粋関数）の契約.

    LOADING_STAGE 文字列のテンプレ/JS 整合は routers/test_pages.py の data-loading-
    stage アサートと e2e の DOM アサートが担保する。
    """

    def test_extrude_yields_positive_amount(self):
        assert parse_loading_command({"type": "extrude", "amount": 2.5}) == Extrude(2.5)

    def test_suck_yields_negative_amount(self):
        assert parse_loading_command({"type": "suck", "amount": 2.5}) == Extrude(-2.5)

    def test_extrude_rotations_yields_positive_rotation(self):
        # retract_rotations 欠落時は引き戻しなし（既定 0.0）。
        assert parse_loading_command(
            {"type": "extrude_rotations", "rotations": 5.0, "rate": 0.5, "accel": 0.5}
        ) == Rotate(5.0, 0.5, 0.5, retract_rotations=0.0)

    def test_extrude_rotations_carries_retract(self):
        assert parse_loading_command(
            {
                "type": "extrude_rotations",
                "rotations": 5.0,
                "rate": 0.5,
                "accel": 0.5,
                "retract_rotations": 1.5,
            }
        ) == Rotate(5.0, 0.5, 0.5, retract_rotations=1.5)

    def test_suck_rotations_yields_negative_rotation(self):
        assert parse_loading_command(
            {"type": "suck_rotations", "rotations": 5.0, "rate": 0.5, "accel": 0.5}
        ) == Rotate(-5.0, 0.5, 0.5)

    def test_suck_rotations_ignores_retract(self):
        assert parse_loading_command(
            {
                "type": "suck_rotations",
                "rotations": 5.0,
                "rate": 0.5,
                "accel": 0.5,
                "retract_rotations": 1.5,
            }
        ) == Rotate(-5.0, 0.5, 0.5, retract_rotations=0.0)

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
            {"type": "extrude_rotations", "rotations": 0, "rate": 0.5, "accel": 0.5},
            {"type": "extrude_rotations", "rotations": 5.0, "rate": 0, "accel": 0.5},
            {"type": "extrude_rotations", "rotations": 5.0, "rate": 0.5},  # accel 欠落
            {  # retract が負
                "type": "extrude_rotations",
                "rotations": 5.0,
                "rate": 0.5,
                "accel": 0.5,
                "retract_rotations": -1.0,
            },
            {"type": "suck_rotations", "rotations": "5", "rate": 0.5, "accel": 0.5},
        ],
    )
    def test_known_type_with_invalid_value_yields_reason(
        self, command: dict[str, object]
    ):
        """ローディング用 type の値不正は理由付き InvalidLoadingCommand になる."""
        result = parse_loading_command(command)

        assert isinstance(result, InvalidLoadingCommand)
        assert str(command["type"]) in result.reason

    def test_invalid_amount_reason_is_user_facing(self):
        result = parse_loading_command({"type": "extrude", "amount": -1.0})

        assert result == InvalidLoadingCommand("extrude の量には正の数値が必要です")

    def test_negative_retract_reason_mentions_non_negative(self):
        result = parse_loading_command(
            {
                "type": "extrude_rotations",
                "rotations": 5.0,
                "rate": 0.5,
                "accel": 0.5,
                "retract_rotations": -1.0,
            }
        )

        assert result == InvalidLoadingCommand(
            "extrude_rotations の引き戻し回転数には 0 以上の数値が必要です"
        )

    @pytest.mark.parametrize(
        "command",
        [
            {"type": "jog", "axis": "x", "dist": 0.1},  # 機械操作（後段判定へ）
            {"type": "bogus"},  # 未知 type
            {},  # キー無し
        ],
    )
    def test_non_loading_command_yields_none(self, command: dict[str, object]):
        assert parse_loading_command(command) is None


class TestParseRunCalibCommand:
    """parse_run_calib_command（純粋関数）の契約.

    CALIBRATION_MENU_STAGE 文字列のテンプレ/JS 整合は routers/test_pages.py の data-
    loading-stage アサートと e2e の DOM アサートが担保する。
    """

    @pytest.mark.parametrize(
        "which",
        ["rotations_per_ul", "max_dispense_rate", "max_fill_speed", "all", "finish"],
    )
    def test_known_which_returns_which(self, which: str):
        assert parse_run_calib_command({"type": "run_calib", "which": which}) == which

    @pytest.mark.parametrize(
        "command",
        [
            {"type": "run_calib"},  # which 欠落
            {"type": "run_calib", "which": "bogus"},  # 未知 which
            {"type": "run_calib", "which": 3},  # 非文字列
            {"type": "extrude", "amount": 1.0},  # 別 type
            {"type": "finish"},  # loading finish（run_calib ではない）
            {},  # キー無し
        ],
    )
    def test_invalid_command_yields_none(self, command: dict[str, object]):
        assert parse_run_calib_command(command) is None


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
            fake_camera_settings.webui_data_dir / record.id / "planned_points.png"
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

    def test_planned_points_preview_stays_visible_while_confirm_waits(
        self,
        catalog: JobCatalog,
        state: AppState,
        fake_camera_settings: Settings,
        copper_pcb_path: Path,
        wait_until: WaitUntil,
    ):
        """Confirm 待ち中は planned_points プレビューを TTL で生カメラに戻さない."""
        preview = PreviewService(state, override_ttl=0.0)
        manager = JobManager(state, preview, catalog, fake_camera_settings)
        state.select_pcb(copper_pcb_path)
        record = manager.start("height_plane", {})
        try:
            wait_until(lambda: record.pending_prompt is not None, timeout=60.0)

            png_path = (
                fake_camera_settings.webui_data_dir / record.id / "planned_points.png"
            )
            planned = cv2.imread(str(png_path))
            assert planned is not None
            frame = _preview_frame(preview)
            assert frame.shape == planned.shape
            assert cv2.absdiff(frame, planned).mean() < 5.0

            pending = record.pending_prompt
            assert pending is not None
            manager.respond_prompt(pending[0], False)
            wait_until(lambda: record.status.terminal, timeout=60.0)

            cleared = _preview_frame(preview)
            assert cleared.shape == (720, 1280, 3)
        finally:
            manager.shutdown()

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
        answer_next_prompt(record, manager, True, answered)
        wait_until(lambda: record.status.terminal, timeout=60.0)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.FAILED
        assert record.error
        assert "M84" in "\n".join(record.log_lines)
        with state.machine_lock("after-failed-job"):
            pass


class TestMachineJobsWithoutKlipper:
    """装置ジョブの graceful FAILED（test-fixture: port 7126 = 接続拒否）.

    実動作（押出・塗布・SUCCEEDED 到達）は実機区分でカバーする分担（計画書 §4）。
    """

    @pytest.mark.parametrize(
        ("name", "needs_pcb"),
        [
            ("paste_solder", True),
            ("toolhead_offset", True),
            ("loading", False),
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

    def test_loading_starts_with_homing_before_enabling_dispenser(
        self,
        manager: JobManager,
        state: AppState,
        wait_until: WaitUntil,
    ):
        record = manager.start("loading", {"position_x": 10.0})
        wait_until(lambda: record.status.terminal, timeout=60.0)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.FAILED
        assert record.progress_stage == "ホーミング", record.error
        assert "全軸ホーミングを実行します" in "\n".join(record.log_lines)

    def test_dispense_calibration_fails_gracefully_and_releases_lock(
        self,
        manager: JobManager,
        state: AppState,
        wait_until: WaitUntil,
    ):
        """共通土台でその場 PCB 生成 → カメラ確保 → ボード計測で Klipper 不通 → FAILED.

        PCB 選択不要（``requires_pcb=False``）。実 pcbnew での銅板生成と FakeCamera
        確保を越えてから接続拒否で落ちるため、長めに待つ。
        """
        record = manager.start("dispense_calibration", {})
        # 実 pcbnew 読込・FakeCamera 起動・接続リトライを含むため長め
        wait_until(lambda: record.status.terminal, timeout=180.0)
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
    """Apply 反映先キーが config_store ホワイトリストに登録済みであること.

    SUCCEEDED は一部実機でしか作れないため、書込経路の成立をここでピンする。
    """

    def test_pasting_apply_keys_write_to_machine_toml(
        self, store: ConfigStore, configs_root: Path
    ):
        store.write_machine_settings(
            "test-fixture",
            {
                "paste_dispenser.rotations_per_ul": 12.345678,
                "paste_dispenser.solder_paste_density": 3.78,
                "paste_dispenser.max_dispense_rate": 0.123456,
                "paste_dispenser.dispense_accel": 1.234567,
                "paste_dispenser.max_fill_speed": 7.654321,
                "paste_dispenser.toolhead.x": -1.2345,
                "paste_dispenser.toolhead.y": 23.4567,
            },
        )

        toml_text = (configs_root / "test-fixture" / "machine.toml").read_text(
            encoding="utf-8"
        )
        assert "12.345678" in toml_text
        assert "3.78" in toml_text
        assert "0.123456" in toml_text
        assert "1.234567" in toml_text
        assert "7.654321" in toml_text
        assert "-1.2345" in toml_text
        assert "23.4567" in toml_text


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

    def test_loading_homes_and_moves_to_specified_x_position(
        self,
        real_manager: JobManager,
        real_state: AppState,
        wait_until: WaitUntil,
    ):
        """ローディング開始で全軸 homing 後、指定した X へ移動する."""
        klipper = create_command_klipper(real_state.machine())
        stage = XYZStage(klipper.readonly)
        target_x = min(stage.limits.x.min + 1.0, stage.limits.x.max)

        record = real_manager.start("loading", {"position_x": target_x})
        _wait_loading_stage_and_settle(record, wait_until)
        position = stage.get_position()
        real_manager.submit_command({"type": "finish"})
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED
        assert position.x == pytest.approx(target_x)

    def test_loading_invalid_value_logs_reason_and_continues(
        self, real_manager: JobManager, wait_until: WaitUntil
    ):
        """不正値コマンドは押し出さず理由をログし、ループは継続する.

        検証（値不正 → InvalidLoadingCommand → ctx.log(reason)）はサーバに 一本化した（JS
        の正値チェックは削除済み）。ループ到達に実 Moonraker 接続（AirPump ON）が必要なため e2e
        ではなく実機区分でピンする。
        """
        record = real_manager.start("loading", {"amount": 0.1})
        _wait_loading_stage_and_settle(record, wait_until)

        real_manager.submit_command({"type": "extrude", "amount": -1.0})
        wait_until(
            lambda: any(
                "extrude の量には正の数値が必要です" in line
                for line in record.log_lines
            ),
            timeout=60.0,
        )
        real_manager.submit_command({"type": "finish"})
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "押出合計 +0.000 uL" in result.summary  # 不正値は押し出していない

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

    def test_loading_rotation_extrude_then_finish_succeeds(
        self, real_manager: JobManager, wait_until: WaitUntil
    ):
        """回転押出 → 終了で SUCCEEDED、summary に回転合計 [rev] が載る."""
        record = real_manager.start(
            "loading", {"rotations": 0.1, "rate": 0.5, "accel": 0.5}
        )
        _wait_loading_stage_and_settle(record, wait_until)

        real_manager.submit_command(
            {
                "type": "extrude_rotations",
                "rotations": 0.1,
                "rate": 0.5,
                "accel": 0.5,
            }
        )
        real_manager.submit_command({"type": "finish"})
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "回転合計" in result.summary
        assert "rev" in result.summary

    def test_loading_rotation_extrude_with_retract_nets_difference(
        self, real_manager: JobManager, wait_until: WaitUntil
    ):
        """回転押出に引き戻しが付随し、回転合計が押出−引き戻しの純増になる."""
        record = real_manager.start(
            "loading", {"rotations": 0.1, "rate": 0.5, "accel": 0.5}
        )
        _wait_loading_stage_and_settle(record, wait_until)

        real_manager.submit_command(
            {
                "type": "extrude_rotations",
                "rotations": 0.1,
                "rate": 0.5,
                "accel": 0.5,
                "retract_rotations": 0.05,
            }
        )
        real_manager.submit_command({"type": "finish"})
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        # 0.1 押出 − 0.05 引き戻し = 0.05 rev の純増
        assert "回転合計 +0.050 rev" in result.summary

    # 吐出量キャリブレーション統合ジョブ（dispense_calibration）の ①②③ 実測は
    # 実 Moonraker + 実カメラ + 実ペースト + 物理銅板の装着・計量を要する。手順が
    # 対話的（ボード計測 → 線引き → 質量/番号入力）でブラウザ目視を伴うため、
    # WebUI 手動 E2E（make webui-fake）でユーザーが検証する分担（large-refactor-workflow）。

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
        answer_next_prompt(record, real_manager, True, answered)
        wait_until(lambda: record.status.terminal, timeout=900.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "点計測" in result.summary
        labels = {artifact.label for artifact in result.artifacts}
        assert labels == {"計測予定点", "ヒートマップ"}
        artifacts_root = real_settings.webui_data_dir
        for artifact in result.artifacts:
            assert artifact.kind == "image"
            image = cv2.imread(str(artifacts_root / artifact.path))
            assert image is not None
            assert image.size > 0
        assert result.apply is None  # height_plane に Apply はない
