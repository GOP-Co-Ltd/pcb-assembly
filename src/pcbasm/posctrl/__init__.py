"""Board/オフセットの位置合わせ共通制御.

座標系（長さは mm、pixel 座標だけ px）:

- board 座標: PCB 設計の座標。外形 bbox の左上が原点、+x が幅方向、+y が高さ方向
- 機械座標: ステージの G-code 座標（``XYZStage.get_position()``）
- カメラ mm 空間: 原点が画像中心。軸の向きは画像と同じ（+x 右、+y 下）
- pixel 座標: 全画面の画素位置。原点は画像左上

変換:

- ``board_transform``: board 座標 → 機械座標（:class:`BoardTransformMeasurer`）
- ``offset_transform``: カメラと機械の軸の回転ずれ（:class:`OffsetTransformMeasurer`）

``observe()`` 関数の契約: 想定→観測の Transform（カメラ mm 空間）を返す。

``observe().apply(Point2d(0, 0))`` が「検出位置 − 画像中心」になる。

計測中の失敗は例外で伝わる（``CircleDetectionError`` は RuntimeError の派生）。

``RegionAlignmentSession`` の ``align`` / ``refine`` だけは失敗を None で返す。
"""

from .aligner import RegionAligner, RegionAlignment
from .alignment import (
    BoardAlignment,
    RegionAlignmentSession,
    is_pad_refinement_target,
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
    DETECTION_MAX_ATTEMPTS,
    DETECTION_RETRY_SEC,
    BoardCalibrationResult,
    CircleDetectionError,
    OffsetObserver,
    machine_session,
    setup_board_calibration,
)

__all__ = [
    "AlignmentRegion",
    "BoardAlignment",
    "BoardCalibrationResult",
    "DETECTION_MAX_ATTEMPTS",
    "DETECTION_RETRY_SEC",
    "CircleDetectionError",
    "BoardTransformMeasurer",
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
    "is_pad_refinement_target",
    "machine_session",
    "plan_alignment_regions",
    "render_edge_match",
    "render_label",
    "setup_board_calibration",
    "to_machine_transform",
]
