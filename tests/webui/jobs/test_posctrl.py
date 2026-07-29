"""`webui.jobs.posctrl` の仕様テスト.

計画書 memory/agents/implementation-planner/webui-phase4.md
「src/webui/jobs/posctrl.py」節 + spec §10 posctrl 表が契約:

- catalog: posctrl 5 ジョブ（reference_point_setup / camera_calibration /
  board_tour / orthogonality_test / generate_grid_pcb）の name / requires_pcb /
  uses_machine / accepts_commands / params の default
- generate_grid_pcb: 出力 .kicad_pcb が PcbFile で読めて pad 数 = divisions^2
  （装置非使用。dev タブから位置合わせタブへ移設）
- camera_calibration: チェッカーボード FakeCamera での prompt 往復と、Klipper
  不通での graceful FAILED（ステージスキャンが必須になったため装置なしでは
  完走しない。歪み校正の通しは pcbasm 側と `@mark_hardware` が担当）
- board_tour / orthogonality_test / reference_point_setup の異常系:
  テスト用 config（Klipper port 7126 非リッスン）で graceful FAILED + ロック解放 +
  PRESENT / relax (M84) 失敗警告。成功系のステージ移動・照合は pcbasm テストと
  実機区分でカバーする分担（計画書 §4）
- 実機通し（実カメラ + 実 Klipper）は `@mark_hardware` でユーザー実行

計画書 `~/.claude/plans/claude-pixels-mm-0-1mm-300-x-swirling-robin.md`（レンズ歪み
補正）§6 と §10 が camera_calibration の上書き契約:

- params は square_size（float 1.5mm）+ residual_limit（float 30.0um・minimum 0.0）。
  両方が persisted_params。crop_width / crop_height は引き続き job param では
  なく machine.toml `[camera.crop]` が真実（二重管理の回避）
- accepts_commands=False を維持（ホーミングも Z 移動もせず、操作者が合わせた
  フォーカス Z を破壊しない）
- フロー: prompt(confirm) → 計画用ショットで検出 → Klipper 接続 + homed_axes 確認
  → ScanGrid.plan → CheckerboardScanner.scan で 15 点 → IntrinsicsCalibrator.solve
  → quality.after.rms_um > residual_limit なら artifacts 保存後に RuntimeError
- prompt は Klipper チェックより**先**（カメラだけで完結する安価なチェックを
  装置に触る前に済ませる）。よって prompt 往復の 2 テストは装置なしで成立する
- z_position は best-effort をやめ必須記録（実機区分で検証）

「直行性テストを対話フローへ戻す」変更が `orthogonality_test` の追加契約:

- 巡回先は四隅（Top-Left / Top-Right / Bottom-Right / Bottom-Left）+ TOP 層 pad
  中心（`Grid {i+1}/{n}`、現在位置から machine 座標での nearest 順。点列は開始時に
  1 回だけ構築し周回間で固定）
- 各点で移動 → `confirm_next_point`（board_ops）の確認プロンプトを出し、ユーザー
  応答を待つ。待機は `while_waiting` 付きで、応答が来るまでポーリング間隔ごとに
  十字線・ROI・ラベルを描いたライブフレームを `ctx.frame` へ流し続ける（到着時に
  1 秒だけ表示するのではなく、待機中ずっとオーバーレイが見えている）
- 「終了」（False）を受けた時点で **SUCCEEDED**（JobAborted は投げない）。summary
  に調整前の指標 + 巡回点数 + 周回数を含む
- 1 周し終えると四隅から再開し、「終了」ボタンか abort までずっと周回する

巡回本体（移動・プレビュー・周回）は装置なしでは検証できない。`setup_board` が
実 Klipper でのホーミングと基準点合わせを必須とし、テスト用 config では最初の
prompt に到達する前に FAILED になるため。よって分担は:

- prompt の spec と True/False の意味・`while_waiting` の委譲 →
  `test_board_ops.py::TestConfirmNextPoint`（実 JobManager 経由の合成ジョブ）
- prompt 待機中 abort → `test_manager.py::TestPrompt`
- 待機中のポーリング（繰り返し呼ばれる・応答後は呼ばれない・例外で FAILED）→
  `test_manager.py::TestPromptWhileWaiting`
- 実際に十字線が待機中ずっと出ているかは実機での目視（下の `@mark_hardware`）
- 巡回の通し（周回・「終了」で SUCCEEDED）→ 下の `@mark_hardware` 区分
- setup 段の graceful FAILED → `TestMachineJobsWithoutKlipper`（変更不要）

cv2 / Moonraker のモックは使わない（skill `testing-strategy`）。Klipper 不通は
テスト用 config の実ポートへの接続拒否で検証する。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from pcbasm.pcb import PcbFile
from pcbasm.vision import CalibrationResult
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

# checkerboard.png は 400x400・1 マス約 66.7px（tests/pcbasm/vision/test_calibration.py
# と同一素材）。計画用ショットの検出だけが装置なしで到達できる範囲なので、
# square_size の値そのものは結果に効かない
CHECKERBOARD_PARAMS = {"square_size": 10.0}


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
    # checkerboard.png は 400x400。crop は job param ではなく machine.toml
    # `[camera.crop]` から読まれる契約なので、画像をはみ出す既定 600 ではなく
    # 400x400 を tmp コピーの machine.toml へ書いてから AppState を作る
    # （プレビューの十字線・関心領域オーバーレイがこの値を毎フレーム読む）
    store.write_machine_settings({"camera.crop.width": 400, "camera.crop.height": 400})
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
        """Params は square_size + residual_limit。crop は machine.toml 連動で params
        外.

        residual_limit（歪み補正後の残差 RMS 上限 [um]）は合否ゲートで実機の 調整対象になるため
        param（board_tour / toolhead_offset の tolerance と
        同じ役割）。負値は意味を持たないので minimum=0.0。両方が入力途中の 即保存対象（persisted_params）。
        """
        definition = default.get("camera_calibration")
        params = {spec.name: spec for spec in definition.params}

        assert set(params) == {"square_size", "residual_limit"}
        assert params["square_size"].value_type == "float"
        assert params["square_size"].default == 1.5
        assert params["residual_limit"].value_type == "float"
        assert params["residual_limit"].default == 30.0
        assert params["residual_limit"].minimum == 0.0
        assert set(definition.persisted_params) == {"square_size", "residual_limit"}

    def test_camera_calibration_params_are_all_optional_with_defaults(
        self, default: JobCatalog
    ):
        """空 body でも両 param が既定値で埋まる（フォーム未入力で実行できる）.

        旧仕様（square_size は必須空欄でエラー）からの挙動変更を維持しつつ、 residual_limit
        にも同じ規約を適用する。
        """
        definition = default.get("camera_calibration")

        validated = default.validate_params(definition, {})

        assert validated == {"square_size": 1.5, "residual_limit": 30.0}

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
    """camera_calibration の装置なし部分（FakeCamera + テスト用 config）.

    ステージスキャン（15 点）が必須になったため、装置なしで到達できるのは
    prompt 往復と計画用ショットの検出まで。歪み校正そのものの通し検証は
    `tests/pcbasm/vision/test_calibration.py::TestDistortionRecovery` /
    `tests/pcbasm/posctrl/test_checkerboard_scan.py` と `@mark_hardware`
    区分が担当する（`TestMachineJobsWithoutKlipper` と同じ分担方針）。
    """

    def test_decline_first_prompt_aborts_without_apply(
        self, checkerboard_manager: JobManager, wait_until: WaitUntil
    ):
        """撮影確認に「いいえ」→ ABORTED（Apply なし）.

        prompt が Klipper 接続チェックより先に出ることの担保でもある。
        """
        record = checkerboard_manager.start("camera_calibration", CHECKERBOARD_PARAMS)
        answered: set[str] = set()
        answer_next_prompt(record, checkerboard_manager, False, answered)
        wait_until(lambda: record.status.terminal, timeout=30.0)

        assert record.status == JobStatus.ABORTED
        assert record.apply_available is False

    def test_fails_gracefully_after_confirm_without_klipper(
        self,
        checkerboard_manager: JobManager,
        checkerboard_state: AppState,
        wait_until: WaitUntil,
    ):
        """確認に「はい」→ 計画用ショットは通るが Klipper 不通で FAILED + ロック解放.

        テスト用 config は Klipper port 7126（非リッスン）なので、計画用 ショットの検出後に置かれた接続 /
        homed_axes チェックで必ず落ちる。 Apply を出さず、装置排他ロックを解放して終わることが契約。
        """
        record = checkerboard_manager.start("camera_calibration", CHECKERBOARD_PARAMS)
        answered: set[str] = set()
        answer_next_prompt(record, checkerboard_manager, True, answered)
        wait_until(lambda: record.status.terminal, timeout=120.0)
        wait_until(lambda: checkerboard_state.busy_owner is None)

        assert record.status == JobStatus.FAILED
        assert record.error  # 接続エラーが error に載る
        assert record.apply_available is False
        assert "M84" in "\n".join(record.log_lines)  # relax 失敗警告（manager 経由）
        with checkerboard_state.machine_lock("after-failed-job"):  # 解放済み
            pass

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
    """装置ジョブの graceful FAILED（テスト用 config: port 7126 = 接続拒否）.

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
    """実機通し（実カメラ + 実 Moonraker、実機 config/）。ユーザー実行.

    前提（計画書 §5「ユーザーへ引き継ぐ実機確認項目」）:

    - Moonraker が localhost:7125 で稼働し、各軸がホーミング可能であること
    - 実カメラが接続済みであること（キャリブレーション済みであることを要求するのは
      それを前提にする各テストで、camera_calibration は逆に未校正から始める）
    - camera_calibration: 手動セットアップの前提が多いため、下記
      `test_camera_calibration_scan_yields_camera_matrix_and_residual_report`
      の docstring に個別に列挙してある
    - board_tour / orthogonality_test: data/testing/fill_coverage の基板が
      ステージにセットされ、基準点マーカーが視野に入ること

    orthogonality_test は対話フロー（巡回先ごとに確認プロンプト）:

    1. セットアップ後、四隅 → TOP 層 pad 中心の順に移動し、各点で
       「ベルトテンションを調整し…」の確認プロンプトが出て停止する
    2. 応答を待っている間、プレビューには十字線・ROI・巡回先ラベルを重ねた
       ライブ映像が出続ける。**目視確認項目**: 待機が何秒続いてもオーバーレイが
       消えない（到着直後だけ表示して消える挙動になっていないこと）
    3. その点で調整・確認を済ませたら「次へ」を押す。1 周し終えると四隅から
       再開し、押し続ける限り周回する
    4. 打ち切りたい点で「終了」を押す。中止ではなく **正常終了（SUCCEEDED）**
       になり、summary に調整前の指標・巡回点数・周回数が出る（指標は開始時の
       1 回計測で、巡回中の調整は反映されない）

    下の自動テストは各点を「次へ」で通し最後に「終了」する流れだけを検証する。
    オーバーレイの表示継続とベルトテンション調整自体は WebUI から目視・手動で
    実施する。
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

    def test_camera_calibration_scan_yields_camera_matrix_and_residual_report(
        self,
        real_manager: JobManager,
        real_settings: Settings,
        wait_until: WaitUntil,
    ):
        """ステージ 15 点スキャンで intrinsics を推定し、残差が補正で改善する.

        **このテストを実行する前に手で整えておく前提**（ジョブはホーミングも
        Z 移動もしないため、満たさないと即 FAILED になる）:

        1. Moonraker が localhost:7125 で稼働している
        2. 全軸（x / y / z）がホーミング済みである（`homed_axes` の確認が入る）
        3. カメラのフォーカス Z へジョグ済みである（その Z が z_position として
           記録される。ジョブは Z を動かさない）
        4. **1 マス 1.5mm・12x9 マス（内部コーナー 11x8）のチェッカーボード**が
           ステージにセットされ、そのマス目が画像中央の十字線に合っている
        5. 現在位置の周囲 **±11.3mm(X) / ±4.4mm(Y)** が可動域内である
           （5x3 格子・span 22.6 x 8.7mm を蛇行で巡回し、最後に開始位置へ戻る）

        検証するのは「補正が有効な (K,D) を得て、残差が実測で改善する」ことのみ。
        残差の絶対値は `residual_limit`（既定 30um）が既にゲートしているので
        ここでは重ねず、`before → after` の単調改善だけをアサートする。
        """
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
            timeout=300.0,
        )
        if not record.status.terminal:
            answer_next_prompt(record, real_manager, False, answered)
            pytest.fail("チェッカーボードが検出されません。視野に配置してください")

        assert record.status == JobStatus.SUCCEEDED, record.error
        result = record.result
        assert result is not None
        artifact_names = {Path(a.path).name for a in result.artifacts}
        assert "scan_verification.json" in artifact_names
        assert "residuals.png" in artifact_names

        # 採用候補の JSON は Apply payload の filename で特定する（命名規約に依存しない）
        payload = real_manager.apply_payload()
        filename = payload.values["camera.calibration_file"]
        json_artifact = next(
            a for a in result.artifacts if Path(a.path).name == filename
        )
        loaded = CalibrationResult.load(
            real_settings.webui_data_dir / json_artifact.path
        )

        assert loaded.z_position is not None  # フォーカス Z が必須記録される
        # 使い物になる K が出ている（焦点距離が正・主点が視野内・歪みが非ゼロ）
        width, height = loaded.resolution
        matrix = loaded.intrinsics.camera_matrix
        assert matrix[0][0] > 0.0
        assert matrix[1][1] > 0.0
        assert 0.0 < matrix[0][2] < width
        assert 0.0 < matrix[1][2] < height
        assert any(coefficient != 0.0 for coefficient in loaded.intrinsics.distortion)
        # 補正で残差が改善している（歪みが実際にモデル化できた証拠）
        assert loaded.quality.after.rms_um < loaded.quality.before.rms_um

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
