"""塗布ジョブ共通のコマンド解釈と対話ループの通知。"""

from __future__ import annotations

import pytest

from pcbasm.hal import XYZStage
from pcbasm.pasting.applicator import build_applicator
from tests.helpers import FakeAudioPlayer, FakeKlipper
from tests.web.api.jobs.conftest import (
    ManagerFactory,
    WaitUntil,
    answer_next_prompt,
    register_synthetic,
)
from web.api.jobs.catalog import JobCatalog
from web.api.jobs.context import JobContext
from web.api.jobs.manager import JobStatus
from web.api.jobs.pasting import (
    Extrude,
    Finish,
    InvalidLoadingCommand,
    Rotate,
    parse_loading_command,
)
from web.api.jobs.pasting.common import (
    LoadingTotals,
    prompt_positive_number,
    run_loading_loop,
)


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

    @pytest.mark.parametrize(
        "value",
        [float("inf"), float("-inf"), float("nan"), 10**400],
        ids=["inf", "-inf", "nan", "overflow-int"],
    )
    @pytest.mark.parametrize(
        "field", ["amount", "rotations", "rate", "accel", "retract_rotations"]
    )
    def test_non_finite_numbers_return_a_reason(self, field: str, value: object):
        command: dict[str, object] = (
            {"type": "extrude", "amount": 1.0}
            if field == "amount"
            else {
                "type": "extrude_rotations",
                "rotations": 1.0,
                "rate": 0.5,
                "accel": 0.5,
                "retract_rotations": 0.0,
            }
        )
        command[field] = value

        result = parse_loading_command(command)

        assert isinstance(result, InvalidLoadingCommand)
        assert result.reason


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
