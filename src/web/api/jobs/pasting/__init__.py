"""Pasting タブのジョブ定義（塗布 / 高さ計測 / ローディング / キャリブレーション / dataset）.

各ジョブは 1 モジュール 1 ジョブで ``register(catalog)`` を持つ。共有の WS コマンド解釈・
prompt・ワークフロー駆動は :mod:`web.api.jobs.pasting.common`。
"""

from web.api.jobs.catalog import JobCatalog
from web.api.jobs.pasting import (
    dataset,
    dispense_calibration,
    generate_rect_pcb,
    height_plane,
    loading,
    paste_solder,
    toolhead_offset,
)
from web.api.jobs.pasting.common import (
    APPLY_DIGITS,
    LOADING_STAGE,
    Extrude,
    Finish,
    InvalidLoadingCommand,
    LoadingTotals,
    Rotate,
    parse_loading_command,
)
from web.api.jobs.pasting.dispense_calibration import (
    CALIBRATION_MENU_STAGE,
    parse_run_calib_command,
)

__all__ = [
    "APPLY_DIGITS",
    "CALIBRATION_MENU_STAGE",
    "LOADING_STAGE",
    "Extrude",
    "Finish",
    "InvalidLoadingCommand",
    "LoadingTotals",
    "Rotate",
    "parse_loading_command",
    "parse_run_calib_command",
    "register_pasting_jobs",
]


def register_pasting_jobs(catalog: JobCatalog) -> None:
    """Pasting タブのジョブを UI の並び順で登録する."""
    for module in (
        paste_solder,
        height_plane,
        loading,
        dispense_calibration,
        dataset,
        generate_rect_pcb,
        toolhead_offset,
    ):
        module.register(catalog)
