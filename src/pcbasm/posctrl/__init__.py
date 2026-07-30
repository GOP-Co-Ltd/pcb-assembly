"""Board/オフセットの位置合わせ共通制御."""

from .aligner import RegionAligner, RegionAlignment
from .alignment import (
    BoardAlignment,
    BorrowedCorrections,
    RegionAlignmentSession,
    corrected_board_transform,
    corrected_pad_targets,
)
from .board import BoardTransformMeasurer
from .copper import (
    CopperEdgeMatcher,
    CopperProjection,
    CopperProjector,
    EdgeMatch,
    PixelRect,
    centered_roi,
)
from .correction import to_machine_transform
from .offset import OffsetTransformMeasurer
from .orthogonality import OrthogonalityMetrics
from .position import XYPositionAdjustor
from .region import AlignmentRegion, plan_alignment_regions
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
    "AlignmentRegion",
    "BoardAlignment",
    "BoardCalibrationResult",
    "BoardTransformMeasurer",
    "BorrowedCorrections",
    "CircleDetectionError",
    "CopperEdgeMatcher",
    "CopperProjection",
    "CopperProjector",
    "EdgeMatch",
    "OffsetObserver",
    "OffsetTransformMeasurer",
    "OrthogonalityMetrics",
    "PadResultRenderer",
    "PixelRect",
    "RegionAligner",
    "RegionAlignment",
    "RegionAlignmentSession",
    "XYPositionAdjustor",
    "centered_roi",
    "corrected_board_transform",
    "corrected_pad_targets",
    "display_at_point",
    "interactive_display_at_point",
    "machine_session",
    "plan_alignment_regions",
    "render_edge_match",
    "render_label",
    "setup_board_calibration",
    "to_machine_transform",
    "wait_for_keypress",
    "window_sink",
]
