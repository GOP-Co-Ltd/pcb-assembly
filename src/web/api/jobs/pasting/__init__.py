"""塗布系のジョブ定義（塗布 / 高さ計測 / ローディング / キャリブレーション / dataset）.

各ジョブは 1 モジュール 1 ジョブで ``register(catalog)`` を持つ。共有の WS コマンド解釈・
prompt・ワークフロー駆動は :mod:`web.api.jobs.pasting.common`。
"""

from web.api.jobs.catalog import JobCatalog
from web.api.jobs.pasting import (
    dataset_finalize,
    dispense_calibration,
    generate_rect_pcb,
    height_plane,
    loading,
    paste_solder,
    paste_volume_calibration,
    paste_volume_refit,
    toolhead_offset,
)


def register_pasting_jobs(catalog: JobCatalog) -> None:
    """塗布系のジョブを登録する."""
    for module in (
        paste_solder,
        height_plane,
        loading,
        dispense_calibration,
        paste_volume_calibration,
        paste_volume_refit,
        dataset_finalize,
        generate_rect_pcb,
        toolhead_offset,
    ):
        module.register(catalog)
