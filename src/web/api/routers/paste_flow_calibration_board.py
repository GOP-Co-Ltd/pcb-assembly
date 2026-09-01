"""はんだペースト流量キャリブレーション基板の生成API."""

from __future__ import annotations

from typing import Annotated, Self

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from pcbasm.pasting.paste_flow_calibration_board import (
    PASTE_FLOW_CALIBRATION_BOARD_KIND,
    PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION,
    PASTE_FLOW_CALIBRATION_CUSTOM_PAD_SHAPES,
    PasteFlowCalibrationBoardConfig,
    PasteFlowCalibrationBoardKind,
    PasteFlowCalibrationBoardPreview,
    PasteFlowCalibrationBoardSchemaVersion,
    PasteFlowCalibrationBoardSpec,
    PasteFlowCalibrationCustomPadDraft,
    PasteFlowCalibrationCustomPadShapeId,
    PasteFlowCalibrationCustomPadSpec,
    PasteFlowCalibrationPattern,
    PasteFlowCalibrationPreviewLayer,
    PasteFlowCalibrationPurgePadSpec,
    parse_paste_flow_calibration_board_document,
)
from web.api.dependencies import PasteFlowCalibrationBoardGeneratorDep

router = APIRouter(
    prefix="/api/pasting/paste-flow-calibration-board",
    tags=["paste-flow-calibration-board"],
)

CONFIG_FILENAME = "pcbasm-paste-flow-calibration-board.json"
BOARD_FILENAME = "pcbasm-paste-flow-calibration-board.kicad_pcb"


class _ApiModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        from_attributes=True,
    )


class PasteFlowCalibrationBoardSpecModel(_ApiModel):
    width_mm: float
    height_mm: float
    edge_margin_mm: float
    pad_gap_mm: float


class PasteFlowCalibrationPurgePadSpecModel(_ApiModel):
    width_mm: float
    height_mm: float


class PasteFlowCalibrationPatternModel(_ApiModel):
    catalog_id: str
    rotation_span_deg: float
    rotation_count: int
    repeat_count: int


class PasteFlowCalibrationCustomPadSpecModel(_ApiModel):
    catalog_id: str
    name: str
    shape: PasteFlowCalibrationCustomPadShapeId
    width_mm: float
    height_mm: float
    corner_radius_mm: float


class PasteFlowCalibrationCustomPadDraftModel(_ApiModel):
    shape: PasteFlowCalibrationCustomPadShapeId
    width_mm: float
    height_mm: float = 0.0
    corner_radius_mm: float = 0.0
    name: str = ""

    def to_core(self) -> PasteFlowCalibrationCustomPadDraft:
        return PasteFlowCalibrationCustomPadDraft(**self.model_dump())


class PasteFlowCalibrationBoardConfigModel(_ApiModel):
    board: PasteFlowCalibrationBoardSpecModel
    purge_pad: PasteFlowCalibrationPurgePadSpecModel
    custom_pads: list[PasteFlowCalibrationCustomPadSpecModel]
    patterns: list[PasteFlowCalibrationPatternModel]

    def to_core(self) -> PasteFlowCalibrationBoardConfig:
        return PasteFlowCalibrationBoardConfig(
            board=PasteFlowCalibrationBoardSpec(**self.board.model_dump()),
            purge_pad=PasteFlowCalibrationPurgePadSpec(**self.purge_pad.model_dump()),
            custom_pads=tuple(
                PasteFlowCalibrationCustomPadSpec(**item.model_dump())
                for item in self.custom_pads
            ),
            patterns=tuple(
                PasteFlowCalibrationPattern(**pattern.model_dump())
                for pattern in self.patterns
            ),
        )


class PasteFlowCalibrationPadPatternModel(_ApiModel):
    catalog_id: str
    footprint_id: str
    footprint_label: str
    label: str
    family_id: str
    family_label: str
    library: str
    footprint: str
    source_pad_numbers: list[str]
    source_pad_count: int
    pad_width_mm: float
    pad_height_mm: float
    default_rotation_span_deg: float
    default_rotation_count: int
    default_repeat_count: int


