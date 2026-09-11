"""`web.api.jobs.machine_commands` の仕様テスト.

計画書 memory/agents/implementation-planner/webui-phase5.md
「src/webui/jobs/machine_commands.py」節が契約:

- handle_machine_command は jog / home / move / relax / focus_z を処理したら
  True を返す
  - 引数不正の ValueError は ctx.log して True（ジョブは継続）
  - focus_z=None のとき focus_z コマンドは移動せず log のみで True
  - 送信失敗（Klipper 不通）の例外は握りつぶさない（ジョブを FAILED に
    するため伝播する）
- 未知 type は False を返す（log は呼び出し側の責務）

jog / move / home の実送信は Moonraker 必須のため実機区分（posctrl の
reference_point_setup 実機テストでカバー）。ここではテスト用 config
（Klipper port 7126 = 接続拒否）で戻り値契約と例外伝播のみ検証する。
Moonraker のモックは使わない（skill `testing-strategy`）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pcbasm.hal import XYZStage
from web.api.jobs.catalog import JobCatalog, JobDefinition
from web.api.jobs.context import JobContext
from web.api.jobs.machine_commands import (
    create_command_klipper,
    handle_machine_command,
)
from web.api.jobs.manager import JobManager, JobRecord, JobStatus

from .conftest import WaitUntil


def _run_handler_job(
    manager: JobManager,
    catalog: JobCatalog,
    wait_until: WaitUntil,
    commands: list[dict[str, Any]],
    *,
    focus_z: float | None,
) -> tuple[JobRecord, list[bool]]:
    """合成ジョブ内で handle_machine_command を順に呼び、戻り値列を返す."""
    results: list[bool] = []

    def run(ctx: JobContext) -> None:
        klipper = create_command_klipper(ctx.machine)
        stage = XYZStage(klipper.readonly)
        for command in commands:
            results.append(
                handle_machine_command(ctx, klipper, stage, command, focus_z=focus_z)
            )

    catalog.register(
        JobDefinition(
            name="handler",
            label="machine_commands 検証ジョブ",
            tab="dev",
            run=run,
            uses_machine=False,
        )
    )
    record = manager.start("handler", {})
    wait_until(lambda: record.status.terminal, timeout=60.0)
    return record, results


class TestHandleMachineCommand:
    """handle_machine_command の戻り値契約と例外伝播."""

    def test_unknown_type_returns_false_without_failing(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        """未知 type は False（ジョブ固有コマンドの後段判定に委ねる）."""
        record, results = _run_handler_job(
            manager, catalog, wait_until, [{"type": "record"}], focus_z=None
        )

        assert record.status == JobStatus.SUCCEEDED
        assert results == [False]

    def test_focus_z_without_calibration_logs_and_returns_true(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        """focus_z=None なら focus_z コマンドは移動せず log のみ（True）."""
        record, results = _run_handler_job(
            manager, catalog, wait_until, [{"type": "focus_z"}], focus_z=None
        )

        assert record.status == JobStatus.SUCCEEDED  # 送信しないので不通でも成功
        assert results == [True]
        assert "フォーカス" in "\n".join(record.log_lines)

    @pytest.mark.parametrize(
        "command",
        [
            {"type": "jog", "axis": "x", "dist": "abc"},
            {"type": "move"},
        ],
    )
    def test_invalid_argument_value_error_is_logged_and_returns_true(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
        command: dict[str, Any],
    ):
        """引数不正（jog の非数値 dist / move の軸なし）は ValueError を log して True（継続）."""
        record, results = _run_handler_job(
            manager, catalog, wait_until, [command], focus_z=None
        )

        assert record.status == JobStatus.SUCCEEDED  # 送信しないので不通でも成功
        assert results == [True]
        assert record.log_lines  # 実行できない旨の log が出る

    @pytest.mark.parametrize(
        ("command", "focus_z"),
        [
            ({"type": "jog", "axis": "x", "dist": 0.1}, None),
            ({"type": "home", "axes": ["x", "y", "z"]}, None),
            ({"type": "move", "x": 1.0, "y": 2.0}, None),
            ({"type": "relax"}, None),
            ({"type": "focus_z"}, 3.0),
        ],
    )
    def test_recognized_command_send_failure_propagates_to_job_failed(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
        command: dict[str, Any],
        focus_z: float | None,
    ):
        """認識した type は送信を試み、Klipper 不通の例外は伝播 → FAILED."""
        record, results = _run_handler_job(
            manager, catalog, wait_until, [command], focus_z=focus_z
        )

        assert record.status == JobStatus.FAILED
        assert record.error  # 接続エラーが error に載る
        assert results == []  # True を返す前に送信例外で中断

    def test_move_to_cap_without_recorded_cap_logs_and_returns_true(
        self, manager: JobManager, catalog: JobCatalog, wait_until: WaitUntil
    ):
        """キャップ未記録なら move_to_cap は移動せず log のみ（True）.

        nozzle-cap-parking 計画書「WebUI」節: ジョブ中 WS ミラー。未対応だと 「未知コマンド」で UX
        が破綻するため、未記録でも処理済み（True）にする。
        """
        record, results = _run_handler_job(
            manager, catalog, wait_until, [{"type": "move_to_cap"}], focus_z=None
        )

        assert record.status == JobStatus.SUCCEEDED  # 送信しないので不通でも成功
        assert results == [True]
        assert "キャップ" in "\n".join(record.log_lines)

    def test_move_to_cap_with_recorded_cap_send_failure_propagates_to_job_failed(
        self,
        manager: JobManager,
        catalog: JobCatalog,
        wait_until: WaitUntil,
        config_dir: Path,
    ):
        """キャップ記録済みは移動を送信し、Klipper 不通の例外は伝播 → FAILED."""
        path = config_dir / "machine.toml"
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n[nozzle_cap]\nx = 10.0\ny = 20.0\nz = 3.5\n",
            encoding="utf-8",
        )

        record, results = _run_handler_job(
            manager, catalog, wait_until, [{"type": "move_to_cap"}], focus_z=None
        )

        assert record.status == JobStatus.FAILED
        assert record.error  # 接続エラーが error に載る
        assert results == []  # True を返す前に送信例外で中断
