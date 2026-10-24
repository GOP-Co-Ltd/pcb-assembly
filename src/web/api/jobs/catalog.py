"""ジョブの一覧（全ジョブを登録したカタログ）."""

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


def default_catalog(stores: PasteStores) -> JobCatalog:
    """全ジョブを登録したカタログを返す（これがジョブの一覧）."""
    catalog = JobCatalog()
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
    machine_check.register(catalog)
    return catalog