class PasteFlowCalibrationFootprintModel(_ApiModel):
    footprint_id: str
    label: str
    library: str
    footprint: str


class PasteFlowCalibrationCustomPadShapeModel(_ApiModel):
    shape: PasteFlowCalibrationCustomPadShapeId
    label: str
    uses_height: bool
    uses_corner_radius: bool


class PasteFlowCalibrationFootprintSearchResponse(_ApiModel):
    query: str
    footprint_count: int
    results: list[PasteFlowCalibrationFootprintModel]


class PasteFlowCalibrationBoardOptionsResponse(_ApiModel):
    kind: PasteFlowCalibrationBoardKind
    schema_version: PasteFlowCalibrationBoardSchemaVersion
    footprint_count: int
    custom_pad_shapes: list[PasteFlowCalibrationCustomPadShapeModel]
    config: PasteFlowCalibrationBoardConfigModel
    catalog: list[PasteFlowCalibrationPadPatternModel]


class PasteFlowCalibrationResolvedConfigResponse(_ApiModel):
    config: PasteFlowCalibrationBoardConfigModel
    catalog: list[PasteFlowCalibrationPadPatternModel]


class PasteFlowCalibrationPatternAdditionResponse(
    PasteFlowCalibrationResolvedConfigResponse
):
    added_count: int


class PasteFlowCalibrationPointModel(_ApiModel):
    x: float
    y: float


class PasteFlowCalibrationPolygonModel(_ApiModel):
    layer: PasteFlowCalibrationPreviewLayer
    points: list[PasteFlowCalibrationPointModel]


class PasteFlowCalibrationBoundsModel(_ApiModel):
    x: float
    y: float
    width: float
    height: float


class PasteFlowCalibrationPadLayoutModel(_ApiModel):
    catalog_id: str
    display_name: str
    reference: str
    bounds: PasteFlowCalibrationBoundsModel
    x: float
    y: float
    rotation_deg: float
    polygons: list[PasteFlowCalibrationPolygonModel]


class PasteFlowCalibrationPatternLayoutModel(_ApiModel):
    catalog_id: str
    angles_deg: list[float]


class PasteFlowCalibrationBoardPreviewResponse(_ApiModel):
    config: PasteFlowCalibrationBoardConfigModel
    catalog: list[PasteFlowCalibrationPadPatternModel]
    board: PasteFlowCalibrationBoardSpecModel
    placement_area: PasteFlowCalibrationBoundsModel
    preview_bounds: PasteFlowCalibrationBoundsModel
    purge_pad: PasteFlowCalibrationBoundsModel
    purge_polygons: list[PasteFlowCalibrationPolygonModel]
    patterns: list[PasteFlowCalibrationPatternLayoutModel]
    pads: list[PasteFlowCalibrationPadLayoutModel]
    pad_count: int
    overflow_message: str | None

    @classmethod
    def from_core(cls, preview: PasteFlowCalibrationBoardPreview) -> Self:
        layout = preview.layout
        return cls.model_validate(
            {
                "config": preview.config,
                "catalog": preview.catalog,
                "board": layout.board,
                "placement_area": layout.placement_area,
                "preview_bounds": layout.preview_bounds,
                "purge_pad": layout.purge_pad,
                "purge_polygons": layout.purge_polygons,
                "patterns": layout.patterns,
                "pads": layout.pads,
                "pad_count": layout.pad_count,
                "overflow_message": preview.overflow_message,
            },
            strict=False,
        )


class PasteFlowCalibrationBoardImportRequest(_ApiModel):
    document: dict[str, object]


class PasteFlowCalibrationPatternAdditionRequest(_ApiModel):
    config: PasteFlowCalibrationBoardConfigModel
    footprint_id: Annotated[str, Field(min_length=1, max_length=300)]


class PasteFlowCalibrationAddCustomPadRequest(_ApiModel):
    config: PasteFlowCalibrationBoardConfigModel
    custom_pad: PasteFlowCalibrationCustomPadDraftModel


