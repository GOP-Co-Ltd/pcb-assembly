"""`web.api.jobs.steps`（複数のジョブが使う台本の部品）の仕様テスト.

対話ジョブ中のマシン操作（handle_machine_command）:

- マシン操作の命令（`web.contract.models.MachineCommandRequest`）を処理したら True を返す
  - 形の誤り（値の型の誤りなど）と実行できない命令（軸の無い move など）は理由を ctx.log して True（ジョブは継続）
  - 校正ファイルに焦点 Z が無いとき focus_z コマンドは移動せず log のみで True
  - 送信失敗（Klipper 不通）の例外は握りつぶさない（ジョブを FAILED に
    するため伝播する）
- type がマシン操作でない command（ジョブ固有の type、type の欠落）は False を返す（log は呼び出し側の責務）

jog / move / home の実送信は Moonraker 必須のため受け入れ試験（docs/acceptance.md の
A1、A3）でカバーする。ここではテスト用 config
（Klipper port 7126 = 接続拒否）で戻り値契約と例外伝播のみ検証する。
Moonraker のモックは使わない（skill `testing-strategy`）。

ローディング（parse_loading_command / loading_loop）: command の解釈と、ローディング待ちの入口での作業者の呼び戻し。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pcbasm.hal import XYZStage
from pcbasm.hal.fake import FakeAudioPlayer, FakeKlipper
from pcbasm.machine.commands import COMMAND_TIMEOUT
from pcbasm.pasting.applicator import build_applicator
from tests.support.polling import wait_until
from tests.support.web import ManagerFactory, register_synthetic
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobRecord, JobStatus
from web.api.jobs.steps import (
    Extrude,
    Finish,
    InvalidLoadingCommand,
    LoadingTotals,
    Rotate,
    handle_machine_command,
    loading_loop,
    parse_loading_command,
)


def _run_handler_job(
    manager: JobManager,
    catalog: JobCatalog,
    commands: list[dict[str, Any]],
) -> tuple[JobRecord, list[bool]]:
    """合成ジョブ内で handle_machine_command を順に呼び、戻り値列を返す."""
    results: list[bool] = []

    def run(ctx: JobContext) -> None:
        klipper = ctx.klipper(COMMAND_TIMEOUT)
        stage = XYZStage(klipper)
        for command in commands:
            results.append(handle_machine_command(ctx, klipper, stage, command))

    catalog.register(
        JobDefinition(
            name="handler",
            label="machine_commands 検証ジョブ",
            run=run,
            uses_machine=False,
        )
    )
    record = manager.start("handler", {})
    wait_until(lambda: record.status.terminal, timeout=60.0)
    return record, results


@pytest.mark.usefixtures("unreachable_klipper")
class TestHandleMachineCommand:
    """handle_machine_command の戻り値契約と例外伝播."""

    @pytest.mark.parametrize(
        "command",
        [{"type": "record"}, {"axis": "x"}],
        ids=["job-command", "missing-type"],
    )
    def test_non_machine_command_returns_false_without_failing(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        command: dict[str, Any],
    ):
        """マシン操作でない type の command は False（ジョブ固有コマンドの後段判定に委ねる）."""
        record, results = _run_handler_job(manager, catalog, [command])

        assert record.status == JobStatus.SUCCEEDED
        assert results == [False]
        assert record.log_lines == ()

    def test_malformed_machine_command_logs_the_field_and_returns_true(
        self, manager: JobManager, catalog: JobCatalog
    ):
        """マシン操作の type で形が誤っていれば、誤ったフィールドを log して True（継続）."""
        record, results = _run_handler_job(
            manager,
            catalog,
            [{"type": "jog", "axis": "x", "distance": "abc"}],
        )

        assert record.status == JobStatus.SUCCEEDED  # 送信しないので不通でも成功
        assert results == [True]
        assert "jog.distance" in "\n".join(record.log_lines)

    def test_focus_z_without_calibration_logs_and_returns_true(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        config_dir: Path,
    ):
        """校正ファイルが無ければ focus_z コマンドは移動せず log のみ（True）."""
        (config_dir / "ov9281_test_fixture.json").unlink()

        record, results = _run_handler_job(manager, catalog, [{"type": "focus_z"}])

        assert record.status == JobStatus.SUCCEEDED  # 送信しないので不通でも成功
        assert results == [True]
        assert "フォーカス" in "\n".join(record.log_lines)

    def test_move_without_axes_is_logged_and_returns_true(
        self, manager: JobManager, catalog: JobCatalog
    ):
        """実行できない命令（move の軸なし）は ValueError を log して True（継続）."""
        record, results = _run_handler_job(manager, catalog, [{"type": "move"}])

        assert record.status == JobStatus.SUCCEEDED  # 送信しないので不通でも成功
        assert results == [True]
        assert record.log_lines  # 実行できない旨の log が出る

    @pytest.mark.parametrize(
        "command",
        [
            {"type": "jog", "axis": "x", "distance": 0.1},
            {"type": "home", "axes": ["x", "y", "z"]},
            {"type": "move", "x": 1.0, "y": 2.0},
            {"type": "relax"},
            {"type": "focus_z"},
            {"type": "gcode", "gcode": "M400"},
        ],
    )
    def test_recognized_command_send_failure_propagates_to_job_failed(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        command: dict[str, Any],
    ):
        """認識した type は送信を試み、Klipper 不通の例外は伝播 → FAILED."""
        record, results = _run_handler_job(manager, catalog, [command])

        assert record.status == JobStatus.FAILED
        assert record.error  # 接続エラーが error に載る
        assert results == []  # True を返す前に送信例外で中断

    def test_move_to_cap_without_recorded_cap_logs_and_returns_true(
        self, manager: JobManager, catalog: JobCatalog
    ):
        """キャップ未記録なら move_to_cap は移動せず log のみ（True）.

        ジョブ中もマシン操作パネルの操作を WS で受ける。
        未対応だと「未知コマンド」になって操作が破綻するため、未記録でも処理済み（True）にする。
        """
        record, results = _run_handler_job(manager, catalog, [{"type": "move_to_cap"}])

        assert record.status == JobStatus.SUCCEEDED  # 送信しないので不通でも成功
        assert results == [True]
        assert "キャップ" in "\n".join(record.log_lines)

    def test_move_to_cap_with_recorded_cap_send_failure_propagates_to_job_failed(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        config_dir: Path,
    ):
        """キャップ記録済みは移動を送信し、Klipper 不通の例外は伝播 → FAILED."""
        path = config_dir / "machine.toml"
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n[solder.nozzle.cap]\nx = 10.0\ny = 20.0\nz = 3.5\n",
            encoding="utf-8",
        )

        record, results = _run_handler_job(manager, catalog, [{"type": "move_to_cap"}])

        assert record.status == JobStatus.FAILED
        assert record.error  # 接続エラーが error に載る
        assert results == []  # True を返す前に送信例外で中断


class TestParseLoadingCommand:
    """parse_loading_command（純粋関数）の契約.

    ボタンを押せる段階（LoadingPanel.stages）のテンプレ/JS 整合は ui/test_pages.py の data-
    loading-stages アサートと e2e の DOM アサートが担保する。
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
            {"type": "jog", "axis": "x", "distance": 0.1},  # 機械操作（後段判定へ）
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


