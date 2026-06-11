"""Board/オフセットの位置合わせ共通制御."""

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
from .pad import CopperPadObserver, PadAligner, PadAlignmentResult
from .position import XYPositionAdjustor
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
    "PadAligner",
    "PadAlignmentResult",
    "PixelRect",
    "RigidEdgeMatch",
    "XYPositionAdjustor",
    "display_at_point",
    "interactive_display_at_point",
    "machine_session",
    "setup_board_calibration",
    "to_machine_transform",
    "wait_for_keypress",
]
