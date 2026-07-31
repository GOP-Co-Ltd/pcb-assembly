"""`webui.jobs.manager` の仕様テスト.

計画書 webui-phase3.md「src/webui/jobs/manager.py」節 + spec §6 が契約:

- ライフサイクル: PENDING → RUNNING ⇄ WAITING_INPUT → SUCCEEDED | FAILED | ABORTED
- start: 未知ジョブ KeyError / パラメータ不正・PCB 未選択 ValueError /
  実行中 BusyError（owner = "job:<name>"）
- prompt: WAITING_INPUT 遷移 + pending_prompt 公開、respond_prompt の型検証
  （confirm→bool, number→float（int は float 化）, text→str, choice→choices 内）、
  不一致は ValueError で prompt は未解決のまま
- prompt(while_waiting=...): 応答が来るまでポーリング間隔ごとにコールバックを呼ぶ
  （待機中もジョブがライブフレームを流し続けられるようにするための契約）。
  応答後は呼ばない / 待機中 abort は while_waiting 無しと同じく JobAborted /
  コールバックの例外は握りつぶさずジョブを FAILED にする
- abort: checkpoint で JobAborted / prompt・next_command 待機中は即時 /
  アクティブジョブ無しは False
- command: submit_command → next_command、"type" キー必須、
  accepts_commands でないジョブへの submit は ValueError
- ログはリングバッファ（log_capacity）
- 排他: 実行中は装置ロックを保持、終端後に解放
- 直近 1 件のみ保持: 新 start で旧 record 置換 + 旧成果物ディレクトリ削除
- Apply: SUCCEEDED + payload のみ取得可。mark_applied / discard / 新ジョブ開始で
  LookupError
- shutdown: 実行中ジョブを ABORTED にして join（冪等）
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from webui.jobs.catalog import JobCatalog, ParamSpec
from webui.jobs.context import ApplyPayload, JobContext, JobResult, PromptSpec
from webui.jobs.manager import (
    JobManager,
    JobStatus,
    prompt_payload,
)
from webui.state import AppState, BusyError

from .conftest import (
    ManagerFactory,
    WaitUntil,
    answer_next_prompt,
    register_gated as _register_gated,
    register_synthetic as _register,
)


def _register_prompting(
    catalog: JobCatalog, spec: PromptSpec, *, name: str = "prompting"
) -> list[object]:
    """Prompt 1 回の応答を answers に記録して終了する合成ジョブを登録する."""
    answers: list[object] = []

    def run(ctx: JobContext) -> None:
        answers.append(ctx.prompt(spec))

    _register(catalog, run, name=name)
    return answers


def _register_polling_prompt(
    catalog: JobCatalog,
    spec: PromptSpec,
    while_waiting: Callable[[], None],
    *,
    name: str = "polling_prompt",
) -> list[object]:
    """`while_waiting` 付きで prompt 1 回を待つ合成ジョブを登録する."""
    answers: list[object] = []

    def run(ctx: JobContext) -> None:
        answers.append(ctx.prompt(spec, while_waiting=while_waiting))

    _register(catalog, run, name=name)
    return answers


class TestLifecycle:
    """状態遷移と start の検証."""

    def test_job_runs_to_succeeded_with_result(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        gate = threading.Event()

        def run(ctx: JobContext) -> JobResult:
            gate.wait(timeout=10.0)
            return JobResult(summary="完了")

        _register(catalog, run)
        record = manager.start("synthetic", {})

        assert record.status in (JobStatus.PENDING, JobStatus.RUNNING)
        assert manager.current() is record

        gate.set()
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert record.error is None
        assert record.result is not None
        assert record.result.summary == "完了"

    def test_exception_marks_failed_with_error_and_traceback_in_log(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        def run(ctx: JobContext) -> None:
            raise RuntimeError("意図的な失敗")

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert "意図的な失敗" in record.error
        # トレースバックはログへ
        assert "RuntimeError" in "\n".join(record.log_lines)

    def test_abort_during_checkpoint_loop_marks_aborted(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        _register_gated(catalog)
        record = manager.start("gated", {})
        wait_until(lambda: record.status == JobStatus.RUNNING)

        assert manager.request_abort() is True

        wait_until(lambda: record.status.terminal)
        assert record.status == JobStatus.ABORTED
        assert any("中止要求を受け付けました" in line for line in record.log_lines)

    def test_request_abort_without_active_job_returns_false(self, manager: JobManager):
        assert manager.request_abort() is False

    def test_request_abort_after_terminal_returns_false(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        _register(catalog, lambda ctx: None)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert manager.request_abort() is False

    def test_start_unknown_job_raises_key_error(self, manager: JobManager):
        with pytest.raises(KeyError):
            manager.start("no-such-job", {})

    def test_start_with_invalid_params_raises_value_error(
        self, manager: JobManager, catalog: JobCatalog
    ):
        _register(
            catalog,
            lambda ctx: None,
            params=(ParamSpec(name="ratio", label="比率", value_type="float"),),
        )

        with pytest.raises(ValueError):
            manager.start("synthetic", {"ratio": "abc"})

    def test_start_requires_pcb_without_selection_raises_value_error(
        self, manager: JobManager, catalog: JobCatalog
    ):
        _register(catalog, lambda ctx: None, requires_pcb=True)

        with pytest.raises(ValueError):
            manager.start("synthetic", {})

    def test_current_is_none_before_first_start(self, manager: JobManager):
        assert manager.current() is None

    def test_start_persists_declared_param_defaults(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        state: AppState,
        wait_until: WaitUntil,
    ):
        _register(
            catalog,
            lambda ctx: None,
            params=(
                ParamSpec("ratio", "比率", "float", default=1.0),
                ParamSpec("count", "回数", "int", default=3),
                ParamSpec("label", "ラベル", "str", default="x"),
            ),
            persisted_params=("ratio", "count"),
        )

        record = manager.start("synthetic", {"ratio": 2.5, "count": 4, "label": "y"})
        wait_until(lambda: record.status.terminal)

        assert state.job_param_defaults("synthetic") == {"ratio": 2.5, "count": 4}


class TestPrompt:
    """Prompt の往復と respond_prompt の検証."""

    def test_prompt_transitions_to_waiting_input_and_resolves(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        answers = _register_prompting(
            catalog, PromptSpec(kind="confirm", message="続行しますか?")
        )
        record = manager.start("prompting", {})
        wait_until(lambda: record.status == JobStatus.WAITING_INPUT)

        pending = record.pending_prompt
        assert pending is not None
        prompt_id, spec = pending
        assert spec.kind == "confirm"
        assert spec.message == "続行しますか?"

        manager.respond_prompt(prompt_id, True)
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert record.pending_prompt is None
        assert answers == [True]

    def test_prompt_labels_are_preserved_in_pending_prompt_and_payload(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        answers = _register_prompting(
            catalog,
            PromptSpec(
                kind="confirm",
                message="安全確認",
                default=True,
                true_label="続行",
                false_label="中止",
            ),
        )
        record = manager.start("prompting", {})
        wait_until(lambda: record.status == JobStatus.WAITING_INPUT)

        pending = record.pending_prompt
        assert pending is not None
        prompt_id, spec = pending
        assert spec.true_label == "続行"
        assert spec.false_label == "中止"
        assert prompt_payload(prompt_id, spec) == {
            "id": prompt_id,
            "kind": "confirm",
            "message": "安全確認",
            "default": True,
            "choices": [],
            "true_label": "続行",
            "false_label": "中止",
        }

        manager.respond_prompt(prompt_id, False)
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert answers == [False]

    @pytest.mark.parametrize(
        ("spec", "answer", "expected"),
        [
            (PromptSpec(kind="confirm", message="続行?"), False, False),
            (PromptSpec(kind="number", message="値?"), 60, 60.0),
            (PromptSpec(kind="number", message="値?"), 61.5, 61.5),
            # false_label 付き number は中止（bool False）をそのまま受け取れる
            (
                PromptSpec(kind="number", message="質量?", false_label="中止"),
                False,
                False,
            ),
            (PromptSpec(kind="text", message="名前?"), "abc", "abc"),
            (
                PromptSpec(kind="choice", message="層?", choices=("top", "bottom")),
                "bottom",
                "bottom",
            ),
        ],
    )
    def test_each_kind_round_trips_answer(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
        spec: PromptSpec,
        answer: object,
        expected: object,
    ):
        answers = _register_prompting(catalog, spec)
        record = manager.start("prompting", {})
        wait_until(lambda: record.pending_prompt is not None)

        pending = record.pending_prompt
        assert pending is not None
        manager.respond_prompt(pending[0], answer)
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert answers == [expected]
        assert type(answers[0]) is type(expected)

    @pytest.mark.parametrize(
        ("spec", "bad_answer"),
        [
            (PromptSpec(kind="confirm", message="続行?"), "yes"),
            (PromptSpec(kind="number", message="値?"), "abc"),
            # false_label 無しの number は中止（bool False）を受け付けない
            (PromptSpec(kind="number", message="値?"), False),
            (PromptSpec(kind="text", message="名前?"), 1.0),
            (
                PromptSpec(kind="choice", message="層?", choices=("top", "bottom")),
                "middle",
            ),
        ],
    )
    def test_mismatched_answer_raises_and_prompt_stays_pending(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
        spec: PromptSpec,
        bad_answer: object,
    ):
        _register_prompting(catalog, spec)
        record = manager.start("prompting", {})
        wait_until(lambda: record.pending_prompt is not None)
        pending = record.pending_prompt
        assert pending is not None
        prompt_id = pending[0]

        with pytest.raises(ValueError):
            manager.respond_prompt(prompt_id, bad_answer)

        # prompt は未解決のまま待ち続ける
        assert record.status == JobStatus.WAITING_INPUT
        assert record.pending_prompt is not None

        # 正しい応答で復帰できる（後始末を兼ねる）
        good = {"confirm": True, "number": 1.0, "text": "ok", "choice": "top"}
        manager.respond_prompt(prompt_id, good[spec.kind])
        wait_until(lambda: record.status.terminal)

    def test_respond_with_wrong_prompt_id_raises_value_error(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        _register_prompting(catalog, PromptSpec(kind="confirm", message="続行?"))
        record = manager.start("prompting", {})
        wait_until(lambda: record.pending_prompt is not None)

        with pytest.raises(ValueError):
            manager.respond_prompt("bogus-prompt-id", True)

        pending = record.pending_prompt
        assert pending is not None
        manager.respond_prompt(pending[0], True)
        wait_until(lambda: record.status.terminal)

    def test_respond_without_pending_prompt_raises_value_error(
        self, manager: JobManager
    ):
        with pytest.raises(ValueError):
            manager.respond_prompt("any-id", True)

    def test_abort_while_waiting_prompt_is_immediate(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        _register_prompting(catalog, PromptSpec(kind="confirm", message="続行?"))
        record = manager.start("prompting", {})
        wait_until(lambda: record.status == JobStatus.WAITING_INPUT)

        assert manager.request_abort() is True

        wait_until(lambda: record.status.terminal)
        assert record.status == JobStatus.ABORTED
        assert record.pending_prompt is None
        assert any("中止要求を受け付けました" in line for line in record.log_lines)


class TestPromptWhileWaiting:
    """prompt(while_waiting=...) のポーリングコールバック契約.

    「直行性テスト中は十字線が常に表示されている」要求を満たすため、prompt は応答待ちの間
    ポーリング間隔ごとに while_waiting を呼び、ジョブがライブフレームを流し続けられるように
    する。ここで固定する契約:

    - 応答が来るまで繰り返し呼ばれ、応答値は while_waiting 無しと同じく返る
    - 応答後は呼ばれない（poll ループが応答で終わる）
    - 待機中の abort は while_waiting 無しと同じく JobAborted → ABORTED
    - while_waiting の例外は握りつぶさずジョブを FAILED にする
    - resolved でない脱出（例外・abort）でも record の pending prompt は掃除される

    while_waiting=None（既定）の従来挙動は `TestPrompt` が担保している。
    """

    _SPEC = PromptSpec(kind="confirm", message="続行しますか?")

    def test_callback_is_called_repeatedly_until_the_answer_arrives(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        polls: list[int] = []
        answers = _register_polling_prompt(catalog, self._SPEC, lambda: polls.append(1))
        record = manager.start("polling_prompt", {})

        # 応答前に複数回呼ばれる（1 回きりのフックではない）
        wait_until(lambda: len(polls) >= 2)
        assert record.status == JobStatus.WAITING_INPUT

        pending = record.pending_prompt
        assert pending is not None
        manager.respond_prompt(pending[0], True)
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert answers == [True]

    def test_callback_is_not_called_after_the_answer(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        """while_waiting は渡した prompt の待機期間だけで閉じる.

        常駐スレッド等で待機期間を越えて呼ばれ続けないことを固定する。

        2 つ目の prompt のポーリング進行を待つので固定 sleep が要らない。

        応答検知の直前にもう 1 回呼ぶ実装はここでは判別できない。

        events の append は worker の単一スレッドなので順序が保証される。
        """
        events: list[str] = []

        def run(ctx: JobContext) -> None:
            ctx.prompt(
                PromptSpec(kind="confirm", message="1 点目"),
                while_waiting=lambda: events.append("first-poll"),
            )
            events.append("answered")
            ctx.prompt(
                PromptSpec(kind="confirm", message="2 点目"),
                while_waiting=lambda: events.append("second-poll"),
            )

        _register(catalog, run)
        record = manager.start("synthetic", {})
        answered: set[str] = set()
        answer_next_prompt(record, manager, True, answered)

        wait_until(lambda: events.count("second-poll") >= 3)

        after_answer = events[events.index("answered") :]
        assert "first-poll" not in after_answer

        answer_next_prompt(record, manager, True, answered)
        wait_until(lambda: record.status.terminal)
        assert record.status == JobStatus.SUCCEEDED, record.error

    def test_abort_while_polling_prompt_marks_aborted(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        polls: list[int] = []
        _register_polling_prompt(catalog, self._SPEC, lambda: polls.append(1))
        record = manager.start("polling_prompt", {})
        wait_until(lambda: len(polls) >= 2)

        assert manager.request_abort() is True

        wait_until(lambda: record.status.terminal)
        assert record.status == JobStatus.ABORTED
        assert record.pending_prompt is None
        assert any("中止要求を受け付けました" in line for line in record.log_lines)

    def test_callback_exception_fails_the_job(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        """コールバックの例外はジョブを FAILED にし、pending prompt は掃除される.

        掃除しないと終端後の job_status が pending_prompt を載せ続ける。

        すると job_console.js がモーダルを開いたままにし、エラーが読めなくなる。
        """

        def boom() -> None:
            raise RuntimeError("プレビュー配信に失敗")

        _register_polling_prompt(catalog, self._SPEC, boom)
        record = manager.start("polling_prompt", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert "プレビュー配信に失敗" in record.error
        assert record.pending_prompt is None


class TestCommands:
    """submit_command / next_command の対話キュー."""

    def test_submit_command_reaches_next_command(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        received: list[dict[str, object] | None] = []
        started = threading.Event()

        def run(ctx: JobContext) -> None:
            started.set()
            received.append(ctx.next_command(timeout=10.0))

        _register(catalog, run, accepts_commands=True)
        record = manager.start("synthetic", {})
        assert started.wait(timeout=10.0)
        wait_until(lambda: record.status == JobStatus.RUNNING)

        manager.submit_command({"type": "jog", "axis": "x", "dist": 0.1})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert received == [{"type": "jog", "axis": "x", "dist": 0.1}]

    def test_submit_command_without_type_key_raises_value_error(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        gate = _register_gated(catalog, accepts_commands=True)
        record = manager.start("gated", {})
        wait_until(lambda: record.status == JobStatus.RUNNING)

        try:
            with pytest.raises(ValueError):
                manager.submit_command({"axis": "x"})
        finally:
            gate.set()
        wait_until(lambda: record.status.terminal)

    def test_submit_to_non_accepting_job_raises_value_error(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        gate = _register_gated(catalog, accepts_commands=False)
        record = manager.start("gated", {})
        wait_until(lambda: record.status == JobStatus.RUNNING)

        try:
            with pytest.raises(ValueError):
                manager.submit_command({"type": "jog"})
        finally:
            gate.set()
        wait_until(lambda: record.status.terminal)

    def test_submit_without_active_job_raises_value_error(self, manager: JobManager):
        with pytest.raises(ValueError):
            manager.submit_command({"type": "jog"})

    def test_abort_while_waiting_command_is_immediate(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        waiting = threading.Event()

        def run(ctx: JobContext) -> None:
            waiting.set()
            ctx.next_command(timeout=None)

        _register(catalog, run, accepts_commands=True)
        record = manager.start("synthetic", {})
        assert waiting.wait(timeout=10.0)

        assert manager.request_abort() is True

        wait_until(lambda: record.status.terminal)
        assert record.status == JobStatus.ABORTED


class TestLogRingBuffer:
    """リングバッファ（log_capacity）."""

    def test_old_lines_drop_beyond_capacity(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        wait_until: WaitUntil,
    ):
        manager = make_manager(catalog, log_capacity=3)

        def run(ctx: JobContext) -> None:
            for i in range(5):
                ctx.log(f"line-{i}")

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.log_lines == ("line-2", "line-3", "line-4")


class TestExclusion:
    """装置排他ロックの共有（spec §6「排他とライフサイクル」）."""

    def test_second_start_while_running_raises_busy_error(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        gate = _register_gated(catalog)
        _register(catalog, lambda ctx: None, name="second")
        record = manager.start("gated", {})
        wait_until(lambda: record.status == JobStatus.RUNNING)

        try:
            with pytest.raises(BusyError) as exc:
                manager.start("second", {})
            assert "gated" in exc.value.owner
        finally:
            gate.set()
        wait_until(lambda: record.status.terminal)

    def test_machine_operations_blocked_while_running(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        state: AppState,
        wait_until: WaitUntil,
    ):
        gate = _register_gated(catalog)
        record = manager.start("gated", {})
        wait_until(lambda: record.status == JobStatus.RUNNING)

        try:
            with pytest.raises(BusyError):
                state.select_pcb(Path("boards/sample.kicad_pcb"))
        finally:
            gate.set()
        wait_until(lambda: record.status.terminal)

    def test_lock_is_released_after_terminal(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        state: AppState,
        wait_until: WaitUntil,
    ):
        gate = _register_gated(catalog)
        record = manager.start("gated", {})
        wait_until(lambda: record.status == JobStatus.RUNNING)
        assert state.busy_owner is not None

        gate.set()
        wait_until(lambda: record.status.terminal)
        # 終端ステータス確定 → ロック解放の順（解放までポーリングで待つ）
        wait_until(lambda: state.busy_owner is None)

        with state.machine_lock("after-job"):
            assert state.busy_owner == "after-job"


class TestSingleRecordRetention:
    """直近 1 件のみ保持・非永続（spec §6 JobRecord）."""

    def test_new_start_replaces_record_and_deletes_old_artifacts(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        captured: list[Path] = []

        def run(ctx: JobContext) -> None:
            (ctx.artifacts_dir / "out.txt").write_text("成果物", encoding="utf-8")
            captured.append(ctx.artifacts_dir)

        _register(catalog, run, name="first")
        _register(catalog, run, name="second")

        first = manager.start("first", {})
        wait_until(lambda: first.status.terminal)
        first_dir = captured[0]
        assert (first_dir / "out.txt").is_file()

        second = manager.start("second", {})
        wait_until(lambda: second.status.terminal)

        assert manager.current() is second
        assert first.id != second.id
        # 旧 record の成果物ディレクトリはディスクからも消える
        assert not first_dir.exists()


class TestApply:
    """Apply / Discard（spec §8「計測結果の Apply / Discard フロー」）."""

    _PAYLOAD = ApplyPayload(
        label="canny_low = 60.0 を設定に反映",
        values={"paste_dispenser.pad_align.canny_low": 60.0},
    )

    def _run_with_payload(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
        payload: ApplyPayload | None,
    ):
        def run(ctx: JobContext) -> JobResult:
            return JobResult(summary="計測完了", apply=payload)

        _register(catalog, run, name="measuring")
        record = manager.start("measuring", {})
        wait_until(lambda: record.status.terminal)
        assert record.status == JobStatus.SUCCEEDED
        return record

    def test_apply_payload_available_after_success(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        record = self._run_with_payload(manager, catalog, wait_until, self._PAYLOAD)

        assert record.apply_available is True
        payload = manager.apply_payload()
        assert payload.values == {"paste_dispenser.pad_align.canny_low": 60.0}

    def test_mark_applied_consumes_payload(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        self._run_with_payload(manager, catalog, wait_until, self._PAYLOAD)
        manager.apply_payload()

        manager.mark_applied()

        with pytest.raises(LookupError):
            manager.apply_payload()

    def test_discard_invalidates_payload_and_is_idempotent(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        self._run_with_payload(manager, catalog, wait_until, self._PAYLOAD)

        manager.discard()
        manager.discard()  # 冪等

        with pytest.raises(LookupError):
            manager.apply_payload()

    def test_succeeded_without_payload_raises_lookup_error(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        record = self._run_with_payload(manager, catalog, wait_until, None)

        assert record.apply_available is False
        with pytest.raises(LookupError):
            manager.apply_payload()

    def test_failed_job_has_no_payload(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        def run(ctx: JobContext) -> None:
            raise RuntimeError("失敗")

        _register(catalog, run, name="failing")
        record = manager.start("failing", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.FAILED
        with pytest.raises(LookupError):
            manager.apply_payload()

    def test_new_job_start_invalidates_payload(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        self._run_with_payload(manager, catalog, wait_until, self._PAYLOAD)
        _register(catalog, lambda ctx: None, name="next_job")

        record = manager.start("next_job", {})
        wait_until(lambda: record.status.terminal)

        with pytest.raises(LookupError):
            manager.apply_payload()

    def test_apply_payload_without_any_job_raises_lookup_error(
        self, manager: JobManager
    ):
        with pytest.raises(LookupError):
            manager.apply_payload()


class TestApplyMachineSettingsFromWorker:
    """ctx.apply_machine_settings による実行中の machine.toml 即時書き込み.

    吐出量キャリブレーションが採用値をジョブ完了（と Apply 操作）を待たずに
    永続化する経路（中止・失敗で計測結果を失わないための契約）。
    """

    def test_running_job_writes_machine_toml_before_termination(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        state: AppState,
        config_dir: Path,
        wait_until: WaitUntil,
    ):
        gate = threading.Event()

        def run(ctx: JobContext) -> None:
            ctx.apply_machine_settings({"paste_dispenser.rotations_per_ul": 42.424242})
            gate.wait(timeout=10.0)

        _register(catalog, run)
        record = manager.start("synthetic", {})
        toml_path = config_dir / "machine.toml"
        wait_until(lambda: "42.424242" in toml_path.read_text())

        assert not record.status.terminal  # ジョブ完了前に永続化されている

        gate.set()
        wait_until(lambda: record.status.terminal)
        assert record.status == JobStatus.SUCCEEDED

    def test_unknown_key_fails_the_job(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        def run(ctx: JobContext) -> None:
            ctx.apply_machine_settings({"bogus.key": 1.0})

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert "bogus.key" in record.error


class TestPresentOnTermination:
    """ジョブ終了時の PRESENT / M84（relax）ベストエフォート送信."""

    def test_present_failure_is_logged_and_succeeded_status_kept(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        state: AppState,
        wait_until: WaitUntil,
    ):
        _register(catalog, lambda ctx: None, uses_machine=True)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.SUCCEEDED
        log_text = "\n".join(record.log_lines)
        assert "PRESENT" in log_text
        assert "M84" in log_text

    def test_present_failure_does_not_change_failed_status(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        state: AppState,
        wait_until: WaitUntil,
    ):
        def run(ctx: JobContext) -> None:
            raise RuntimeError("意図的な失敗")

        _register(catalog, run, uses_machine=True)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.FAILED
        assert record.error is not None
        assert "意図的な失敗" in record.error
        log_text = "\n".join(record.log_lines)
        assert "PRESENT" in log_text
        assert "M84" in log_text

    def test_recorded_cap_failure_logs_park_message_without_present(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        state: AppState,
        wait_until: WaitUntil,
        config_dir: Path,
    ):
        """キャップ記録済み（paste）はキャップ駐機経路になり、失敗ログは退避文言のみ.

        nozzle-cap-parking 計画書「呼び出し 3 箇所の差し替え」節: catch-all 文言は
        「タスク終了時の退避に失敗: {exc}」で "PRESENT" / "M84" の語を含めない
        （フォールバック経路のログ断言との一意性を保つ）。
        """
        path = config_dir / "machine.toml"
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n[nozzle_cap]\nx = 10.0\ny = 20.0\nz = 3.5\n",
            encoding="utf-8",
        )
        _register(catalog, lambda ctx: None, uses_machine=True)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.SUCCEEDED
        log_text = "\n".join(record.log_lines)
        assert "退避に失敗" in log_text
        assert "PRESENT" not in log_text
        assert "M84" not in log_text

    def test_no_present_attempt_for_non_machine_job(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        state: AppState,
        wait_until: WaitUntil,
    ):
        """uses_machine=False（dev ジョブ相当）では終了処理を試行しない."""
        _register(catalog, lambda ctx: None, uses_machine=False)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)
        wait_until(lambda: state.busy_owner is None)

        assert record.status == JobStatus.SUCCEEDED
        assert "PRESENT" not in "\n".join(record.log_lines)
        assert "M84" not in "\n".join(record.log_lines)


class TestPcbasmLogBridge:
    """Pcbasm ログブリッジ（Phase 5）.

    計画書 webui-phase5.md「src/webui/jobs/manager.py」節が契約: ジョブ実行中、 worker
    スレッドが発する logger "pcbasm.*" の INFO ログを record.log_lines
    （ジョブコンソール）へ転送する。worker 以外のスレッド由来は転送しない。 ジョブ終了後は handler が detach
    され転送されない。
    """

    def test_worker_thread_pcbasm_log_appears_in_job_log(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        def run(ctx: JobContext) -> None:
            logging.getLogger("pcbasm.bridge_test").info("ブリッジ転送されるログ")

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert "ブリッジ転送されるログ" in "\n".join(record.log_lines)

    def test_other_thread_pcbasm_log_is_not_forwarded(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        """Preview スレッド等の pcbasm ログを混ぜない（worker スレッド限定）."""

        def run(ctx: JobContext) -> None:
            other = threading.Thread(
                target=lambda: logging.getLogger("pcbasm.bridge_test").info(
                    "別スレッドのログ"
                )
            )
            other.start()
            other.join()

        _register(catalog, run)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        assert record.status == JobStatus.SUCCEEDED
        assert "別スレッドのログ" not in "\n".join(record.log_lines)

    def test_pcbasm_log_after_job_end_is_not_forwarded(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        _register(catalog, lambda ctx: None)
        record = manager.start("synthetic", {})
        wait_until(lambda: record.status.terminal)

        logging.getLogger("pcbasm.bridge_test").info("終了後のログ")

        assert "終了後のログ" not in "\n".join(record.log_lines)


class TestUpdateCurrentParams:
    """JobManager.update_current_params（計画書「能力 1」manager 節が契約）.

    実行中ジョブに対する out-of-band な runtime_editable patch:
    - アクティブ（非終端）ジョブが無ければ ValueError
    - runtime_editable な値を適用し validated dict を返す
    - runtime_editable=False（固定）の値は ValueError
    - 適用後、実行中ジョブの ctx.params（ライブビュー）が新値を映す
    - persist=True で次回フォーム既定値（job_param_defaults）にも保存される
    """

    _PARAMS = (
        ParamSpec("board_width", "基板幅", "float", default=40.0),
        ParamSpec("line_length", "線長", "float", default=10.0, runtime_editable=True),
        ParamSpec("line_count", "本数", "int", default=3, runtime_editable=True),
    )

    def _register_param_reader(
        self, catalog: JobCatalog, gate: threading.Event, reads: list[object]
    ) -> None:
        """Gate 前後で ctx.params["line_length"] を読み reads に追記する合成ジョブ."""

        def run(ctx: JobContext) -> None:
            reads.append(ctx.params["line_length"])
            gate.wait(timeout=10.0)
            reads.append(ctx.params["line_length"])

        _register(
            catalog,
            run,
            name="reader",
            params=self._PARAMS,
            persisted_params=("line_length",),
        )

    def test_update_without_active_job_raises_value_error(self, manager: JobManager):
        with pytest.raises(ValueError):
            manager.update_current_params({"line_length": 5.0})

    def test_update_after_terminal_raises_value_error(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        _register(catalog, lambda ctx: None, params=self._PARAMS, name="quick")
        record = manager.start("quick", {})
        wait_until(lambda: record.status.terminal)

        with pytest.raises(ValueError):
            manager.update_current_params({"line_length": 5.0})

    def test_returns_validated_runtime_subset(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        gate = threading.Event()
        reads: list[object] = []
        self._register_param_reader(catalog, gate, reads)
        record = manager.start("reader", {})
        wait_until(lambda: record.status == JobStatus.RUNNING)

        try:
            applied = manager.update_current_params({"line_length": 12.5})
        finally:
            gate.set()
        wait_until(lambda: record.status.terminal)

        assert applied == {"line_length": 12.5}

    def test_fixed_param_update_raises_value_error(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        gate = threading.Event()
        reads: list[object] = []
        self._register_param_reader(catalog, gate, reads)
        record = manager.start("reader", {})
        wait_until(lambda: record.status == JobStatus.RUNNING)

        try:
            # board_width は runtime_editable=False（固定）
            with pytest.raises(ValueError):
                manager.update_current_params({"board_width": 99.0})
        finally:
            gate.set()
        wait_until(lambda: record.status.terminal)

    def test_live_params_reflect_update_on_next_read(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        """編集前の読みは旧値、編集後の読みは新値（ctx.params のライブビュー）."""
        gate = threading.Event()
        reads: list[object] = []
        self._register_param_reader(catalog, gate, reads)
        record = manager.start("reader", {"line_length": 10.0})
        # gate 前の最初の読みが入るまで待つ
        wait_until(lambda: len(reads) == 1)

        manager.update_current_params({"line_length": 20.0})
        gate.set()
        wait_until(lambda: record.status.terminal)

        # 1 回目（編集前）は開始時の値、2 回目（編集後）は新値
        assert reads == [10.0, 20.0]

    def test_record_params_reflect_update_for_get_current(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        """GET /jobs/current の元になる record.params も新値を映す."""
        gate = threading.Event()
        reads: list[object] = []
        self._register_param_reader(catalog, gate, reads)
        record = manager.start("reader", {"line_length": 10.0})
        wait_until(lambda: record.status == JobStatus.RUNNING)

        try:
            manager.update_current_params({"line_length": 30.0})
            assert record.params["line_length"] == 30.0
        finally:
            gate.set()
        wait_until(lambda: record.status.terminal)

    def test_persist_true_saves_next_form_default(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        state: AppState,
        wait_until: WaitUntil,
    ):
        gate = threading.Event()
        reads: list[object] = []
        self._register_param_reader(catalog, gate, reads)
        record = manager.start("reader", {"line_length": 10.0})
        wait_until(lambda: record.status == JobStatus.RUNNING)

        try:
            manager.update_current_params({"line_length": 42.0}, persist=True)
        finally:
            gate.set()
        wait_until(lambda: record.status.terminal)

        assert state.job_param_defaults("reader")["line_length"] == 42.0

    def test_persist_false_does_not_save_next_form_default(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        state: AppState,
        wait_until: WaitUntil,
    ):
        gate = threading.Event()
        reads: list[object] = []
        self._register_param_reader(catalog, gate, reads)
        record = manager.start("reader", {"line_length": 10.0})
        wait_until(lambda: record.status == JobStatus.RUNNING)

        try:
            # persist 省略（既定 False）はライブのみで既定値保存しない
            manager.update_current_params({"line_length": 55.0})
        finally:
            gate.set()
        wait_until(lambda: record.status.terminal)

        # 起動時に persisted_params で 10.0 が保存されたまま（55.0 にはならない）
        assert state.job_param_defaults("reader")["line_length"] == 10.0


class TestShutdown:
    """Lifespan shutdown 用の後始末."""

    def test_shutdown_aborts_running_job_and_is_idempotent(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        _register_gated(catalog)
        record = manager.start("gated", {})
        wait_until(lambda: record.status == JobStatus.RUNNING)

        manager.shutdown(timeout=10.0)

        assert record.status == JobStatus.ABORTED
        manager.shutdown(timeout=10.0)  # 冪等
