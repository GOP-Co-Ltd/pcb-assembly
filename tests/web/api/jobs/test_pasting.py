"""`web.api.jobs.pasting` の仕様テスト.

計画書 memory/agents/implementation-planner/webui-phase5.md「src/webui/jobs/pasting.py」節
+ spec §10 pasting 表が契約:

- catalog: pasting 7 ジョブ（paste_solder / height_plane / loading /
  dispense_calibration / paste_volume_calibration / generate_rect_pcb /
  toolhead_offset）の name / requires_pcb / uses_machine / accepts_commands と、
  params のうち振る舞いを持つもの（下限による拒否・optional・runtime_editable の
  切り分け）。宣言そのままの default / unit / persisted_params の列挙は
  `routers/test_jobs.py` が API 越しに汎用で担保する
- parse_loading_command: extrude / suck / finish の純粋パーサ。ローディング用
  type の値不正（欠落・非正・非数）は InvalidLoadingCommand(reason)、
  未知 type は None（機械操作の後段判定へ）
- height_plane: 計測前に planned_points.png を artifacts へ生成し、
  diagnostics と /artifacts/ リンクを log してから confirm を挟む。
  confirm False → ABORTED / True → Klipper 不通（setup）で FAILED
- 装置ジョブの異常系: テスト用 config（Klipper port 7126 = 接続拒否）で graceful
  FAILED + PRESENT / relax (M84) 失敗警告 + 排他ロック解放
- Apply 反映先キーが config_store のホワイトリストに載っていることは
  `test_config_store.py` が担保する（計画書 前提）

実押出・SUCCEEDED 到達・Apply 反映は実機区分（`@mark_hardware`、ユーザー実行。
paste_solder / toolhead_offset の実機通しはプレビュー目視を伴うため WebUI 手動
E2E（計画書 §5 引き継ぎ 3・6）に委ねる）。

cv2 / Moonraker / matplotlib / time.sleep のモックは使わない
（skill `testing-strategy`）。Klipper 不通は テスト用 config の実ポートへの
接続拒否で検証する。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import attrs
import cv2
import pytest

from pcbasm.gcode import GCode
from pcbasm.hal import XYZStage
from pcbasm.pasting.applicator import build_applicator
from pcbasm.pasting.dataset.reader import DatasetSession
from pcbasm.pasting.nozzle_clean import TRAVEL_Z, clean_nozzle
from pcbasm.pasting.paste_volume.calibration import (
    CALIBRATION_SUFFIX,
    list_calibrations,
    load_calibration,
    write_calibration,
)
from pcbasm.pasting.paste_volume.fit import fit_session
from pcbasm.pcb import PadHierarchy, PcbFile
from tests.helpers import (
    PROJECT_ROOT,
    FakeAudioPlayer,
    FakeKlipper,
    build_paste_volume_session,
    mark_hardware,
)
from tests.web.api.conftest import decode_jpeg, jpeg_payload
from web.api.board_settings import BoardSettingsStore
from web.api.jobs.catalog import JobCatalog, ParamSpec, default_catalog
from web.api.jobs.context import JobContext, JobResult
from web.api.jobs.machine_commands import create_command_klipper
from web.api.jobs.manager import JobManager, JobRecord, JobStatus
from web.api.jobs.pasting import (
    LOADING_STAGE,
    Extrude,
    Finish,
    InvalidLoadingCommand,
    Rotate,
    parse_loading_command,
    parse_run_calib_command,
    register_pasting_jobs,
)
from web.api.jobs.pasting.common import (
    LoadingTotals,
    prompt_positive_number,
    run_loading_loop,
)
from web.api.jobs.pasting.paste_volume_calibration import (
    fit_calibration,
    verify_with_calibration,
)
from web.api.jobs.pasting.paste_volume_common import (
    COLLECTED_SAVE_NAME_PARAM,
    DETECTION_PARAMS,
    REQUIRE_BLANK_ZERO_PARAM,
)
from web.api.preview import PreviewService
from web.api.settings import Settings
from web.api.state import AppState

from .conftest import (
    ManagerFactory,
    WaitUntil,
    answer_next_prompt,
    register_synthetic,
)

PASTING_JOBS = (
    "paste_solder",
    "height_plane",
    "loading",
    "dispense_calibration",
    "paste_volume_calibration",
    "paste_dataset_finalize",
    "paste_volume_refit",
    "generate_rect_pcb",
    "toolhead_offset",
)


@pytest.fixture
def catalog() -> JobCatalog:
    """Pasting ジョブのみ登録した catalog（jobs/conftest の manager が使う）."""
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
            ("paste_volume_calibration", False, True, True),
            ("paste_dataset_finalize", False, False, False),
            ("paste_volume_refit", False, False, False),
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

    def test_paste_volume_calibration_requires_a_paste_id(self, default: JobCatalog):
        """ペースト製品 ID はデータセットの素性なので既定値を持たず必須."""
        definition = default.get("paste_volume_calibration")

        with pytest.raises(ValueError, match="paste_id"):
            default.validate_params(definition, {})

    def test_paste_volume_calibration_drives_loading_from_the_form(
        self, default: JobCatalog
    ):
        """塗布パス先頭のローディングは体積・回転とも既定値をフォームから受ける.

        `loading_param` が無いとページがローディング操作 UI 自体を出さず、
        `run_loading_loop` の待ち受けへ運転者が応答できなくなる。
        """
        definition = default.get("paste_volume_calibration")
        params = {spec.name: spec for spec in definition.params}

        assert definition.loading_param == "loading_amount"
        assert definition.accepts_commands is True
        for name, unit in (
            ("loading_amount", "uL"),
            ("loading_rotations", "rev"),
            ("loading_rate", "rev/s"),
            ("loading_accel", "rev/s^2"),
            ("loading_retract_rotations", "rev"),
        ):
            assert params[name].unit == unit, name
            assert params[name].default is not None, name

    def test_toolhead_offset_paste_diameter_min_stays_positive(
        self, default: JobCatalog
    ):
        """最小直径 0（下限なし）は受けない（未塗布板のテクスチャを拾うため）."""
        definition = default.get("toolhead_offset")
        params = {spec.name: spec for spec in definition.params}

        # 実素材の未塗布板は 0.2 mm 相当の小片まで残る
        assert params["paste_diameter_min"].minimum == 0.3

        with pytest.raises(ValueError) as exc_info:
            default.validate_params(definition, {"paste_diameter_min": 0.2})

        assert "paste_diameter_min" in str(exc_info.value)
        assert "0.3 以上" in str(exc_info.value)

    def test_toolhead_offset_rejects_point_count_below_five(self, default: JobCatalog):
        definition = default.get("toolhead_offset")

        with pytest.raises(ValueError) as exc_info:
            default.validate_params(definition, {"point_count": 4})

        assert "point_count" in str(exc_info.value)

    def test_toolhead_offset_persists_all_params(self, default: JobCatalog):
        """全 params が次回フォーム既定値として保存される（保存漏れを検知する）."""
        definition = default.get("toolhead_offset")

        assert set(definition.persisted_params) == {
            spec.name for spec in definition.params
        }

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
    """generate_rect_pcb（実 pcbnew・装置非使用）."""

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

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ({"type": "extrude", "amount": 2.5}, Extrude(2.5)),
            # suck は押出の符号反転
            ({"type": "suck", "amount": 2.5}, Extrude(-2.5)),
            (
                # retract_rotations 欠落時は引き戻しなし（既定 0.0）
                {
                    "type": "extrude_rotations",
                    "rotations": 5.0,
                    "rate": 0.5,
                    "accel": 0.5,
                },
                Rotate(5.0, 0.5, 0.5, retract_rotations=0.0),
            ),
            (
                {
                    "type": "extrude_rotations",
                    "rotations": 5.0,
                    "rate": 0.5,
                    "accel": 0.5,
                    "retract_rotations": 1.5,
                },
                Rotate(5.0, 0.5, 0.5, retract_rotations=1.5),
            ),
            (
                {
                    "type": "suck_rotations",
                    "rotations": 5.0,
                    "rate": 0.5,
                    "accel": 0.5,
                },
                Rotate(-5.0, 0.5, 0.5),
            ),
            (
                # 吸い戻しに引き戻しは無い（retract は無視される）
                {
                    "type": "suck_rotations",
                    "rotations": 5.0,
                    "rate": 0.5,
                    "accel": 0.5,
                    "retract_rotations": 1.5,
                },
                Rotate(-5.0, 0.5, 0.5, retract_rotations=0.0),
            ),
            ({"type": "finish"}, Finish()),
        ],
    )
    def test_valid_command_yields_its_operation(
        self, command: dict[str, object], expected: object
    ):
        assert parse_loading_command(command) == expected

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
    """height_plane の計測前フロー（装置なし・FakeCamera + テスト用 config）.

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
        board_store: BoardSettingsStore,
        copper_pcb_path: Path,
        wait_until: WaitUntil,
    ):
        """Confirm 待ち中は planned_points プレビューを TTL で生カメラに戻さない."""
        preview = PreviewService(state, override_ttl=0.0)
        manager = JobManager(state, preview, catalog, fake_camera_settings, board_store)
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
    """装置ジョブの graceful FAILED（テスト用 config: port 7126 = 接続拒否）.

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


class TestPasteDatasetCollectionPreflight:
    """Dataset収集は装置を開く前に設定・配置・撮影窓を確定する（PCB非依存）."""

    @pytest.mark.parametrize(
        ("params", "expected"),
        [
            # 盤面に載らない本数（容量不足）
            (
                {
                    "plate_width": 12.0,
                    "plate_height": 12.0,
                    "volume_divisions": 5,
                    "samples_per_volume": 5,
                    "blank_count": 4,
                },
                "サンプル",
            ),
            # 同じ session で作った校正を同じ session へ当てると汎化を測れない
            (
                {
                    "save_name": f"self-reference{CALIBRATION_SUFFIX}",
                    "volume_calibration": f"self-reference{CALIBRATION_SUFFIX}",
                },
                "汎化",
            ),
            ({"paste_height": 0.0}, "塗布高さ"),
            ({"view_count": 4, "view_offset": 0.0}, "移動距離"),
            # crop がセルピッチを超えると隣のセルが写り込む
            ({"cell_size": 2.0, "cell_gap": 1.0, "crop_size": 4.0}, "crop"),
            # open_kernel_px は k×k バイトを確保する。下限・上限とも弾く
            ({"open_kernel_px": 4}, "openカーネル"),
            ({"open_kernel_px": 1_048_577}, "openカーネル"),
        ],
    )
    def test_invalid_settings_fail_before_prompt_or_machine(
        self,
        manager: JobManager,
        wait_until: WaitUntil,
        params: dict[str, object],
        expected: str,
    ):
        """1 時間の収集を終えてから設定の不正を知らされては遅い."""
        record = manager.start(
            "paste_volume_calibration", {"paste_id": "paste-1", **params}
        )
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert expected in record.error
        assert record.pending_prompt is None

    def test_a_zero_min_area_is_refused_at_start(self, manager: JobManager):
        """ParamSpec.minimum が受理前に弾く（ジョブは始まりもしない）."""
        with pytest.raises(ValueError, match="min_area_px"):
            manager.start(
                "paste_volume_calibration",
                {"paste_id": "paste-1", "min_area_px": 0},
            )

    def test_valid_settings_reach_confirmations_without_pcb(
        self, manager: JobManager, wait_until: WaitUntil
    ):
        record = manager.start(
            "paste_volume_calibration",
            {
                "paste_id": "paste-1",
                "paste_lot": "lot-1",
                "volume_divisions": 2,
                "samples_per_volume": 1,
                "blank_count": 1,
                "view_count": 1,
            },
        )

        wait_until(lambda: record.pending_prompt is not None, timeout=60.0)
        first = record.pending_prompt
        assert first is not None
        assert "吐出量キャリブレーション" in first[1].message
        manager.respond_prompt(first[0], True)

        wait_until(
            lambda: record.pending_prompt is not None
            and record.pending_prompt[0] != first[0],
            timeout=60.0,
        )
        second = record.pending_prompt
        assert second is not None
        assert "TARE" in second[1].message
        manager.respond_prompt(second[0], False)
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.ABORTED
        assert any("収集計画" in line for line in record.log_lines)


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
    """実機通し（実 Moonraker、実機 config/）。ユーザー実行.

    前提（計画書 §5「ユーザーへ引き継ぐ実機確認項目」1・2・4・5）:

    - Moonraker が localhost:7125 で稼働し、各軸がホーミング可能であること
    - ペーストディスペンサーが装着・ペースト充填可能であること
    - height_plane: data/testing/led_blinker の基板がステージにセットされ、
      実カメラがキャリブレーション済みであること
    - paste_solder の probe → XY 照合までは自動検証し、塗布開始前に中止する
    - paste_solder / toolhead_offset の完全な通しはプレビュー目視を伴うため
      WebUI 手動 E2E（計画書 §5 引き継ぎ 3・6）で確認する
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

    def test_nozzle_clean_motion_returns_to_travel_z(self, real_state: AppState):
        """ペーストを出さずにクリーニングのモーションと可動域だけを確認する.

        こすり効果（先端にペーストが残らないか）の確認は WebUI の手動 E2E に委ねる。
        """
        machine = real_state.machine()
        clean = machine.nozzle_clean
        if clean is None:
            pytest.skip("[nozzle_clean] が未記録のため実行しない")
        klipper = create_command_klipper(machine)
        stage = XYZStage(klipper.readonly)
        klipper.send_gcode(GCode.homing() + GCode.wait_for_done())

        with build_applicator(klipper, stage, machine.paste_dispenser) as applicator:
            clean_nozzle(klipper, stage, applicator, attrs.evolve(clean, purge_ul=0.0))

        assert stage.get_position().z == pytest.approx(TRAVEL_Z, abs=0.01)

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
    # WebUI 手動 E2E（make api-fake）でユーザーが検証する分担（large-refactor-workflow）。

    def test_paste_solder_probes_then_aligns_at_camera_focus_z(
        self,
        real_manager: JobManager,
        real_state: AppState,
        wait_until: WaitUntil,
    ):
        """Probe 後に camera focus Z で領域照合し、塗布前に中止する."""
        focus_z = real_state.focus_z()
        assert focus_z is not None
        stage = XYZStage(create_command_klipper(real_state.machine()).readonly)
        real_state.select_pcb(Path("data/testing/led_blinker/led_blinker.kicad_pcb"))
        record = real_manager.start("paste_solder", {})
        calibration_stages = {"高さ計測", "銅箔照合", "pad照合"}
        try:
            wait_until(
                lambda: record.progress_stage in calibration_stages,
                timeout=300.0,
            )
            assert record.progress_stage == "高さ計測"
            wait_until(lambda: record.progress_stage == "銅箔照合", timeout=900.0)
            wait_until(
                lambda: abs(stage.get_position().z - focus_z) <= 0.01,
                timeout=60.0,
            )
        finally:
            real_manager.request_abort()
            wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.ABORTED

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


