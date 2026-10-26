"""ジョブの一覧（機体の機能に応じたジョブを登録したカタログ）."""

from collections.abc import Collection
from pathlib import Path

from pcbasm.config import Capability
from web.api.jobs.align import (
    board_tour,
    camera_calibration,
    orthogonality_test,
    reference_point_setup,
)
from web.api.jobs.kicad import (
    extract_pcb,
    generate_grid_pcb,
    generate_rect_pcb,
    make_fill_coverage_pcb,
)
from web.api.jobs.machine import machine_check
from web.api.jobs.pasting import (
    PasteStores,
    dispense_calibration,
    height_plane,
    loading,
    nozzle_clean_test,
    paste_dataset_finalize,
    paste_solder,
    paste_volume_calibration,
    paste_volume_refit,
    toolhead_offset,
)
from web.api.jobs.runtime.catalog import JobCatalog


def default_catalog(
    capabilities: Collection[Capability], stores: PasteStores, machine_check_file: Path
) -> JobCatalog:
    """機体の機能に応じたジョブを登録したカタログを返す（これがジョブの一覧）.

    位置合わせ・カメラ・基板生成・装置診断はどの機体にも登録し、塗布のジョブは solder 機能の機体にだけ登録する。

    Args:
        capabilities: 機体の機能
        stores: 塗布のジョブが読み書きする永続データ
        machine_check_file: 装置診断の直近の結果の置き場所
    """
    catalog = JobCatalog()
    if "solder" in capabilities:
        paste_solder.register(catalog, stores)
        loading.register(catalog)
        height_plane.register(catalog)
        toolhead_offset.register(catalog)
        nozzle_clean_test.register(catalog)
        dispense_calibration.register(catalog)
        paste_volume_calibration.register(catalog, stores)
        paste_volume_refit.register(catalog, stores)
        paste_dataset_finalize.register(catalog, stores)
    reference_point_setup.register(catalog)
    camera_calibration.register(catalog)
    board_tour.register(catalog)
    orthogonality_test.register(catalog)
    extract_pcb.register(catalog)
    generate_grid_pcb.register(catalog)
    generate_rect_pcb.register(catalog)
    make_fill_coverage_pcb.register(catalog)
    machine_check.register(catalog, machine_check_file)
    return catalog
