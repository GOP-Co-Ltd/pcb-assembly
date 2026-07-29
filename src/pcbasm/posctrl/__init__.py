"""Board/オフセットの位置合わせ共通制御."""

from .alignment import (
    ComponentAlignments,
    PadAlignmentSession,
    sorted_top_component_pads,
)
from .board import BoardTransformMeasurer
from .checkerboard_scan import (
    CheckerboardScanner,
    ScanFailure,
    ScanOutcome,
    ScanProgress,
)
from .copper import (
    CopperEdgeMatcher,
    CopperProjection,
    CopperProjector,
    EdgeMatch,
    PixelRect,
    RigidEdgeMatch,
)
from .correction import to_machine_transform
from .offset import OffsetTransformMeasurer
from .orthogonality import OrthogonalityMetrics
from .pad import (
    ComponentPads,
    CopperPadObserver,
    PadAligner,
    PadAlignmentResult,
    group_pads_by_component,
)
from .position import XYPositionAdjustor
from .render import PadResultRenderer, render_edge_match, render_label
from .setup import (
    BoardCalibrationResult,
    CircleDetectionError,
    OffsetObserver,
    machine_session,
    setup_board_calibration,
)
from .tour import (
    display_at_point,
    interactive_display_at_point,
    wait_for_keypress,
    window_sink,
)

__all__ = [
    "BoardCalibrationResult",
    "CircleDetectionError",
    "ComponentAlignments",
    "ComponentPads",
    "BoardTransformMeasurer",
    "CheckerboardScanner",
    "CopperEdgeMatcher",
    "CopperPadObserver",
    "CopperProjection",
    "CopperProjector",
    "EdgeMatch",
    "OffsetObserver",
    "OffsetTransformMeasurer",
    "OrthogonalityMetrics",
    "PadAligner",
    "PadAlignmentResult",
    "PadAlignmentSession",
    "PadResultRenderer",
    "PixelRect",
    "RigidEdgeMatch",
    "ScanFailure",
    "ScanOutcome",
    "ScanProgress",
    "XYPositionAdjustor",
    "display_at_point",
    "group_pads_by_component",
    "interactive_display_at_point",
    "machine_session",
    "render_edge_match",
    "render_label",
    "setup_board_calibration",
    "sorted_top_component_pads",
    "to_machine_transform",
    "wait_for_keypress",
    "window_sink",
]
