"""`web.api.routers.jobs` の仕様テスト（REST + WS /api/ws）.

計画書 webui-phase3.md「src/webui/routers/jobs.py」節 + spec §6 / §9 が契約:

- POST /api/jobs/{name} → 201 {"job": JobSummary} / 404 / 400 / 409
- GET /api/jobs/current → {"job": JobSummary | null}（WS 再接続時の同期用）
- POST /api/jobs/current/abort → 200 {"aborted": true} / 409
- POST /api/jobs/last/apply → 200 {"applied": {...}}（machine.toml へ書込・
  コメント保持）/ 409、POST /api/jobs/last/discard → 200（冪等）
- WS /api/ws: job_status / log / progress / prompt / prompt_resolved /
  state_changed / error、クライアント → respond_prompt / command / abort
- 排他の波及: ジョブ実行中は machine-control / マシン切替 / 設定保存 /
  PCB 切替が 409
- /artifacts: 成果物 URL 配信 + traversal 拒否
- /api/state に job ブリーフ

MR2（計画書 docs/plans/web-api-ui-split.md「MR2」節）が追記契約:

- GET /api/jobs → {"jobs": [JobSpecInfo]}。登録順の全件（hidden も filter しない）で、
  `params` は保存済み既定値を `default` に反映した状態
- preview ペイン / ローディング UI の有無は JobDefinition の provides_preview /
  loading_param / loading_stages が正（pages.py のハードコード辞書は撤去済み）

同期待ちは threading.Event ゲート付き合成ジョブ + ポーリング / WS 受信駆動で
決定的に行う（sleep 固定値のアサート禁止）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from pcbasm.vision import CalibrationResult
from tests.web.api.jobs.conftest import register_gated, register_synthetic
from web.api.config_store import ConfigStore
from web.api.jobs.catalog import ParamSpec
from web.api.jobs.context import Artifact, JobContext, JobResult, PromptSpec
from web.api.jobs.manager import JobManager
from web.api.state import AppState

_TERMINAL = ("succeeded", "failed", "aborted")


def _current_job(client: TestClient) -> dict[str, Any] | None:
    response = client.get("/api/jobs/current")
    assert response.status_code == 200
    return response.json()["job"]


def _wait_job_status(
    client: TestClient, status: str, *, timeout: float = 60.0
) -> dict[str, Any]:
    """GET /api/jobs/current をポーリングして指定ステータスを待つ."""
    deadline = time.monotonic() + timeout
    job: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        job = _current_job(client)
        if job is not None and job["status"] == status:
            return job
        if job is not None and job["status"] in _TERMINAL and status not in _TERMINAL:
            pytest.fail(f"終端 {job['status']} に到達: {job}")
        time.sleep(0.02)
    pytest.fail(f"{timeout}s 以内に status={status} になりませんでした: {job}")


def _register_gated(
    app: FastAPI,
    name: str = "gated_router",
    *,
    notify_on_completion: bool = False,
) -> threading.Event:
    """App の catalog へ gated 合成ジョブを登録する（jobs/conftest 共有形の薄い委譲）."""
    return register_gated(
        app.state.catalog,
        name=name,
        notify_on_completion=notify_on_completion,
        hidden=True,
    )


def _register_runtime_editable(
    app: FastAPI, name: str = "runtime_editable_router"
) -> None:
    """Confirm prompt で WAITING_INPUT に留まる、runtime_editable param 付き合成ジョブ.

    board_width は固定（runtime_editable=False）、line_length と
    removal_z_offset は 実行中変更可。PUT /jobs/current/params の即反映と検証（固定/未知/型/負
    offset）を GET /jobs/current で観測するための題材（実 dispense_calibration の機械フローに
    依存しない）。
    """

    def run(ctx: JobContext) -> None:
        ctx.prompt(PromptSpec(kind="confirm", message="待機"))

    register_synthetic(
        app.state.catalog,
        run,
        name=name,
        label="実行中編集ジョブ",
        params=(
            ParamSpec("board_width", "基板幅", "float", default=40.0),
            ParamSpec(
                "line_length",
                "線長",
                "float",
                default=10.0,
                runtime_editable=True,
            ),
            ParamSpec(
                "removal_z_offset",
                "退避Zオフセット",
                "float",
                default=0.0,
                unit="mm",
                runtime_editable=True,
                minimum=0.0,
            ),
        ),
        persisted_params=("line_length",),
        hidden=True,
    )


def _receive_until(
    ws: WebSocketTestSession,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    answer_prompts: bool = False,
    number_answer: float = 60.0,
    limit: int = 1000,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """条件を満たす WS イベントまで受信する（受信駆動・sleep なし）.

    answer_prompts=True なら prompt イベントへ kind に応じて応答する （confirm=True /
    number=number_answer）。
    """
    history: list[dict[str, Any]] = []
    for _ in range(limit):
        message = ws.receive_json()
        history.append(message)
        if answer_prompts and message["type"] == "prompt":
            prompt = message["prompt"]
            answer = True if prompt["kind"] == "confirm" else number_answer
            ws.send_json(
                {
                    "type": "respond_prompt",
                    "prompt_id": prompt["id"],
                    "answer": answer,
                }
            )
        if predicate(message):
            return message, history
    pytest.fail(f"{limit} 件以内に期待する WS イベントが届きませんでした")


def _complete_job_demo(
    client: TestClient, app: FastAPI, *, answer: float = 61.5
) -> None:
    """job_demo を起動し、prompt 2 回を manager 経由で応答して完走させる."""
    jobs: JobManager = app.state.jobs
    response = client.post(
        "/api/jobs/job_demo", json={"params": {"steps": 1, "interval": 0.01}}
    )
    assert response.status_code == 201
    answered: set[str] = set()
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        record = jobs.current()
        pending = record.pending_prompt if record is not None else None
        if pending is not None and pending[0] not in answered:
            prompt_id, spec = pending
            jobs.respond_prompt(prompt_id, True if spec.kind == "confirm" else answer)
            answered.add(prompt_id)
        job = _current_job(client)
        if job is not None and job["status"] == "succeeded":
            return
        if job is not None and job["status"] in ("failed", "aborted"):
            pytest.fail(f"job_demo が {job['status']} になりました: {job}")
        time.sleep(0.02)
    pytest.fail("job_demo が完走しませんでした")


def _jobs_by_name(client: TestClient) -> dict[str, dict[str, Any]]:
    """GET /api/jobs のジョブ一覧を name 引きの dict にする."""
    response = client.get("/api/jobs")
    assert response.status_code == 200
    return {job["name"]: job for job in response.json()["jobs"]}


class TestJobCatalogApi:
    """GET /api/jobs — ジョブカタログの公開表現（MR2）."""

    def test_lists_every_registered_job_in_registration_order(
        self, client: TestClient, app: FastAPI
    ):
        names = [job["name"] for job in client.get("/api/jobs").json()["jobs"]]

        assert names == [d.name for d in app.state.catalog.list()]

    def test_includes_hidden_jobs(self, client: TestClient, app: FastAPI):
        """Hidden ジョブも filter しない（実行時登録され POST もできるため）."""
        register_synthetic(
            app.state.catalog,
            lambda ctx: JobResult(summary="ok"),
            name="hidden_router_job",
            hidden=True,
        )

        jobs = _jobs_by_name(client)

        assert jobs["hidden_router_job"]["hidden"] is True
        assert jobs["paste_solder"]["hidden"] is False

    def test_reports_preview_and_loading_facts_from_definition(
        self, client: TestClient
    ):
        """Preview ペイン / ローディング UI の有無は JobDefinition が正（SSR と同じ値）."""
        jobs = _jobs_by_name(client)

        paste_solder = jobs["paste_solder"]
        assert paste_solder["provides_preview"] is True
        assert paste_solder["loading_param"] == "amount"
        assert paste_solder["loading_stages"] == "ローディング"

        # メニュー段階のプライムでもボタンを有効化する（カンマ区切り）
        assert jobs["dispense_calibration"]["loading_stages"] == (
            "キャリブレーションメニュー,ローディング"
        )

        # カメラもローディングも使わない生成ジョブ
        assert jobs["generate_rect_pcb"]["provides_preview"] is False
        assert jobs["generate_rect_pcb"]["loading_param"] is None

    def test_reports_paste_dataset_collection_contract(self, client: TestClient):
        job = _jobs_by_name(client)["paste_dataset_collection"]

        assert job["requires_pcb"] is False
        assert job["uses_machine"] is True
        assert job["provides_preview"] is True
        assert job["hidden"] is False

    def test_reports_start_policy_flags(self, client: TestClient, app: FastAPI):
        definition = app.state.catalog.get("paste_solder")

        job = _jobs_by_name(client)["paste_solder"]

        assert job["label"] == definition.label
        assert job["tab"] == definition.tab
        assert job["requires_pcb"] == definition.requires_pcb
        assert job["uses_machine"] == definition.uses_machine
        assert job["notify_on_completion"] == definition.notify_on_completion
        assert job["accepts_commands"] == definition.accepts_commands
        assert job["persisted_params"] == list(definition.persisted_params)
        assert job["runtime_params"] == list(definition.runtime_params)

    def test_param_specs_expose_every_declared_field(
        self, client: TestClient, app: FastAPI
    ):
        """ParamSpec の全フィールドがそのまま公開される（フォーム描画に必要な全量）."""
        specs = {spec.name: spec for spec in app.state.catalog.get("loading").params}

        params = {p["name"]: p for p in _jobs_by_name(client)["loading"]["params"]}

        assert set(params) == set(specs)
        for name, spec in specs.items():
            assert params[name] == {
                "name": spec.name,
                "label": spec.label,
                "value_type": spec.value_type,
                "default": spec.default,
                "choices": list(spec.choices),
                "unit": spec.unit,
                "help": spec.help,
                "runtime_editable": spec.runtime_editable,
                "minimum": spec.minimum,
                "optional": spec.optional,
            }

    def test_params_reflect_saved_defaults(
        self, client: TestClient, appstate: AppState
    ):
        """保存済み既定値が ParamSpec.default に反映された状態で返る（SSR と同じ）."""
        appstate.merge_job_param_defaults("loading", {"amount": 0.2, "rate": 1.5})

        params = {p["name"]: p for p in _jobs_by_name(client)["loading"]["params"]}

        assert params["amount"]["default"] == 0.2
        assert params["rate"]["default"] == 1.5

    def test_params_ignore_type_mismatched_saved_values(
        self, client: TestClient, app: FastAPI, appstate: AppState
    ):
        """型不一致の保存値は黙って除外され、spec 既定値のまま返る."""
        spec_default = {
            spec.name: spec.default for spec in app.state.catalog.get("loading").params
        }
        appstate.merge_job_param_defaults("loading", {"amount": "とても多め"})

        params = {p["name"]: p for p in _jobs_by_name(client)["loading"]["params"]}

        assert params["amount"]["default"] == spec_default["amount"]


class TestStartJob:
    """POST /api/jobs/{name}."""

    def test_start_returns_201_with_job_summary(self, client: TestClient, app: FastAPI):
        gate = _register_gated(app)

        response = client.post("/api/jobs/gated_router", json={})

        assert response.status_code == 201
        job = response.json()["job"]
        assert job["id"]
        assert job["name"] == "gated_router"
        assert job["status"] in ("pending", "running")

        gate.set()
        _wait_job_status(client, "succeeded")

    def test_summary_includes_completion_notification_policy(
        self, client: TestClient, app: FastAPI
    ):
        gate = _register_gated(app, name="notifying_router", notify_on_completion=True)

        response = client.post("/api/jobs/notifying_router", json={})

        assert response.status_code == 201
        assert response.json()["job"]["notify_on_completion"] is True
        gate.set()
        current = _wait_job_status(client, "succeeded")
        assert current["notify_on_completion"] is True

    def test_start_fills_param_defaults_into_summary(self, client: TestClient):
        response = client.post(
            "/api/jobs/job_demo", json={"params": {"steps": 1, "interval": 0.01}}
        )

        assert response.status_code == 201
        params = response.json()["job"]["params"]
        assert params["steps"] == 1
        assert params["interval"] == 0.01
        assert params["fail"] is False

        # 後始末: prompt 待ちで止まるため abort して終端まで待つ
        assert client.post("/api/jobs/current/abort").status_code == 200
        _wait_job_status(client, "aborted")

    def test_unknown_job_returns_404(self, client: TestClient):
        response = client.post("/api/jobs/no-such-job", json={})

        assert response.status_code == 404

    @pytest.mark.parametrize(
        "params",
        [
            {"steps": "many"},  # 型不一致
            {"no_such_param": 1.0},  # 未知キー
        ],
    )
    def test_invalid_params_return_400(
        self, client: TestClient, params: dict[str, object]
    ):
        response = client.post("/api/jobs/job_demo", json={"params": params})

        assert response.status_code == 400

    def test_requires_pcb_without_selection_returns_400(self, client: TestClient):
        response = client.post("/api/jobs/extract_pcb", json={})

        assert response.status_code == 400

    def test_double_start_returns_409(self, client: TestClient, app: FastAPI):
        gate = _register_gated(app)
        assert client.post("/api/jobs/gated_router", json={}).status_code == 201
        _wait_job_status(client, "running")

        response = client.post("/api/jobs/job_demo", json={"params": {"steps": 1}})

        assert response.status_code == 409

        gate.set()
        _wait_job_status(client, "succeeded")


class TestSaveParamDefaults:
    """POST /api/jobs/{name}/param-defaults（フォーム入力の即保存）.

    「実行」を待たず、入力途中でも persisted_params の有効値を次回フォーム既定値へ
    マージ保存する。型不一致・persisted 外キーは無視（404: 未知ジョブ）。
    """

    def test_saves_persisted_values_for_next_form(
        self, client: TestClient, appstate: AppState
    ):
        response = client.post(
            "/api/jobs/loading/param-defaults",
            json={
                "values": {
                    "amount": 0.4,
                    "rotations": 7.0,
                    "rate": 1.5,
                    "accel": 2.0,
                }
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["defaults"] == {
            "amount": 0.4,
            "rotations": 7.0,
            "rate": 1.5,
            "accel": 2.0,
        }
        # 実行を経ずに次回フォームの既定値になる（MR4 以降、これが描画に乗ることは
        # frontend 側の tests/web/ui/test_pages.py が `GET /api/jobs` 経由で確かめる）
        assert appstate.job_param_defaults("loading")["rotations"] == 7.0
        assert appstate.job_param_defaults("loading")["amount"] == 0.4

    def test_ignores_non_persisted_and_invalid_values(
        self, client: TestClient, appstate: AppState
    ):
        response = client.post(
            "/api/jobs/loading/param-defaults",
            json={
                "values": {
                    "amount": 0.5,  # persisted・有効
                    "rate": "fast",  # 型不一致 → 無視
                    "no_such": 1.0,  # persisted 外 → 無視
                }
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["defaults"] == {"amount": 0.5}
        assert appstate.job_param_defaults("loading") == {"amount": 0.5}

    def test_merges_with_previously_saved(self, client: TestClient):
        client.post(
            "/api/jobs/loading/param-defaults",
            json={"values": {"amount": 0.3, "rotations": 6.0}},
        )
        response = client.post(
            "/api/jobs/loading/param-defaults",
            json={"values": {"rotations": 9.0}},  # rotations だけ更新
        )

        assert response.json()["defaults"] == {"amount": 0.3, "rotations": 9.0}

    def test_unknown_job_returns_404(self, client: TestClient):
        response = client.post(
            "/api/jobs/no-such-job/param-defaults", json={"values": {"amount": 1.0}}
        )

        assert response.status_code == 404


class TestCurrentAndAbort:
    """GET /api/jobs/current / POST /api/jobs/current/abort."""

    def test_current_is_null_before_any_job(self, client: TestClient):
        assert _current_job(client) is None

    def test_current_reports_running_then_succeeded(
        self, client: TestClient, app: FastAPI
    ):
        gate = _register_gated(app)
        assert client.post("/api/jobs/gated_router", json={}).status_code == 201

        running = _wait_job_status(client, "running")
        assert running["name"] == "gated_router"

        gate.set()
        done = _wait_job_status(client, "succeeded")
        assert done["error"] is None

    def test_abort_running_job_returns_200_and_aborts(
        self, client: TestClient, app: FastAPI
    ):
        _register_gated(app)
        assert client.post("/api/jobs/gated_router", json={}).status_code == 201
        _wait_job_status(client, "running")

        response = client.post("/api/jobs/current/abort")

        assert response.status_code == 200
        assert response.json()["aborted"] is True
        _wait_job_status(client, "aborted")

    def test_abort_while_waiting_prompt_is_immediate(self, client: TestClient):
        assert (
            client.post(
                "/api/jobs/job_demo", json={"params": {"steps": 1, "interval": 0.01}}
            ).status_code
            == 201
        )
        # prompt 待ち（waiting_input）に入るのを待つ
        _wait_job_status(client, "waiting_input")

        assert client.post("/api/jobs/current/abort").status_code == 200

        _wait_job_status(client, "aborted")

    def test_abort_without_active_job_returns_409(self, client: TestClient):
        response = client.post("/api/jobs/current/abort")

        assert response.status_code == 409

    def test_abort_after_terminal_returns_409(self, client: TestClient, app: FastAPI):
        gate = _register_gated(app)
        assert client.post("/api/jobs/gated_router", json={}).status_code == 201
        gate.set()
        _wait_job_status(client, "succeeded")

        response = client.post("/api/jobs/current/abort")

        assert response.status_code == 409


class TestUpdateCurrentParams:
    """PUT /api/jobs/current/params（計画書「能力 1」routers 節が契約）.

    実行中ジョブの runtime_editable な subset を out-of-band で patch する:
    - アクティブジョブ無し → 400
    - 固定キー / 未知キー / 型不正 / 負 removal_z_offset → 400
    - 正常 → 200 で {"params": {...}} を返し、GET /jobs/current が新値を映す
    """

    def _put_params(
        self, client: TestClient, values: dict[str, object], *, persist: bool = False
    ):
        return client.put(
            "/api/jobs/current/params", json={"values": values, "persist": persist}
        )

    def test_without_active_job_returns_400(self, client: TestClient):
        response = self._put_params(client, {"line_length": 5.0})

        assert response.status_code == 400

    def test_runtime_editable_update_returns_200_and_reflects_in_current(
        self, client: TestClient, app: FastAPI
    ):
        _register_runtime_editable(app)
        assert (
            client.post("/api/jobs/runtime_editable_router", json={}).status_code == 201
        )
        _wait_job_status(client, "waiting_input")

        response = self._put_params(client, {"line_length": 12.5})

        assert response.status_code == 200, response.text
        assert response.json()["params"] == {"line_length": 12.5}
        # GET /jobs/current が即座に新値を映す
        job = _current_job(client)
        assert job is not None
        assert job["params"]["line_length"] == 12.5

        assert client.post("/api/jobs/current/abort").status_code == 200
        _wait_job_status(client, "aborted")

    def test_fixed_param_update_returns_400(self, client: TestClient, app: FastAPI):
        _register_runtime_editable(app)
        assert (
            client.post("/api/jobs/runtime_editable_router", json={}).status_code == 201
        )
        _wait_job_status(client, "waiting_input")

        # board_width は runtime_editable=False（固定）→ 400
        response = self._put_params(client, {"board_width": 99.0})

        assert response.status_code == 400

        assert client.post("/api/jobs/current/abort").status_code == 200
        _wait_job_status(client, "aborted")

    @pytest.mark.parametrize(
        "values",
        [
            {"no_such_param": 1.0},  # 未知キー
            {"line_length": "fast"},  # 型不正
        ],
    )
    def test_invalid_patch_returns_400(
        self, client: TestClient, app: FastAPI, values: dict[str, object]
    ):
        _register_runtime_editable(app)
        assert (
            client.post("/api/jobs/runtime_editable_router", json={}).status_code == 201
        )
        _wait_job_status(client, "waiting_input")

        response = self._put_params(client, values)

        assert response.status_code == 400

        assert client.post("/api/jobs/current/abort").status_code == 200
        _wait_job_status(client, "aborted")

    def test_negative_removal_z_offset_returns_400(
        self, client: TestClient, app: FastAPI
    ):
        """removal_z_offset 負値は 400（負退避を拒否）.

        退避 Z = max(z_min, z_max − offset)。負 offset はパラメータ検証で拒否する。
        """
        _register_runtime_editable(app)
        assert (
            client.post("/api/jobs/runtime_editable_router", json={}).status_code == 201
        )
        _wait_job_status(client, "waiting_input")

        response = self._put_params(client, {"removal_z_offset": -1.0})

        assert response.status_code == 400

        assert client.post("/api/jobs/current/abort").status_code == 200
        _wait_job_status(client, "aborted")

    def test_persist_true_updates_next_form_default(
        self, client: TestClient, app: FastAPI, appstate: AppState
    ):
        _register_runtime_editable(app)
        assert (
            client.post(
                "/api/jobs/runtime_editable_router",
                json={"params": {"line_length": 10.0}},
            ).status_code
            == 201
        )
        _wait_job_status(client, "waiting_input")

        response = self._put_params(client, {"line_length": 42.0}, persist=True)

        assert response.status_code == 200, response.text
        # persisted_params に含まれる line_length が次回フォーム既定へ保存される
        assert (
            appstate.job_param_defaults("runtime_editable_router")["line_length"]
            == 42.0
        )

        assert client.post("/api/jobs/current/abort").status_code == 200
        _wait_job_status(client, "aborted")


class TestStateBrief:
    """GET /api/state の job ブリーフ."""

    def test_state_job_is_null_initially(self, client: TestClient):
        assert client.get("/api/state").json()["job"] is None

    def test_state_reports_running_job_brief(self, client: TestClient, app: FastAPI):
        gate = _register_gated(app)
        assert client.post("/api/jobs/gated_router", json={}).status_code == 201
        _wait_job_status(client, "running")

        brief = client.get("/api/state").json()["job"]

        assert brief is not None
        assert brief["id"]
        assert brief["name"] == "gated_router"
        assert brief["status"] == "running"

        gate.set()
        _wait_job_status(client, "succeeded")


class TestExclusionPropagation:
    """ジョブ実行中の設定保存・machine-control・PCB 切替は 409."""

    def test_machine_endpoints_return_409_while_job_running(
        self, client: TestClient, app: FastAPI
    ):
        gate = _register_gated(app)
        assert client.post("/api/jobs/gated_router", json={}).status_code == 201
        _wait_job_status(client, "running")

        try:
            assert (
                client.post(
                    "/api/machine-control", json={"action": "relax"}
                ).status_code
                == 409
            )
            assert (
                client.put(
                    "/api/settings/machine",
                    json={"values": {"paste_dispenser.max_fill_speed": 0.9}},
                ).status_code
                == 409
            )
            assert (
                client.put(
                    "/api/pcb-file", json={"path": "boards/sample.kicad_pcb"}
                ).status_code
                == 409
            )
        finally:
            gate.set()
        _wait_job_status(client, "succeeded")


class TestApplyDiscard:
    """POST /api/jobs/last/apply / /api/jobs/last/discard."""

    def test_apply_writes_machine_toml_preserving_comments(
        self, client: TestClient, app: FastAPI, config_dir: Path
    ):
        _complete_job_demo(client, app, answer=61.5)

        response = client.post("/api/jobs/last/apply")

        assert response.status_code == 200
        applied = response.json()["applied"]
        assert applied == {"paste_dispenser.pad_align.canny_low": 61.5}

        toml_text = (config_dir / "machine.toml").read_text(encoding="utf-8")
        assert "61.5" in toml_text
        # tomlkit によりコメントが保持される
        assert "Cannyエッジ検出の下側閾値" in toml_text

    def test_second_apply_returns_409(self, client: TestClient, app: FastAPI):
        _complete_job_demo(client, app)
        assert client.post("/api/jobs/last/apply").status_code == 200

        assert client.post("/api/jobs/last/apply").status_code == 409

    def test_discard_then_apply_returns_409_and_discard_is_idempotent(
        self, client: TestClient, app: FastAPI
    ):
        _complete_job_demo(client, app)

        assert client.post("/api/jobs/last/discard").status_code == 200
        assert client.post("/api/jobs/last/apply").status_code == 409
        # 冪等
        assert client.post("/api/jobs/last/discard").status_code == 200

    def test_apply_without_applicable_job_returns_409(self, client: TestClient):
        assert client.post("/api/jobs/last/apply").status_code == 409

    def test_apply_while_machine_lock_held_returns_409(
        self, client: TestClient, app: FastAPI, appstate: AppState
    ):
        _complete_job_demo(client, app)

        with appstate.machine_lock("pytest-control"):
            assert client.post("/api/jobs/last/apply").status_code == 409

        # ロック解放後は反映できる
        assert client.post("/api/jobs/last/apply").status_code == 200


class TestWebSocket:
    """WS /api/ws のイベント往復（受信駆動）."""

    def test_job_demo_full_event_stream(self, client: TestClient):
        with client.websocket_connect("/api/ws") as ws:
            response = client.post(
                "/api/jobs/job_demo",
                json={"params": {"steps": 2, "interval": 0.01}},
            )
            assert response.status_code == 201

            final, history = _receive_until(
                ws,
                lambda m: m["type"] == "job_status" and m["job"]["status"] in _TERMINAL,
                answer_prompts=True,
                number_answer=60.0,
            )

            assert final["job"]["status"] == "succeeded"
            assert final["job"]["apply_available"] is True
            types = {message["type"] for message in history}
            assert {
                "job_status",
                "log",
                "progress",
                "prompt",
                "prompt_resolved",
            } <= types
            statuses = {
                message["job"]["status"]
                for message in history
                if message["type"] == "job_status"
            }
            assert "running" in statuses
            assert "waiting_input" in statuses

    def test_job_status_includes_completion_notification_policy(
        self, client: TestClient, app: FastAPI
    ):
        gate = _register_gated(app, name="notifying_ws", notify_on_completion=True)

        with client.websocket_connect("/api/ws") as ws:
            response = client.post("/api/jobs/notifying_ws", json={})
            assert response.status_code == 201
            running, _ = _receive_until(
                ws,
                lambda message: message["type"] == "job_status"
                and message["job"]["status"] in ("pending", "running"),
            )
            assert running["job"]["notify_on_completion"] is True
            gate.set()
            final, _ = _receive_until(
                ws,
                lambda message: message["type"] == "job_status"
                and message["job"]["status"] == "succeeded",
            )
            assert final["job"]["notify_on_completion"] is True

    def test_prompt_labels_are_sent_over_ws_and_job_status(
        self, client: TestClient, app: FastAPI
    ):
        def run(ctx: JobContext) -> None:
            ctx.prompt(
                PromptSpec(
                    kind="confirm",
                    message="安全確認",
                    default=True,
                    true_label="続行",
                    false_label="中止",
                )
            )

        register_synthetic(
            app.state.catalog,
            run,
            name="labeled_prompt_router",
            label="ラベル付きプロンプト",
            hidden=True,
        )

        with client.websocket_connect("/api/ws") as ws:
            response = client.post("/api/jobs/labeled_prompt_router", json={})
            assert response.status_code == 201

            prompt_msg, _ = _receive_until(ws, lambda m: m["type"] == "prompt")
            assert prompt_msg["prompt"]["true_label"] == "続行"
            assert prompt_msg["prompt"]["false_label"] == "中止"

            current = client.get("/api/jobs/current")
            assert current.status_code == 200
            pending = current.json()["job"]["pending_prompt"]
            assert pending["true_label"] == "続行"
            assert pending["false_label"] == "中止"

            ws.send_json(
                {
                    "type": "respond_prompt",
                    "prompt_id": prompt_msg["prompt"]["id"],
                    "answer": False,
                }
            )
            final, _ = _receive_until(
                ws,
                lambda m: m["type"] == "job_status" and m["job"]["status"] in _TERMINAL,
            )
            assert final["job"]["status"] == "succeeded"

    def test_abort_message_aborts_running_job(self, client: TestClient):
        with client.websocket_connect("/api/ws") as ws:
            response = client.post(
                "/api/jobs/job_demo",
                json={"params": {"steps": 500, "interval": 0.02}},
            )
            assert response.status_code == 201
            _receive_until(
                ws,
                lambda m: m["type"] == "job_status" and m["job"]["status"] == "running",
            )

            ws.send_json({"type": "abort"})

            final, _ = _receive_until(
                ws,
                lambda m: m["type"] == "job_status" and m["job"]["status"] in _TERMINAL,
            )
            assert final["job"]["status"] == "aborted"

    def test_invalid_respond_prompt_yields_error_and_keeps_connection(
        self, client: TestClient
    ):
        with client.websocket_connect("/api/ws") as ws:
            response = client.post(
                "/api/jobs/job_demo",
                json={"params": {"steps": 1, "interval": 0.01}},
            )
            assert response.status_code == 201
            prompt_msg, _ = _receive_until(ws, lambda m: m["type"] == "prompt")

            # id 不一致 → error イベント、接続は維持される
            ws.send_json(
                {
                    "type": "respond_prompt",
                    "prompt_id": "bogus-id",
                    "answer": True,
                }
            )
            error, _ = _receive_until(ws, lambda m: m["type"] == "error")
            assert error["detail"]

            # 同じ接続で正しい応答を送って完走できる
            ws.send_json(
                {
                    "type": "respond_prompt",
                    "prompt_id": prompt_msg["prompt"]["id"],
                    "answer": True,
                }
            )
            final, _ = _receive_until(
                ws,
                lambda m: m["type"] == "job_status" and m["job"]["status"] in _TERMINAL,
                answer_prompts=True,
            )
            assert final["job"]["status"] == "succeeded"

    def test_command_message_echoes_into_log(self, client: TestClient):
        with client.websocket_connect("/api/ws") as ws:
            response = client.post(
                "/api/jobs/job_demo",
                json={
                    "params": {
                        "steps": 1,
                        "interval": 0.01,
                        "command_phase": True,
                    }
                },
            )
            assert response.status_code == 201

            # prompt 2 回を消化して command フェーズへ
            resolved: list[dict[str, Any]] = []

            def _both_resolved(message: dict[str, Any]) -> bool:
                if message["type"] == "prompt_resolved":
                    resolved.append(message)
                return len(resolved) == 2

            _receive_until(ws, _both_resolved, answer_prompts=True)

            ws.send_json(
                {
                    "type": "command",
                    "command": {"type": "jog", "axis": "x", "dist": 0.1},
                }
            )
            _receive_until(ws, lambda m: m["type"] == "log" and "jog" in m["line"])

            ws.send_json({"type": "command", "command": {"type": "quit"}})
            final, _ = _receive_until(
                ws,
                lambda m: m["type"] == "job_status" and m["job"]["status"] in _TERMINAL,
            )
            assert final["job"]["status"] == "succeeded"

    def test_pcb_switch_broadcasts_state_changed(self, client: TestClient):
        with client.websocket_connect("/api/ws") as ws:
            assert (
                client.put(
                    "/api/pcb-file", json={"path": "boards/sample.kicad_pcb"}
                ).status_code
                == 200
            )

            _receive_until(ws, lambda m: m["type"] == "state_changed")


class TestCameraCalibrationApplyFlow:
    """Phase 4: camera_calibration の WS 完走 → POST /api/jobs/last/apply.

    計画書 webui-phase4.md §4「tests/webui/routers/test_jobs.py（追記）」が契約:
    checkerboard FakeCamera で WS 完走後、Apply で tmp config の machine.toml
    の calibration_file 更新 + JSON ファイル生成を実ファイルで確認する。

    計画書 webui-camera-calib.md「設計判断 a」追記: crop は job param から
    削除され machine.toml `[camera.crop]` 由来になったため、開始前に
    ConfigStore で crop を checkerboard.png（400x400）へ合わせる。
    """

    def test_ws_full_run_then_apply_writes_calibration_files(
        self,
        checkerboard_camera_client: TestClient,
        store: ConfigStore,
        config_dir: Path,
    ):
        # checkerboard.png（400x400・1 マス約 66.7px）に合わせて crop を書く
        # （既定 600 は画像をはみ出す）
        store.write_machine_settings(
            {"camera.crop.width": 400, "camera.crop.height": 400}
        )
        client = checkerboard_camera_client
        with client.websocket_connect("/api/ws") as ws:
            response = client.post(
                "/api/jobs/camera_calibration",
                json={"params": {"square_size": 10.0}},
            )
            assert response.status_code == 201

            # 撮影確認 prompt(confirm) は _receive_until が True で応答する
            final, _ = _receive_until(
                ws,
                lambda m: m["type"] == "job_status" and m["job"]["status"] in _TERMINAL,
                answer_prompts=True,
            )
            assert final["job"]["status"] == "succeeded"
            assert final["job"]["apply_available"] is True
            apply_info = final["job"]["result"]["apply"]
            assert apply_info is not None
            assert "camera.calibration_file" in apply_info["values"]

        response = client.post("/api/jobs/last/apply")

        assert response.status_code == 200
        applied = response.json()["applied"]
        filename = applied["camera.calibration_file"]
        assert isinstance(filename, str)
        assert filename.endswith(".json")

        # config ディレクトリへ実ファイルが書かれる
        toml_text = (config_dir / "machine.toml").read_text(encoding="utf-8")
        assert filename in toml_text
        # tomlkit によりコメントが保持される
        assert "非リッスンポート" in toml_text

        loaded = CalibrationResult.load(config_dir / filename)
        # 400px / 6 マス / 10mm ≈ 6.67 px/mm（素材と整合する実数値）
        assert loaded.pixel_per_mm == pytest.approx(400 / 6 / 10, rel=0.01)
        assert loaded.z_position is None  # Klipper 不通（port 7126）の best-effort


class TestPastingJobsOverWs:
    """Phase 5: pasting ジョブの WS 往復と実行中 artifacts 配信（装置なし）.

    計画書 webui-phase5.md §4「tests/webui/routers/test_jobs.py（追記）」が契約。
    Klipper は テスト用 config（port 7126 = 接続拒否）。
    """

    def test_height_plane_artifact_is_served_while_waiting_confirm(
        self, client: TestClient, copper_pcb_path: Path
    ):
        """計画点 PNG はジョブ実行中（confirm 待ち）でも /artifacts から配信される."""
        assert (
            client.put(
                "/api/pcb-file", json={"path": copper_pcb_path.as_posix()}
            ).status_code
            == 200
        )
        with client.websocket_connect("/api/ws") as ws:
            response = client.post("/api/jobs/height_plane", json={})
            assert response.status_code == 201
            job_id = response.json()["job"]["id"]

            prompt_msg, _ = _receive_until(ws, lambda m: m["type"] == "prompt")
            assert prompt_msg["prompt"]["kind"] == "confirm"

            artifact = client.get(f"/artifacts/{job_id}/planned_points.png")
            assert artifact.status_code == 200
            image = cv2.imdecode(
                np.frombuffer(artifact.content, dtype=np.uint8), cv2.IMREAD_COLOR
            )
            assert image is not None
            assert image.size > 0

            # 後始末: confirm に「いいえ」で中止する
            ws.send_json(
                {
                    "type": "respond_prompt",
                    "prompt_id": prompt_msg["prompt"]["id"],
                    "answer": False,
                }
            )
            final, _ = _receive_until(
                ws,
                lambda m: m["type"] == "job_status" and m["job"]["status"] in _TERMINAL,
            )
            assert final["job"]["status"] == "aborted"


class TestArtifacts:
    """/artifacts の配信と traversal 拒否."""

    def test_artifact_url_serves_generated_file(self, client: TestClient, app: FastAPI):
        def run(ctx: JobContext) -> JobResult:
            (ctx.artifacts_dir / "hello.txt").write_text(
                "hello artifacts", encoding="utf-8"
            )
            return JobResult(
                artifacts=(
                    Artifact(
                        label="テキスト",
                        path=f"{ctx.artifacts_dir.name}/hello.txt",
                        kind="file",
                    ),
                )
            )

        register_synthetic(
            app.state.catalog,
            run,
            name="artifact_job",
            label="成果物ジョブ",
            hidden=True,
        )
        assert client.post("/api/jobs/artifact_job", json={}).status_code == 201
        job = _wait_job_status(client, "succeeded")

        artifacts = job["result"]["artifacts"]
        assert len(artifacts) == 1
        url = artifacts[0]["url"]
        assert url.startswith("/artifacts/")

        response = client.get(url)
        assert response.status_code == 200
        assert response.text == "hello artifacts"

    def test_path_traversal_is_rejected(self, client: TestClient):
        response = client.get("/artifacts/%2e%2e/webui_state.json")

        assert response.status_code == 404
