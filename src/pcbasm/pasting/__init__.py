"""ペースト塗布の制御.

公開 API は遅延解決する。これにより、データ処理や ML のサブパッケージを import するだけで、実機 HAL
やカメラドライバまで読み込まれることを避ける。
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pcbasm.config import DispenseMode, LineDirection, PasteHeight

    from .applicator import DispenseExecution, PasteApplicationResult, PasteApplicator
    from .calibration import (
        FlowCalibration,
        FlowCalibrationSet,
        MassFlowCalibration,
        MassFlowEstimate,
        estimate_mass_flow,
    )
    from .dispense_calibration import (
        DispenseRateCalibration,
        FillSpeedSweep,
        LineLayout,
        LineLayoutOverflowError,
        RateMeasurement,
        RotationsPerUlRound,
        dispense_rate_schedule,
        fill_speed_schedule,
        rate_sweep_amount,
        slot_area,
    )
    from .fill_path import (
        PasteFillPlan,
        build_pad_fill_plan_for,
        build_paste_fill_path,
        build_paste_fill_plan,
    )
    from .fill_sequence import FillSequence
    from .height import HeightPlaneMeasurer
    from .initial_purge import (
        DATASET_PURGE_PAD_ID,
        ResolvedInitialPurge,
        resolve_dataset_initial_purge,
        resolve_initial_purge,
        validate_initial_purge,
    )
    from .paste_dataset import (
        DatasetCapturedView,
        DatasetExecution,
        DatasetPolygon,
        DatasetResolvedPaste,
        DatasetView,
        PadImageCrop,
        PasteDatasetBoard,
        PasteDatasetCamera,
        PasteDatasetConfig,
        PasteDatasetMachine,
        PasteDatasetMetadata,
        PasteDatasetNozzle,
        PasteDatasetPad,
        PasteDatasetPaste,
        PasteDatasetPurge,
        PasteDatasetTotal,
        PasteDatasetWriter,
        allocate_volume_by_rotations,
        crop_pad_image,
        pad_image_crop_to_rgb,
        validate_dataset_image_margins,
    )
    from .probe import ProbeExecutor
    from .route import PasteRouteStop, plan_paste_route, routed_enabled_pads
    from .settings import (
        NUMERIC_PASTE_OVERRIDE_FIELDS,
        PASTE_OVERRIDE_FIELDS,
        EnableState,
        LevelSetting,
        PasteOverride,
        PasteSettingsModel,
        PasteSettingValue,
        ResolvedPaste,
        base_override_from_config,
        find_orphans,
        is_pad_enabled,
        resolve_node_settings,
        resolve_pad_settings,
        select_enabled_pads,
        settings_from_dict,
        settings_to_dict,
        validate_field_names,
        validate_override_values,
    )
    from .toolhead_offset import (
        MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT,
        ToolheadOffsetResult,
        ToolheadOffsetSample,
        plan_toolhead_offset_points,
    )

_MODULE_EXPORTS = {
    "pcbasm.config": ("DispenseMode", "LineDirection", "PasteHeight"),
    "pcbasm.pasting.applicator": (
        "DispenseExecution",
        "PasteApplicationResult",
        "PasteApplicator",
    ),
    "pcbasm.pasting.calibration": (
        "FlowCalibration",
        "FlowCalibrationSet",
        "MassFlowCalibration",
        "MassFlowEstimate",
        "estimate_mass_flow",
    ),
    "pcbasm.pasting.dispense_calibration": (
        "DispenseRateCalibration",
        "FillSpeedSweep",
        "LineLayout",
        "LineLayoutOverflowError",
        "RateMeasurement",
        "RotationsPerUlRound",
        "dispense_rate_schedule",
        "fill_speed_schedule",
        "rate_sweep_amount",
        "slot_area",
    ),
    "pcbasm.pasting.fill_path": (
        "PasteFillPlan",
        "build_pad_fill_plan_for",
        "build_paste_fill_path",
        "build_paste_fill_plan",
    ),
    "pcbasm.pasting.fill_sequence": ("FillSequence",),
    "pcbasm.pasting.height": ("HeightPlaneMeasurer",),
    "pcbasm.pasting.initial_purge": (
        "DATASET_PURGE_PAD_ID",
        "ResolvedInitialPurge",
        "resolve_dataset_initial_purge",
        "resolve_initial_purge",
        "validate_initial_purge",
    ),
    "pcbasm.pasting.paste_dataset": (
        "DatasetCapturedView",
        "DatasetExecution",
        "DatasetPolygon",
        "DatasetResolvedPaste",
        "DatasetView",
        "PadImageCrop",
        "PasteDatasetBoard",
        "PasteDatasetCamera",
        "PasteDatasetConfig",
        "PasteDatasetMachine",
        "PasteDatasetMetadata",
        "PasteDatasetNozzle",
        "PasteDatasetPad",
        "PasteDatasetPaste",
        "PasteDatasetPurge",
        "PasteDatasetTotal",
        "PasteDatasetWriter",
        "allocate_volume_by_rotations",
        "crop_pad_image",
        "pad_image_crop_to_rgb",
        "validate_dataset_image_margins",
    ),
    "pcbasm.pasting.probe": ("ProbeExecutor",),
    "pcbasm.pasting.route": (
        "PasteRouteStop",
        "plan_paste_route",
        "routed_enabled_pads",
    ),
    "pcbasm.pasting.settings": (
        "NUMERIC_PASTE_OVERRIDE_FIELDS",
        "PASTE_OVERRIDE_FIELDS",
        "EnableState",
        "LevelSetting",
        "PasteOverride",
        "PasteSettingsModel",
        "PasteSettingValue",
        "ResolvedPaste",
        "base_override_from_config",
        "find_orphans",
        "is_pad_enabled",
        "resolve_node_settings",
        "resolve_pad_settings",
        "select_enabled_pads",
        "settings_from_dict",
        "settings_to_dict",
        "validate_field_names",
        "validate_override_values",
    ),
    "pcbasm.pasting.toolhead_offset": (
        "MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT",
        "ToolheadOffsetResult",
        "ToolheadOffsetSample",
        "plan_toolhead_offset_points",
    ),
}

_EXPORT_MODULE = {
    exported_name: module_name
    for module_name, exported_names in _MODULE_EXPORTS.items()
    for exported_name in exported_names
}


def __getattr__(name: str) -> Any:
    """公開シンボルを初回アクセス時に解決する."""
    module_name = _EXPORT_MODULE.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})


__all__ = [
    "DispenseRateCalibration",
    "DispenseExecution",
    "EnableState",
    "FillSequence",
    "FillSpeedSweep",
    "DispenseMode",
    "FlowCalibration",
    "FlowCalibrationSet",
    "HeightPlaneMeasurer",
    "LevelSetting",
    "LineLayout",
    "LineLayoutOverflowError",
    "LineDirection",
    "MassFlowCalibration",
    "MassFlowEstimate",
    "MINIMUM_TOOLHEAD_OFFSET_SAMPLE_COUNT",
    "NUMERIC_PASTE_OVERRIDE_FIELDS",
    "PasteApplicator",
    "PasteApplicationResult",
    "DatasetCapturedView",
    "DATASET_PURGE_PAD_ID",
    "DatasetExecution",
    "DatasetPolygon",
    "DatasetResolvedPaste",
    "DatasetView",
    "PadImageCrop",
    "PasteDatasetBoard",
    "PasteDatasetCamera",
    "PasteDatasetConfig",
    "PasteDatasetMachine",
    "PasteDatasetMetadata",
    "PasteDatasetNozzle",
    "PasteDatasetPad",
    "PasteDatasetPaste",
    "PasteDatasetPurge",
    "PasteDatasetTotal",
    "PasteDatasetWriter",
    "RateMeasurement",
    "ResolvedInitialPurge",
    "RotationsPerUlRound",
    "dispense_rate_schedule",
    "fill_speed_schedule",
    "rate_sweep_amount",
    "slot_area",
    "PasteFillPlan",
    "PasteHeight",
    "PasteOverride",
    "PasteSettingValue",
    "PasteSettingsModel",
    "PASTE_OVERRIDE_FIELDS",
    "PasteRouteStop",
    "ProbeExecutor",
    "ResolvedPaste",
    "ToolheadOffsetResult",
    "ToolheadOffsetSample",
    "base_override_from_config",
    "build_pad_fill_plan_for",
    "build_paste_fill_path",
    "build_paste_fill_plan",
    "allocate_volume_by_rotations",
    "crop_pad_image",
    "pad_image_crop_to_rgb",
    "estimate_mass_flow",
    "find_orphans",
    "is_pad_enabled",
    "plan_paste_route",
    "plan_toolhead_offset_points",
    "resolve_node_settings",
    "resolve_dataset_initial_purge",
    "resolve_initial_purge",
    "resolve_pad_settings",
    "routed_enabled_pads",
    "select_enabled_pads",
    "settings_from_dict",
    "settings_to_dict",
    "validate_field_names",
    "validate_dataset_image_margins",
    "validate_initial_purge",
    "validate_override_values",
]
