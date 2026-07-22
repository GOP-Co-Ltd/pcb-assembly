"""Board/オフセットの位置合わせ共通制御."""

from .alignment import (
    PadAlignmentSession,
    RegionAlignments,
    sorted_top_pad_regions,
)
from .board import BoardTransformMeasurer
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
    CopperPadObserver,
    PadAligner,
    PadAlignmentResult,
    PadRegion,
    plan_pad_regions,
)
from .position import XYPositionAdjustor
from .render import PadResultRenderer, render_edge_match, render_label
from .setup import (
    BoardCalibrationResult,
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
    "BoardTransformMeasurer",
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
    "PadRegion",
    "PadResultRenderer",
    "PixelRect",
    "RegionAlignments",
    "RigidEdgeMatch",
    "XYPositionAdjustor",
    "display_at_point",
    "interactive_display_at_point",
    "machine_session",
    "plan_pad_regions",
    "render_edge_match",
    "render_label",
    "setup_board_calibration",
    "sorted_top_pad_regions",
    "to_machine_transform",
    "wait_for_keypress",
    "window_sink",
]
