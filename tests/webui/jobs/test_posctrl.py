"""`webui.jobs.posctrl` の仕様テスト.

計画書 memory/agents/implementation-planner/webui-phase4.md
「src/webui/jobs/posctrl.py」節 + spec §10 posctrl 表が契約:

- catalog: posctrl 5 ジョブ（reference_point_setup / camera_calibration /
  board_tour / orthogonality_test / generate_grid_pcb）の name / requires_pcb /
  uses_machine / accepts_commands / params の default
- generate_grid_pcb: 出力 .kicad_pcb が PcbFile で読めて pad 数 = divisions^2
  （装置非使用。dev タブから位置合わせタブへ移設）
- camera_calibration: チェッカーボード FakeCamera でのフル結合（prompt 往復、
  artifacts、Apply payload、Klipper 不通での Z best-effort = z_position None）
- board_tour / orthogonality_test / reference_point_setup の異常系:
  test-fixture（Klipper port 7126 非リッスン）で graceful FAILED + ロック解放 +
  PRESENT / relax (M84) 失敗警告。成功系のステージ移動・照合は pcbasm テストと
  実機区分でカバーする分担（計画書 §4）
- 実機通し（実カメラ + 実 Klipper）は `@mark_hardware` でユーザー実行

計画書 memory/agents/implementation-planner/webui-camera-calib.md「公開インターフェース案」
「1. src/webui/jobs/posctrl.py」節が追加契約:

- camera_calibration の params は square_size のみ（default 1.5・persisted_params
  に含む）。crop_width / crop_height は削除され、実行時は machine.toml
  `[camera.crop]`（`ctx.machine.camera.crop.size`）を読む（真実は machine.toml
  に一本化。二重管理の回避）

「直行性テストを対話フローへ戻す」変更が `orthogonality_test` の追加契約:

- 巡回先は四隅（Top-Left / Top-Right / Bottom-Right / Bottom-Left）+ TOP 層 pad
  中心（`Grid {i+1}/{n}`、現在位置から machine 座標での nearest 順。点列は開始時に
  1 回だけ構築し周回間で固定）
- 各点で移動 → ラベル付きプレビュー → `confirm_next_point`（board_ops）の確認
  プロンプトを出し、ユーザー応答を待つ
- 「終了」（False）を受けた時点で **SUCCEEDED**（JobAborted は投げない）。summary
  に調整前の指標 + 巡回点数 + 周回数を含む
- 1 周し終えると四隅から再開し、「終了」ボタンか abort までずっと周回する

巡回本体（移動・プレビュー・周回）は装置なしでは検証できない。`setup_board` が
実 Klipper でのホーミングと基準点合わせを必須とし、test-fixture では最初の
prompt に到達する前に FAILED になるため。よって分担は:

- prompt の spec と True/False の意味 → `test_board_ops.py::TestConfirmNextPoint`
  （実 JobManager 経由の合成ジョブ）
- prompt 待機中 abort → `test_manager.py::TestPrompt`
- 巡回の通し（周回・「終了」で SUCCEEDED）→ 下の `@mark_hardware` 区分
- setup 段の graceful FAILED → `TestMachineJobsWithoutKlipper`（変更不要）

cv2 / Moonraker のモックは使わない（skill `testing-strategy`）。Klipper 不通は
test-fixture の実ポートへの接続拒否で検証する。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from pcbasm.pcb import PcbFile
from pcbasm.vision import CalibrationResult, Image
from tests.helpers import mark_hardware
from webui.config_store import ConfigStore
from webui.jobs.catalog import JobCatalog, default_catalog
from webui.jobs.manager import JobManager, JobStatus
from webui.jobs.posctrl import register_posctrl_jobs
from webui.preview import PreviewService
from webui.settings import Settings
from webui.state import AppState

from .conftest import WaitUntil, answer_next_prompt

POSCTRL_JOBS = (
    "reference_point_setup",
    "camera_calibration",
    "board_tour",
    "orthogonality_test",
    "generate_grid_pcb",
)

# checkerboard.png は 400x400・1 マス約 66.7px。square_size=10mm で
# pixel_per_mm ≈ 400/6/10 ≈ 6.67（tests/pcbasm/vision/test_calibration.py と
# 同一素材）。crop は machine.toml `[camera.crop]` 由来（checkerboard_state
# fixture が 400x400 に書き換える。既定 600 は 400px 画像をはみ出す）
CHECKERBOARD_PARAMS = {"square_size": 10.0}
CHECKERBOARD_CROP_SIZE = (400, 400)
CHECKERBOARD_PIXEL_PER_MM = 400 / 6 / 10


@pytest.fixture
def catalog() -> JobCatalog:
    """Posctrl 5 ジョブのみ登録した catalog（jobs/conftest の manager が使う）."""
    catalog = JobCatalog()
    register_posctrl_jobs(catalog)
    return catalog


@pytest.fixture
def checkerboard_state(
    checkerboard_camera_settings: Settings, store: ConfigStore
) -> Iterator[AppState]:
    # checkerboard.png は 400x400。既定 crop 600 は画像をはみ出すため、
    # tmp コピーの machine.toml へ 400x400 を書いてから AppState を作る
    # （crop は machine.toml `[camera.crop]` から読まれる契約。要確認事項 a）
    store.write_machine_settings(
        "kurousagi", {"camera.crop.width": 400, "camera.crop.height": 400}
    )
    state = AppState(checkerboard_camera_settings, store)
    yield state
    state.close()


@pytest.fixture
def checkerboard_manager(
    checkerboard_state: AppState,
    checkerboard_camera_settings: Settings,
    catalog: JobCatalog,
) -> Iterator[JobManager]:
    manager = JobManager(
        checkerboard_state,
        PreviewService(checkerboard_state),
        catalog,
        checkerboard_camera_settings,
    )
    yield manager
    manager.shutdown()


class TestCatalog:
    """default_catalog への posctrl 5 ジョブ登録（計画書「ジョブ定義表」のピン）."""

    @pytest.fixture
    def default(self) -> JobCatalog:
        return default_catalog()

    def test_posctrl_tab_has_exactly_phase4_jobs(self, default: JobCatalog):
        names = {definition.name for definition in default.list(tab="posctrl")}

        assert names == set(POSCTRL_JOBS)

    @pytest.mark.parametrize(
        ("name", "requires_pcb", "uses_machine", "accepts_commands"),
        [
            ("reference_point_setup", False, True, True),
            ("camera_calibration", False, True, False),
            ("board_tour", True, True, False),
            ("orthogonality_test", True, True, False),
            # 装置を使わない生成ジョブ（dev タブから移設）
            ("generate_grid_pcb", False, False, False),
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

    def test_reference_point_setup_has_no_params(self, default: JobCatalog):
        assert default.get("reference_point_setup").params == ()

    def test_camera_calibration_params(self, default: JobCatalog):
        """Square_size のみが params。crop は machine.toml 連動で params から削除済み.

        default 1.5（旧: 必須空欄）+ persisted_params に square_size を含む
        （入力途中の即保存対象。計画書「要確認事項 1」採用）。
        """
        definition = default.get("camera_calibration")
        params = {spec.name: spec for spec in definition.params}

        assert set(params) == {"square_size"}
        assert params["square_size"].value_type == "float"
        assert params["square_size"].default == 1.5
        assert definition.persisted_params == ("square_size",)

    @pytest.mark.parametrize("name", ["board_tour", "orthogonality_test"])
    def test_tolerance_param_defaults(self, default: JobCatalog, name: str):
        params = {spec.name: spec for spec in default.get(name).params}

        assert set(params) == {"tolerance"}
        assert params["tolerance"].value_type == "float"
        assert params["tolerance"].default == 0.1

    def test_generate_grid_pcb_params(self, default: JobCatalog):
        params = {spec.name: spec for spec in default.get("generate_grid_pcb").params}

        assert set(params) == {"size", "divisions", "pad_size"}
        assert params["size"].value_type == "float"
        assert params["divisions"].value_type == "int"
        assert params["pad_size"].value_type == "float"


class TestGenerateGridPcb:
    """generate_grid_pcb（実 pcbnew・装置非使用。dev タブから移設）."""

    def test_output_is_readable_with_divisions_squared_pads(
        self,
        manager: JobManager,
        fake_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        record = manager.start(
            "generate_grid_pcb",
            {"size": 30.0, "divisions": 2, "pad_size": 1.0},
        )
        # 実 pcbnew 読込は Raspberry Pi では数十秒かかり得る
        wait_until(lambda: record.status.terminal, timeout=120.0)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert record.result is not None
        outputs = [a for a in record.result.artifacts if a.path.endswith(".kicad_pcb")]
        assert len(outputs) == 1

        pcb = PcbFile(fake_camera_settings.webui_data_dir / outputs[0].path)
        assert len(pcb.pads) == 4


class TestCameraCalibrationJob:
    """camera_calibration のフル結合（FakeCamera + test-fixture、装置なし）."""

    def test_full_run_with_checkerboard_yields_apply_payload(
        self,
        checkerboard_manager: JobManager,
        checkerboard_camera_settings: Settings,
        wait_until: WaitUntil,
    ):
        """チェッカーボード画像で SUCCEEDED まで完走し Apply payload を返す.

        - prompt(confirm) に True 応答で撮影 → 検出成功
        - Z は best-effort: Klipper 不通（port 7126）でも続行し z_position=None、
          summary に「未取得」（ユーザー決定 2026-06-12）
        - artifacts: コーナー描画 PNG（image）+ calibration JSON（file）。
          JSON は CalibrationResult.load で読め、数値が素材と整合する
        - apply: values は camera.calibration_file のみ、files に JSON 1 件
        """
        record = checkerboard_manager.start("camera_calibration", CHECKERBOARD_PARAMS)
        answered: set[str] = set()
        answer_next_prompt(record, checkerboard_manager, True, answered)
        wait_until(lambda: record.status.terminal, timeout=30.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "未取得" in result.summary  # Z best-effort 失敗の明示

        kinds = {artifact.kind for artifact in result.artifacts}
        assert kinds == {"image", "file"}
        artifacts_root = checkerboard_camera_settings.webui_data_dir
        png_artifact = next(a for a in result.artifacts if a.kind == "image")
        json_artifact = next(a for a in result.artifacts if a.kind == "file")
        Image.load(artifacts_root / png_artifact.path)  # 読めなければ例外
        loaded = CalibrationResult.load(artifacts_root / json_artifact.path)
        assert loaded.pixel_per_mm == pytest.approx(CHECKERBOARD_PIXEL_PER_MM, rel=0.01)
        assert loaded.z_position is None  # Klipper 不通 → 記録なし
        # crop は machine.toml 由来（checkerboard_state fixture が書いた 400x400）
        assert loaded.crop_size == CHECKERBOARD_CROP_SIZE

        assert record.apply_available is True
        payload = checkerboard_manager.apply_payload()
        filename = payload.values["camera.calibration_file"]
        assert isinstance(filename, str)
        assert filename.endswith(".json")
        assert set(payload.values) == {"camera.calibration_file"}
        assert len(payload.files) == 1
        assert payload.files[0].filename == filename

    def test_decline_first_prompt_aborts_without_apply(
        self, checkerboard_manager: JobManager, wait_until: WaitUntil
    ):
        """撮影確認に「いいえ」→ ABORTED（Apply なし）."""
        record = checkerboard_manager.start("camera_calibration", CHECKERBOARD_PARAMS)
        answered: set[str] = set()
        answer_next_prompt(record, checkerboard_manager, False, answered)
        wait_until(lambda: record.status.terminal, timeout=30.0)

        assert record.status == JobStatus.ABORTED
        assert record.apply_available is False

    def test_missing_square_size_uses_default_and_succeeds(
        self, checkerboard_manager: JobManager, wait_until: WaitUntil
    ):
        """Square_size 省略は required エラーではなく default 1.5 で実行される.

        旧仕様（必須空欄はエラー）からの挙動変更（計画書「要確認事項 1」）。
        """
        record = checkerboard_manager.start("camera_calibration", {})
        answered: set[str] = set()
        answer_next_prompt(record, checkerboard_manager, True, answered)
        wait_until(lambda: record.status.terminal, timeout=30.0)

        assert record.status == JobStatus.SUCCEEDED, record.error

    def test_removed_crop_param_is_rejected_as_unknown(
        self, checkerboard_manager: JobManager
    ):
        """削除済み crop_width を渡すと開始前に ValueError（→ router で 400）.

        crop は machine.toml `[camera.crop]` に一本化され job param からは
        削除済み（計画書「設計判断 a」）。
        """
        with pytest.raises(ValueError, match="未知のパラメータ"):
            checkerboard_manager.start(
                "camera_calibration", {"square_size": 10.0, "crop_width": 400}
            )

    def test_undetectable_image_logs_warning_and_reprompts(
        self, manager: JobManager, wait_until: WaitUntil
    ):
        """検出不能画像（fake_camera.png）→ 警告 log 後に再 prompt で継続できる.

        jobs/conftest の manager は既定の fake_camera.png（チェッカーボード なし）を使う。再
        prompt に「いいえ」で中止できる。
        """
        record = manager.start("camera_calibration", CHECKERBOARD_PARAMS)
        answered: set[str] = set()
        answer_next_prompt(record, manager, True, answered)

        # 検出失敗 → 警告 log → 2 回目の prompt（別 id）が来る
        answer_next_prompt(record, manager, False, answered)
        wait_until(lambda: record.status.terminal, timeout=30.0)

        assert len(answered) == 2
        assert "検出できませんでした" in "\n".join(record.log_lines)
        assert record.status == JobStatus.ABORTED


class TestMachineJobsWithoutKlipper:
    """装置ジョブの graceful FAILED（test-fixture: port 7126 = 接続拒否）.

    成功系のステージ移動・照合は pcbasm のテスト（test_setup / test_alignment）
    と実機区分でカバーする分担（計画書 §4）。
    """

    @pytest.mark.parametrize("name", ["board_tour", "orthogonality_test"])
    def test_pcb_job_fails_gracefully_and_releases_lock(
        self,
        manager: JobManager,
        state: AppState,
        real_pcb_path: Path,
        wait_until: WaitUntil,
        name: str,
    ):
        state.select_pcb(real_pcb_path)
        record = manager.start(name, {})
        wait_until(lambda: record.status.terminal, timeout=60.0)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.FAILED
        assert record.error  # 接続エラーが error に載る
        assert "M84" in "\n".join(record.log_lines)  # relax 失敗警告（manager 経由）
        with state.machine_lock("after-failed-job"):  # ロックは解放済み
            pass

    def test_reference_point_setup_fails_gracefully_without_klipper(
        self, manager: JobManager, state: AppState, wait_until: WaitUntil
    ):
        """ホーミング（G28）で Klipper 不通 → FAILED + ロック解放."""
        record = manager.start("reference_point_setup", {})
        wait_until(lambda: record.status.terminal, timeout=60.0)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.FAILED
        assert record.error
        assert "M84" in "\n".join(record.log_lines)
        with state.machine_lock("after-failed-job"):
            pass

    @pytest.mark.parametrize("name", ["board_tour", "orthogonality_test"])
    def test_pcb_job_without_selection_raises_value_error(
        self, manager: JobManager, name: str
    ):
        """requires_pcb=True: PCB 未選択は開始前に ValueError（→ 400）."""
        with pytest.raises(ValueError):
            manager.start(name, {})


@mark_hardware
class TestPosctrlHardware:
    """実機通し（実カメラ + 実 Moonraker、configs/kurousagi）。ユーザー実行.

    前提（計画書 §5「ユーザーへ引き継ぐ実機確認項目」）:

    - Moonraker が localhost:7125 で稼働し、各軸がホーミング可能であること
    - 実カメラが接続済みでキャリブレーション済みであること
    - camera_calibration: 1 マス 1.5mm の実チェッカーボードを視野に配置
    - board_tour / orthogonality_test: data/testing/fill_coverage の基板が
      ステージにセットされ、基準点マーカーが視野に入ること

    orthogonality_test は対話フロー（巡回先ごとに確認プロンプト）:

    1. セットアップ後、四隅 → TOP 層 pad 中心の順に移動し、各点で
       「ベルトテンションを調整し…」の確認プロンプトが出て停止する
    2. その点で調整・確認を済ませたら「次へ」を押す。1 周し終えると四隅から
       再開し、押し続ける限り周回する
    3. 打ち切りたい点で「終了」を押す。中止ではなく **正常終了（SUCCEEDED）**
       になり、summary に調整前の指標・巡回点数・周回数が出る（指標は開始時の
       1 回計測で、巡回中の調整は反映されない）

    下の自動テストは各点を「次へ」で通し最後に「終了」する流れだけを検証する。
    ベルトテンション調整自体は WebUI から手動で実施する。
    """

    def test_reference_point_setup_jog_and_record_applies_settings_immediately(
        self,
        real_manager: JobManager,
        real_state: AppState,
        wait_until: WaitUntil,
    ):
        """ジョグ → record で現在位置を保存し、追加の Apply を要求しない."""
        record = real_manager.start("reference_point_setup", {})
        wait_until(lambda: record.status == JobStatus.RUNNING, timeout=30.0)
        # command はキューに積まれ、ホーミング完了後のループで順に消費される
        real_manager.submit_command({"type": "jog", "axis": "x", "dist": 0.1})
        real_manager.submit_command({"type": "record"})
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "設定に反映しました" in result.summary
        assert result.apply is None
        assert record.apply_available is False
        saved = real_state.machine().reference_point
        assert f"x={saved.x:.3f}" in result.summary
        assert f"y={saved.y:.3f}" in result.summary

    def test_reference_point_setup_quit_aborts_without_apply(
        self, real_manager: JobManager, wait_until: WaitUntil
    ):
        record = real_manager.start("reference_point_setup", {})
        wait_until(lambda: record.status == JobStatus.RUNNING, timeout=30.0)
        real_manager.submit_command({"type": "quit"})
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.ABORTED
        assert record.apply_available is False

    def test_camera_calibration_records_z_position(
        self,
        real_manager: JobManager,
        real_settings: Settings,
        wait_until: WaitUntil,
    ):
        """実チェッカーボードで z_position が記録される（Z best-effort 成功側）."""
        record = real_manager.start("camera_calibration", {"square_size": 1.5})
        answered: set[str] = set()
        answer_next_prompt(record, real_manager, True, answered)

        # 検出失敗なら再 prompt が来る → 中止してセットアップ不備として fail
        wait_until(
            lambda: record.status.terminal
            or (
                (pending := record.pending_prompt) is not None
                and pending[0] not in answered
            ),
            timeout=120.0,
        )
        if not record.status.terminal:
            answer_next_prompt(record, real_manager, False, answered)
            pytest.fail("チェッカーボードが検出されません。視野に配置してください")

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        json_artifact = next(a for a in result.artifacts if a.kind == "file")
        loaded = CalibrationResult.load(
            real_settings.webui_data_dir / json_artifact.path
        )
        assert loaded.z_position is not None  # 実 Klipper から Z を取得

    def test_board_tour_runs_to_success(
        self,
        real_manager: JobManager,
        real_state: AppState,
        wait_until: WaitUntil,
    ):
        """セットアップ → 巡回 → 照合の通しが SUCCEEDED で完了する（非対話）."""
        real_state.select_pcb(
            Path("data/testing/fill_coverage/fill_coverage.kicad_pcb")
        )
        record = real_manager.start("board_tour", {})
        wait_until(lambda: record.status.terminal, timeout=900.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "照合" in result.summary

    def test_orthogonality_test_prompts_each_point_and_quit_succeeds(
        self,
        real_manager: JobManager,
        real_state: AppState,
        wait_until: WaitUntil,
    ):
        """巡回先ごとに確認プロンプトが出て、「終了」で正常終了する.

        四隅（4 点）を「次へ」で通し、5 点目（TOP 層 pad の 1 点目）で「終了」を 返す。中止扱いではなく
        SUCCEEDED で、summary に指標と周回数が載る。
        """
        real_state.select_pcb(
            Path("data/testing/fill_coverage/fill_coverage.kicad_pcb")
        )
        record = real_manager.start("orthogonality_test", {})
        answered: set[str] = set()
        # 1 点目のプロンプトはホーミング + 基準点合わせの後に来るため待ちが長い
        for _ in range(4):
            answer_next_prompt(record, real_manager, True, answered, timeout=900.0)
        answer_next_prompt(record, real_manager, False, answered, timeout=300.0)
        wait_until(lambda: record.status.terminal, timeout=300.0)

        assert record.status == JobStatus.SUCCEEDED
        result = record.result
        assert result is not None
        assert result.summary is not None
        assert "軸間角" in result.summary
        assert "巡回 5 点" in result.summary  # 「終了」を押した点まで数える
        assert "1 周目で終了" in result.summary
