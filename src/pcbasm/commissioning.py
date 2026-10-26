"""立ち上げ手順の表と、各段が済んだかの判定（readiness）.

手順の表は、どの機体にもある共通の段（:data:`COMMON_STEPS`）と、機能ごとの段（:data:`CAPABILITY_STEPS`）に分ける。
新しい機体は :func:`steps` が機体の機能に応じて並べた順に段を済ませる。
docs/commissioning.md の表は全機能の段を :data:`STEPS` の順に並べる（テストが一致を確かめる）。
段は設定キーと参照ファイルがそろえば完了とし、日付は見ない。古くなったかどうかは、やり直しの契機を見て運転者が決める。
判定は読むだけで、装置には触れない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import attrs

from pcbasm.config import Capability, MachineConfig
from pcbasm.config.solder import SolderConfig
from pcbasm.machine.diagnostics import load_checks
from pcbasm.vision import load_focus_z

type StepState = Literal["done", "todo", "blocked"]

_STATE_LABELS: dict[StepState, str] = {
    "done": "完了",
    "todo": "未完了",
    "blocked": "前提待ち",
}


@attrs.frozen
class Step:
    """立ち上げの 1 段.

    Attributes:
        name: 段を行う機能の slug（ジョブならジョブ名）
        label: 表示名
        requires: 先に済ませる段の name
        done_when: 完了とみなす条件
        redo_when: やり直す契機
    """

    name: str
    label: str
    requires: tuple[str, ...]
    done_when: str
    redo_when: str


COMMON_STEPS: tuple[Step, ...] = (
    Step(
        "camera_calibration",
        "カメラ校正",
        (),
        "[camera] の calibration_file があり、焦点 Z が記録されている",
        "カメラの取り付け・ピント・解像度を変えたとき",
    ),
    Step(
        "machine_check",
        "装置診断",
        ("camera_calibration",),
        "直近の装置診断が全項目 ok",
        "printer.cfg・カメラ・スピーカーを変えたとき",
    ),
    Step(
        "reference_point_setup",
        "基準点",
        ("camera_calibration",),
        "[reference_point] に x / y がある",
        "基板を固定する治具を動かしたとき",
    ),
)

SOLDER_STEPS: tuple[Step, ...] = (
    Step(
        "nozzle_cap",
        "ノズル位置（キャップ / クリーニング）",
        ("machine_check",),
        "キャップ位置とクリーニング位置の x / y / z がある",
        "ノズルキャップやクリーナーを動かしたとき",
    ),
    Step(
        "toolhead_offset",
        "ツールヘッドオフセット",
        ("reference_point_setup",),
        "[solder.nozzle.offset] に x / y がある",
        "ノズルを付け替えたとき、カメラを動かしたとき",
    ),
    Step(
        "dispense_calibration",
        "吐出量の校正",
        ("toolhead_offset",),
        "rotations_per_ul、max_dispense_rate、max_fill_speed がある",
        "ペーストのロットやノズルを替えたとき",
    ),
    Step(
        "paste_volume_calibration",
        "塗布量の校正",
        ("dispense_calibration",),
        "[solder.volume_correction] の calibration_file が data/paste-volume-calibrations にある",
        "ペーストの種類やノズル径を替えたとき",
    ),
)

# 機能 → その機能を使えるようにする段（共通の段の後ろに、機能の並びで続ける）
CAPABILITY_STEPS: dict[Capability, tuple[Step, ...]] = {
    "solder": SOLDER_STEPS,
    "pnp": (),
}

# 全機能の段（docs/commissioning.md の表の並び）
STEPS: tuple[Step, ...] = COMMON_STEPS + tuple(
    step for capability_steps in CAPABILITY_STEPS.values() for step in capability_steps
)

_LABELS = {step.name: step.label for step in STEPS}


def steps(capabilities: tuple[Capability, ...]) -> tuple[Step, ...]:
    """機能 ``capabilities`` の機体が済ませる段を、済ませる順に返す."""
    return COMMON_STEPS + tuple(
        step for capability in capabilities for step in CAPABILITY_STEPS[capability]
    )


@attrs.frozen
class StepStatus:
    """1 段の状態.

    Attributes:
        name: 段を行う機能の slug
        label: 表示名
        state: 完了 / 未完了 / 前提待ち（未完了で、先に済ませる段が残っている）
        detail: 完了なら完了の条件、そうでなければ残っていること
        redo_when: やり直す契機
    """

    name: str
    label: str
    state: StepState
    detail: str
    redo_when: str

    @property
    def state_label(self) -> str:
        return _STATE_LABELS[self.state]


@attrs.frozen
class Readiness:
    """立ち上げ状況（:func:`steps` の順の各段の状態）."""

    steps: tuple[StepStatus, ...]

    @property
    def complete(self) -> bool:
        return all(step.state == "done" for step in self.steps)

    @property
    def label(self) -> str:
        """トップバーと機体一覧に出す要約."""
        if self.complete:
            return "立ち上げ済み"
        done = sum(step.state == "done" for step in self.steps)
        return f"立ち上げ {done}/{len(self.steps)}"


def readiness(
    machine: MachineConfig | None,
    capabilities: tuple[Capability, ...],
    machine_check_file: Path,
    calibration_dir: Path,
) -> Readiness:
    """機能 ``capabilities`` の機体の各段が済んだかを判定する.

    Args:
        machine: machine.toml（設定エラーで読めなければ None）
        capabilities: 機体の機能
        machine_check_file: 装置診断ジョブが直近の結果を残すファイル
        calibration_dir: 塗布量校正の置き場所
    """
    missing = _missing(machine, machine_check_file, calibration_dir)
    done: set[str] = set()
    statuses = []
    for step in steps(capabilities):
        reason = missing[step.name]
        pending = [_LABELS[name] for name in step.requires if name not in done]
        state: StepState
        if reason is None:
            done.add(step.name)
            state, detail = "done", step.done_when
        elif pending:
            names = "、".join(f"「{label}」" for label in pending)
            state, detail = "blocked", f"先に{names}を済ませてください"
        else:
            state, detail = "todo", reason
        statuses.append(
            StepStatus(step.name, step.label, state, detail, step.redo_when)
        )
    return Readiness(tuple(statuses))


def _missing(
    machine: MachineConfig | None, machine_check_file: Path, calibration_dir: Path
) -> dict[str, str | None]:
    """段ごとの、完了に足りないこと（完了なら None）."""
    reasons: dict[str, str | None] = dict.fromkeys(
        _LABELS, "machine.toml を読めないため判定できません"
    )
    if machine is not None:
        camera_file = machine.camera.calibration_file
        reasons.update(
            camera_calibration=(
                None
                if load_focus_z(camera_file) is not None
                else f"校正ファイル {camera_file.name} が無いか、焦点 Z が記録されていません"
            ),
            # 基準点は必須の設定なので、machine.toml を読めれば書かれている
            reference_point_setup=None,
        )
        if machine.solder is not None:
            reasons.update(_missing_solder(machine.solder, calibration_dir))
    reasons["machine_check"] = _missing_machine_check(machine_check_file)
    return reasons


def _missing_solder(
    solder: SolderConfig, calibration_dir: Path
) -> dict[str, str | None]:
    untaught = [
        name
        for name, position in (
            ("キャップ位置", solder.nozzle.cap.position),
            ("クリーニング位置", solder.nozzle.clean.position),
        )
        if position is None
    ]
    volume_file = solder.volume_correction.calibration_file
    if not volume_file:
        volume = "運転時補正に使う校正ファイルを選んでいません"
    elif not (calibration_dir / volume_file).is_file():
        volume = f"校正ファイル {volume_file} がありません"
    else:
        volume = None
    return {
        "nozzle_cap": f"{'と'.join(untaught)}を記録していません" if untaught else None,
        # ツールヘッドと吐出量は必須の設定なので、[solder] を読めれば書かれている
        "toolhead_offset": None,
        "dispense_calibration": None,
        "paste_volume_calibration": volume,
    }


def _missing_machine_check(path: Path) -> str | None:
    checks, reason = load_checks(path)
    if checks is None:
        return reason
    failed = [check.name for check in checks if not check.ok]
    if failed:
        return f"直近の診断で不合格: {'、'.join(failed)}"
    return None