class TestPasteDatasetFinalize:
    """未完了 dataset の救出（pending.json + 計量質量 → metadata.json）.

    収集の最後に WebUI が落ちて計量質量を入力できなかったセッションを、装置を
    使わずに確定させる。撮影済み画像を作り直さない（rename だけ）のが契約。
    """

    STEM = "plate-20x20-20260908T143052.123+0000"
    MEASURED_MASS_MG = 0.945

    def _incomplete_session(self, settings: Settings) -> Path:
        """pending.json と、それが参照する capture を持つ未完了 session を作る."""
        document = json.loads(
            (
                PROJECT_ROOT / "data/testing/schemas/paste_dataset_pending_v1.json"
            ).read_text(encoding="utf-8")
        )
        session = settings.paste_dataset_dir / f"{self.STEM}.incomplete"
        for phase in ("pre", "post"):
            (session / phase).mkdir(parents=True)
        for target in (*document["samples"], *document["blanks"]):
            for view in target["views"]:
                for phase in ("pre", "post"):
                    (session / view[phase]).write_bytes(b"")
        (session / "pending.json").write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        return session

    def test_finalizes_the_selected_session_without_touching_the_machine(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        incomplete = self._incomplete_session(fake_camera_settings)

        record = manager.start(
            "paste_dataset_finalize", {"measured_mass": self.MEASURED_MASS_MG}
        )
        answer_next_prompt(record, manager, incomplete.name, set())
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.SUCCEEDED, record.error
        session = fake_camera_settings.paste_dataset_dir / self.STEM
        assert not incomplete.exists()
        metadata = json.loads((session / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["schema_version"] == 3
        assert metadata["total"]["measured_mass_mg"] == self.MEASURED_MASS_MG
        # パージが無いので、塗布 sample だけで総体積を分け合う。
        assert metadata["samples"][0]["measured_volume_ul"] == pytest.approx(0.25)
        assert (session / "pre" / "000001.00.png").is_file()
        assert not (session / "pending.json").exists()

    def test_fails_when_no_session_can_be_rescued(
        self, manager: JobManager, wait_until: WaitUntil
    ):
        record = manager.start(
            "paste_dataset_finalize", {"measured_mass": self.MEASURED_MASS_MG}
        )
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert "未完了dataset" in record.error
        assert record.pending_prompt is None

    @pytest.mark.parametrize("mass", [0.0, -1.0])
    def test_rejects_non_positive_mass_before_prompting(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        mass: float,
    ):
        self._incomplete_session(fake_camera_settings)

        record = manager.start("paste_dataset_finalize", {"measured_mass": mass})
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.FAILED
        assert record.pending_prompt is None


class TestPasteVolumeCalibrationFitStep:
    """収集ジョブの最後で校正まで作る（`fit_calibration`）.

    運転者の目的は校正を作ることなので、収集の終わりに別ページへ移らせない。

    収集本体は装置を要求するので、合成ジョブへ同じ関数を通して確かめる。
    """

    STEM = "plate-47.5x20-20260909T145923.452+0900"

    @pytest.fixture
    def fit_manager(
        self, make_manager: ManagerFactory, catalog: JobCatalog
    ) -> JobManager:
        """`fit_calibration` だけを呼ぶ合成ジョブを積んだ manager."""

        def run(ctx: JobContext) -> JobResult:
            if bool(ctx.params["block_scatter"]):
                # savefig を失敗させる（path が directory なら書けない）
                (ctx.artifacts_dir / "paste_volume_calibration.png").mkdir(
                    parents=True, exist_ok=True
                )
            summary, artifacts = fit_calibration(
                ctx, Path(str(ctx.params["session_path"]))
            )
            return JobResult(summary=summary, artifacts=artifacts)

        register_synthetic(
            catalog,
            run,
            name="fit_calibration",
            params=(
                ParamSpec("session_path", "session", "str"),
                ParamSpec("block_scatter", "図を壊す", "bool", default=False),
                *DETECTION_PARAMS,
                REQUIRE_BLANK_ZERO_PARAM,
                COLLECTED_SAVE_NAME_PARAM,
            ),
        )
        return make_manager(catalog)

    def _run(
        self,
        manager: JobManager,
        wait_until: WaitUntil,
        session: Path,
        **overrides: object,
    ) -> JobRecord:
        record = manager.start(
            "fit_calibration", {"session_path": str(session), **overrides}
        )
        wait_until(lambda: record.status.terminal, timeout=60.0)
        return record

    def test_saves_a_calibration_without_a_second_page(
        self,
        fit_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        """校正ファイルと診断（総体積誤差・図）が収集ジョブの中で揃う."""
        session = build_paste_volume_session(
            fake_camera_settings.paste_dataset_dir / self.STEM
        )

        record = self._run(fit_manager, wait_until, session, save_name="collected")

        assert record.status == JobStatus.SUCCEEDED, record.error
        saved = list_calibrations(fake_camera_settings.paste_volume_calibration_dir)
        assert len(saved) == 1
        calibration, error = load_calibration(saved[0])
        assert error is None, error
        assert calibration is not None
        assert calibration.source.session == self.STEM

        assert record.result is not None
        assert "総体積誤差" in str(record.result.summary)
        assert {a.label for a in record.result.artifacts} >= {
            "直径と体積の散布図",
            "検出モンタージュ",
            "校正ファイル",
        }

    def test_an_empty_save_name_still_saves_under_an_auto_name(
        self,
        fit_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        """1 時間かけた実行の主成果物なので「保存しない」は選ばせない.

        名前を空にしたときはペースト・ノズル径・塗布高さと時刻から自動命名する。
        保存しない選択が要るのは `paste_volume_refit`（ハイパラ探索）の方。
        """
        session = build_paste_volume_session(
            fake_camera_settings.paste_dataset_dir / self.STEM
        )

        record = self._run(fit_manager, wait_until, session)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.result is not None
        assert "保存先" in str(record.result.summary)
        saved = list_calibrations(fake_camera_settings.paste_volume_calibration_dir)
        assert len(saved) == 1
        # metadata のペースト・ノズル径・塗布高さから名前を組む
        assert saved[0].name.startswith("paste-1-n0.34-h0.20-")

    def test_a_blank_false_positive_does_not_lose_the_collection(
        self,
        fit_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        """校正づくりが目的でも、1 時間の収集を FAILED で捨てない.

        ハイパラを変えて作り直すのは `paste_volume_refit` の役目。
        """
        session = build_paste_volume_session(
            fake_camera_settings.paste_dataset_dir / self.STEM,
            blank_material="large",
        )

        record = self._run(fit_manager, wait_until, session, save_name="broken")

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.result is not None
        assert record.result.summary == " / 校正生成失敗"
        assert record.result.artifacts == ()
        assert not fake_camera_settings.paste_volume_calibration_dir.exists()

    def test_a_figure_failure_still_saves_the_calibration(
        self,
        fit_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        """図は診断の補助。描けなくても校正は成果として残す.

        散布図の出力先を directory にして savefig を失敗させる。
        """
        session = build_paste_volume_session(
            fake_camera_settings.paste_dataset_dir / self.STEM
        )
        (fake_camera_settings.webui_data_dir / "jobs").mkdir(
            parents=True, exist_ok=True
        )

        record = self._run(
            fit_manager,
            wait_until,
            session,
            save_name="figures-broken",
            block_scatter=True,
        )

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.result is not None
        assert "診断図なし" in str(record.result.summary)
        assert "保存先" in str(record.result.summary)
        saved = list_calibrations(fake_camera_settings.paste_volume_calibration_dir)
        assert len(saved) == 1

    def test_an_unreadable_session_does_not_lose_the_collection(
        self,
        fit_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        record = self._run(
            fit_manager,
            wait_until,
            fake_camera_settings.paste_dataset_dir / "absent",
            save_name="broken",
        )

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.result is not None
        assert record.result.summary == " / 校正生成失敗"


class TestPasteDatasetVolumeVerification:
    """収集ジョブへ組み込んだ検証（`verify_with_calibration`）.

    検証の失敗で収集結果を失わないことが最優先の契約。

    この時点で 1 時間の収集と計量が終わっている。

    誤差の値そのものは test_evaluate.py が担保する。

    収集ジョブ本体は装置を要求するので、合成ジョブへ同じ関数を通して確かめる。
    """

    STEM = "plate-47.5x20-20260909T145923.452+0900"

    @pytest.fixture
    def verify_manager(
        self, make_manager: ManagerFactory, catalog: JobCatalog
    ) -> JobManager:
        """`verify_with_calibration` だけを呼ぶ合成ジョブを積んだ manager."""

        def run(ctx: JobContext) -> JobResult:
            summary, artifacts = verify_with_calibration(
                ctx, Path(str(ctx.params["session_path"]))
            )
            return JobResult(summary=summary, artifacts=artifacts)

        register_synthetic(
            catalog,
            run,
            name="verify_volume",
            params=(
                ParamSpec("session_path", "session", "str"),
                ParamSpec("volume_calibration", "校正", "str", default=""),
            ),
        )
        return make_manager(catalog)

    def _session(self, settings: Settings) -> Path:
        return build_paste_volume_session(settings.paste_dataset_dir / self.STEM)

    def _calibration(self, settings: Settings) -> str:
        """収集した session から校正を作って保存し、ファイル名を返す."""
        session, error = DatasetSession.load(settings.paste_dataset_dir / self.STEM)
        assert error is None, error
        assert session is not None
        fit, error = fit_session(session)
        assert error is None, error
        assert fit is not None
        name = f"verify{CALIBRATION_SUFFIX}"
        write_calibration(settings.paste_volume_calibration_dir / name, fit.calibration)
        return name

    def _run(
        self,
        manager: JobManager,
        wait_until: WaitUntil,
        session: Path,
        calibration: str,
    ) -> JobRecord:
        record = manager.start(
            "verify_volume",
            {"session_path": str(session), "volume_calibration": calibration},
        )
        wait_until(lambda: record.status.terminal, timeout=60.0)
        assert record.status == JobStatus.SUCCEEDED, record.error
        return record

    def test_verifies_the_collected_session_and_writes_the_report(
        self,
        verify_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        """総体積誤差の summary + JSON レポート + 散布図が成果物として揃う.

        blank と検出失敗はレポート上で別の数として持つ（同じ袋に入れると回帰が見えない）。
        """
        session = self._session(fake_camera_settings)
        name = self._calibration(fake_camera_settings)

        record = self._run(verify_manager, wait_until, session, name)

        assert record.result is not None
        assert "総体積誤差" in str(record.result.summary)
        by_label = {a.label: a for a in record.result.artifacts}
        assert set(by_label) == {"塗布量の検証", "実測と推定の散布図"}

        root = fake_camera_settings.webui_data_dir
        report = json.loads(
            (root / by_label["塗布量の検証"].path).read_text(encoding="utf-8")
        )
        assert report["session"] == self.STEM
        assert report["accepted_count"] > 0
        assert report["detection_failure_count"] == 0
        assert report["blank_count"] == 1
        assert report["dispensed_count"] == report["cell_count"] - 1
        assert cv2.imread(str(root / by_label["実測と推定の散布図"].path)) is not None

    def test_does_nothing_when_no_calibration_is_selected(
        self,
        verify_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        session = self._session(fake_camera_settings)

        record = self._run(verify_manager, wait_until, session, "")

        assert record.result is not None
        assert record.result.summary == ""
        assert record.result.artifacts == ()

    @pytest.mark.parametrize("broken", [False, True])
    def test_an_unusable_calibration_does_not_lose_the_collection(
        self,
        verify_manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
        broken: bool,
    ):
        """収集は終わっている。後付けの検証で例外を投げてはならない.

        校正が見つからない経路と、読めても parse に失敗する経路の両方。
        """
        session = self._session(fake_camera_settings)
        name = f"{'broken' if broken else 'absent'}{CALIBRATION_SUFFIX}"
        if broken:
            root = fake_camera_settings.paste_volume_calibration_dir
            root.mkdir(parents=True, exist_ok=True)
            (root / name).write_text("{ not json", encoding="utf-8")

        record = self._run(verify_manager, wait_until, session, name)

        assert record.result is not None
        assert record.result.summary == " / 検証失敗"
        assert record.result.artifacts == ()


class TestPasteVolumeRefit:
    """収集済み session から直径ベース校正を作る（装置不要）.

    判定とフィットは `pcbasm.pasting.paste_volume` にあり、ここが確かめるのは
    session の選択・成果物・保存の可否というジョブ層の契約。
    """

    STEM = "plate-47.5x20-20260909T145923.452+0900"

    def _session(self, settings: Settings, *, blank_material: str = "blank") -> Path:
        return build_paste_volume_session(
            settings.paste_dataset_dir / self.STEM, blank_material=blank_material
        )

    def _start(self, manager: JobManager, **overrides: object) -> JobRecord:
        return manager.start("paste_volume_refit", {**overrides})

    def test_fits_the_selected_session_and_writes_the_calibration(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        self._session(fake_camera_settings)

        record = self._start(manager, save_name="s3x70-n030-h020")
        answer_next_prompt(record, manager, self.STEM, set())
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.SUCCEEDED, record.error
        saved = list_calibrations(fake_camera_settings.paste_volume_calibration_dir)
        assert len(saved) == 1
        calibration, error = load_calibration(saved[0])
        assert error is None, error
        assert calibration is not None
        assert calibration.source.session == self.STEM
        assert calibration.diagnostics.blank_false_positive_count == 0
        assert calibration.diagnostics.detection_failure_count == 0

    def test_an_empty_save_name_still_reports_and_draws_the_diagnostics(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        """保存名なし = ハイパラ探索。図とまとめだけ出し、保存先は汚さない.

        主基準は session 総体積の誤差なので、まとめに必ず出す。
        """
        self._session(fake_camera_settings)

        record = self._start(manager)
        answer_next_prompt(record, manager, self.STEM, set())
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert not fake_camera_settings.paste_volume_calibration_dir.exists()

        assert record.result is not None
        assert record.result.summary is not None
        assert "総体積誤差" in record.result.summary
        # 散布図と検出モンタージュ
        artifacts_root = fake_camera_settings.webui_data_dir
        images = [a for a in record.result.artifacts if a.kind == "image"]
        assert len(images) == 2
        for artifact in images:
            image = cv2.imread(str(artifacts_root / artifact.path))
            assert image is not None
            assert image.size > 0

    def test_fails_when_a_blank_cell_is_detected_as_a_deposit(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        self._session(fake_camera_settings, blank_material="large")

        record = self._start(manager, save_name="broken")
        answer_next_prompt(record, manager, self.STEM, set())
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert "blank" in record.error
        assert not fake_camera_settings.paste_volume_calibration_dir.exists()

    def test_can_continue_past_a_blank_false_positive_on_request(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        self._session(fake_camera_settings, blank_material="large")

        record = self._start(manager, require_blank_zero=False)
        answer_next_prompt(record, manager, self.STEM, set())
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.SUCCEEDED, record.error

    def test_fails_when_no_completed_dataset_exists(
        self, manager: JobManager, wait_until: WaitUntil
    ):
        record = self._start(manager)
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert "完成dataset" in record.error
        assert record.pending_prompt is None

    def test_rejects_an_invalid_detection_hyperparameter_before_prompting(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        self._session(fake_camera_settings)

        record = self._start(manager, open_kernel_px=4)
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.FAILED
        assert record.pending_prompt is None


class TestPromptPositiveNumberNotification:
    """`prompt_positive_number` が応答待ちのたびに通知音を鳴らす.

    計量入力のように装置の前を離れた作業者を呼び戻す用途。非正の入力による再プロンプトでも 鳴らす（1
    度目を聞き逃した作業者を呼び直すため）。
    """

    def test_notifies_on_every_prompt_until_positive(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        player = FakeAudioPlayer()
        manager = make_manager(catalog, audio_player=player)
        answers: list[float | None] = []

        def run(ctx: JobContext) -> None:
            answers.append(prompt_positive_number(ctx, "質量 [mg]"))

        register_synthetic(catalog, run, name="mass_prompt")

        record = manager.start("mass_prompt", {})
        answered: set[str] = set()
        answer_next_prompt(record, manager, -1.0, answered)
        answer_next_prompt(record, manager, 110.5, answered)
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert answers == [110.5]
        assert [sound for sound, _ in player.played] == ["prompt", "prompt"]


class TestLoadingLoopNotification:
    """`run_loading_loop` がローディング待ちの入口で作業者を呼び戻す.

    手動ペーストローディング（`loading`）と、ローディング段階を持つキャリブ各ジョブが
    共有する入口。押出ボタンを押すたびではなく、段階に入った 1 回だけ鳴らす契約
    （押すたびに鳴ると、装置の前に居る作業者にはただの騒音になる）。

    実 `PasteApplicator` を `FakeKlipper` + 実 `XYZStage` に載せて回す。
    """

    def test_notifies_once_on_entry_and_not_per_command(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        player = FakeAudioPlayer()
        manager = make_manager(catalog, audio_player=player)
        klipper = FakeKlipper()
        stage = XYZStage(klipper.readonly)
        totals: list[LoadingTotals] = []

        def run(ctx: JobContext) -> None:
            with build_applicator(
                klipper, stage, ctx.machine.paste_dispenser
            ) as applicator:
                totals.append(run_loading_loop(ctx, klipper, stage, applicator))

        register_synthetic(catalog, run, name="loading_loop", accepts_commands=True)

        record = manager.start("loading_loop", {})
        # 入口の drain を越えた合図。これを待たずに submit すると破棄されうる
        wait_until(lambda: len(player.played) == 1)

        manager.submit_command({"type": "extrude", "amount": 0.5})
        wait_until(lambda: any("体積ローディング" in line for line in record.log_lines))
        manager.submit_command({"type": "finish"})
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert totals == [LoadingTotals(amount_ul=0.5, rotations=0.0)]
        # 押出コマンドを挟んでも入口の 1 回きり
        assert [sound for sound, _ in player.played] == ["prompt"]