@router.get("/options")
def get_paste_flow_calibration_board_options(
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> PasteFlowCalibrationBoardOptionsResponse:
    """初期レシピと検索可能なfootprint件数を返す."""

    resolved = generator.resolve_config(PasteFlowCalibrationBoardConfig())
    return PasteFlowCalibrationBoardOptionsResponse.model_validate(
        {
            "kind": PASTE_FLOW_CALIBRATION_BOARD_KIND,
            "schema_version": PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION,
            "footprint_count": generator.footprint_count,
            "custom_pad_shapes": PASTE_FLOW_CALIBRATION_CUSTOM_PAD_SHAPES,
            "config": resolved.config,
            "catalog": resolved.catalog,
        },
        strict=False,
    )


@router.post("/custom-pads")
def add_paste_flow_calibration_custom_pad(
    body: PasteFlowCalibrationAddCustomPadRequest,
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> PasteFlowCalibrationResolvedConfigResponse:
    """任意寸法の基本SMDパッドを設定へ追加する."""

    resolved = generator.add_custom_pad(
        body.config.to_core(), body.custom_pad.to_core()
    )
    return PasteFlowCalibrationResolvedConfigResponse.model_validate(
        resolved,
        strict=False,
    )


@router.get("/footprints")
def search_paste_flow_calibration_footprints(
    generator: PasteFlowCalibrationBoardGeneratorDep,
    query: str = Query(default="", max_length=120),
    limit: int = Query(default=30, ge=1, le=100),
) -> PasteFlowCalibrationFootprintSearchResponse:
    """インストール済みKiCad footprintを名前で検索する."""

    return PasteFlowCalibrationFootprintSearchResponse.model_validate(
        {
            "query": query,
            "footprint_count": generator.footprint_count,
            "results": generator.search_footprints(query, limit),
        },
        strict=False,
    )


@router.post("/patterns/from-footprint")
def add_paste_flow_calibration_footprint_patterns(
    body: PasteFlowCalibrationPatternAdditionRequest,
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> PasteFlowCalibrationPatternAdditionResponse:
    """選択footprintの未追加パッド種を設定へ追加する."""

    addition = generator.add_footprint_patterns(
        body.config.to_core(), body.footprint_id
    )
    return PasteFlowCalibrationPatternAdditionResponse.model_validate(
        addition,
        strict=False,
    )


@router.post("/preview")
def preview_paste_flow_calibration_board(
    body: PasteFlowCalibrationBoardConfigModel,
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> PasteFlowCalibrationBoardPreviewResponse:
    """実パッド形状から配置とpreview polygonを解決する."""

    return PasteFlowCalibrationBoardPreviewResponse.from_core(
        generator.preview(body.to_core())
    )


@router.post("/export")
def export_paste_flow_calibration_board_config(
    body: PasteFlowCalibrationBoardConfigModel,
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> Response:
    """配置可否に依らず、解決可能な設定JSONをダウンロードする."""

    return Response(
        generator.config_bytes(body.to_core()),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{CONFIG_FILENAME}"'},
    )


@router.post("/import")
def import_paste_flow_calibration_board_config(
    body: PasteFlowCalibrationBoardImportRequest,
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> PasteFlowCalibrationResolvedConfigResponse:
    """自己識別情報を含むJSONを検証・正規化して返す（保存はしない）."""

    parsed = parse_paste_flow_calibration_board_document(body.document)
    if parsed is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "はんだペースト流量キャリブレーション基板の設定JSONではないか、"
                "内容が不正です"
            ),
        )
    return PasteFlowCalibrationResolvedConfigResponse.model_validate(
        generator.resolve_config(parsed),
        strict=False,
    )


@router.post("/generate")
def generate_paste_flow_calibration_board(
    body: PasteFlowCalibrationBoardConfigModel,
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> Response:
    """配置可能な設定からKiCad基板を直接ダウンロードする."""

    return Response(
        generator.board_bytes(body.to_core()),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{BOARD_FILENAME}"'},
    )