class TestLoadingLoopNotification:
    """`loading_loop` がローディング待ちの入口で作業者を呼び戻す.

    手動ペーストローディング（`loading`）と、ローディング段階を持つキャリブ各ジョブが
    共有する入口。押出ボタンを押すたびではなく、段階に入った 1 回だけ鳴らす契約
    （押すたびに鳴ると、装置の前に居る作業者にはただの騒音になる）。

    実 `PasteApplicator` を `FakeKlipper` + 実 `XYZStage` に載せて回す。
    """

    def test_notifies_once_on_entry_and_not_per_command(
        self,
        make_manager: ManagerFactory,
        catalog: JobCatalog,
        audio_player: FakeAudioPlayer,
    ):
        manager = make_manager(catalog)
        klipper = FakeKlipper()
        stage = XYZStage(klipper)
        totals: list[LoadingTotals] = []

        def run(ctx: JobContext) -> None:
            with build_applicator(
                klipper, stage, ctx.machine.require_solder()
            ) as applicator:
                totals.append(loading_loop(ctx, klipper, stage, applicator))

        register_synthetic(catalog, run, name="loading_loop", accepts_commands=True)

        record = manager.start("loading_loop", {})
        # 入口の drain を越えた合図。これを待たずに submit すると破棄されうる
        wait_until(lambda: len(audio_player.played) == 1)

        manager.submit_command({"type": "extrude", "amount": 0.5})
        wait_until(lambda: any("体積ローディング" in line for line in record.log_lines))
        manager.submit_command({"type": "finish"})
        wait_until(lambda: record.status.terminal, timeout=60.0)

        assert record.status == JobStatus.SUCCEEDED, record.error
        assert totals == [LoadingTotals(amount_ul=0.5, rotations=0.0)]
        # 押出コマンドを挟んでも入口の 1 回きり
        assert [sound for sound, *_ in audio_player.played] == ["prompt"]
