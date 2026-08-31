"""はんだペースト流量キャリブレーション基板の生成API."""

from __future__ import annotations

from typing import Any, Self

import attrs
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from pcbasm.pasting.paste_flow_calibration_board import (
    PASTE_FLOW_CALIBRATION_BOARD_KIND,
    PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION,
    PASTE_FLOW_CALIBRATION_CUSTOM_PAD_SHAPES,
    PasteFlowCalibrationBoardConfig,
    PasteFlowCalibrationBoardConfigError,
    PasteFlowCalibrationBoardEnvironmentError,
    PasteFlowCalibrationBoardGenerator,
    PasteFlowCalibrationBoardLayout,
    PasteFlowCalibrationBoardOverflowError,
    PasteFlowCalibrationBoardSpec,
    PasteFlowCalibrationCustomPadDraft,
    PasteFlowCalibrationCustomPadShape,
    PasteFlowCalibrationCustomPadSpec,
    PasteFlowCalibrationPadPattern,
    PasteFlowCalibrationPattern,
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
    model_config = ConfigDict(extra="forbid")


class PasteFlowCalibrationBoardSpecModel(_ApiModel):
    width_mm: float = 40.0
    height_mm: float = 40.0
    edge_margin_mm: float = 1.0
    pad_gap_mm: float = 1.0


class PasteFlowCalibrationPurgePadSpecModel(_ApiModel):
    width_mm: float = 2.0
    height_mm: float = 2.0


class PasteFlowCalibrationPatternModel(_ApiModel):
    catalog_id: str
    rotation_span_deg: float = 180.0
    rotation_count: int = 4
    repeat_count: int = 3
    transpose: bool = False


class PasteFlowCalibrationCustomPadSpecModel(_ApiModel):
    catalog_id: str
    name: str
    shape: str
    width_mm: float
    height_mm: float
    corner_radius_mm: float = 0.0


class PasteFlowCalibrationCustomPadDraftModel(_ApiModel):
    name: str
    shape: str
    width_mm: float
    height_mm: float
    corner_radius_mm: float = 0.0

    def to_core(self) -> PasteFlowCalibrationCustomPadDraft:
        return PasteFlowCalibrationCustomPadDraft(**self.model_dump())


class PasteFlowCalibrationBoardConfigModel(_ApiModel):
    auto_pack: bool = True
    board: PasteFlowCalibrationBoardSpecModel = Field(
        default_factory=PasteFlowCalibrationBoardSpecModel
    )
    purge_pad: PasteFlowCalibrationPurgePadSpecModel = Field(
        default_factory=PasteFlowCalibrationPurgePadSpecModel
    )
    custom_pads: list[PasteFlowCalibrationCustomPadSpecModel] = Field(
        default_factory=list
    )
    patterns: list[PasteFlowCalibrationPatternModel]

    def to_core(self) -> PasteFlowCalibrationBoardConfig:
        return PasteFlowCalibrationBoardConfig(
            auto_pack=self.auto_pack,
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

    @classmethod
    def from_core(cls, config: PasteFlowCalibrationBoardConfig) -> Self:
        return cls.model_validate(
            {
                "auto_pack": config.auto_pack,
                "board": attrs.asdict(config.board),
                "purge_pad": attrs.asdict(config.purge_pad),
                "custom_pads": [
                    attrs.asdict(custom_pad) for custom_pad in config.custom_pads
                ],
                "patterns": [attrs.asdict(pattern) for pattern in config.patterns],
            }
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
    default_transpose: bool

    @classmethod
    def from_core(cls, item: PasteFlowCalibrationPadPattern) -> Self:
        return cls.model_validate(attrs.asdict(item))


class PasteFlowCalibrationFootprintModel(_ApiModel):
    footprint_id: str
    label: str
    library: str
    footprint: str


class PasteFlowCalibrationCustomPadShapeModel(_ApiModel):
    shape: str
    label: str
    uses_height: bool
    uses_corner_radius: bool

    @classmethod
    def from_core(cls, item: PasteFlowCalibrationCustomPadShape) -> Self:
        return cls.model_validate(attrs.asdict(item))


class PasteFlowCalibrationFootprintSearchResponse(_ApiModel):
    query: str
    footprint_count: int
    results: list[PasteFlowCalibrationFootprintModel]


class PasteFlowCalibrationPadPatternsResponse(_ApiModel):
    footprint_id: str
    catalog: list[PasteFlowCalibrationPadPatternModel]


class PasteFlowCalibrationBoardOptionsResponse(_ApiModel):
    kind: str
    schema_version: int
    footprint_count: int
    custom_pad_shapes: list[PasteFlowCalibrationCustomPadShapeModel]
    config: PasteFlowCalibrationBoardConfigModel
    catalog: list[PasteFlowCalibrationPadPatternModel]


class PasteFlowCalibrationConfigResponse(_ApiModel):
    config: PasteFlowCalibrationBoardConfigModel
    catalog: list[PasteFlowCalibrationPadPatternModel]


class PasteFlowCalibrationPointModel(_ApiModel):
    x: float
    y: float


class PasteFlowCalibrationPolygonModel(_ApiModel):
    layer: str
    points: list[PasteFlowCalibrationPointModel]


class PasteFlowCalibrationBoundsModel(_ApiModel):
    x: float
    y: float
    width: float
    height: float


class PasteFlowCalibrationPadLayoutModel(_ApiModel):
    catalog_id: str
    reference: str
    x: float
    y: float
    rotation_deg: float
    polygons: list[PasteFlowCalibrationPolygonModel]


class PasteFlowCalibrationGroupLayoutModel(_ApiModel):
    catalog_id: str
    label: str
    footprint_label: str
    family_id: str
    family_label: str
    bounds: PasteFlowCalibrationBoundsModel
    cell_width_mm: float
    cell_height_mm: float
    angles_deg: list[float]
    repeat_count: int
    transpose: bool
    pads: list[PasteFlowCalibrationPadLayoutModel]


class PasteFlowCalibrationBoardLayoutResponse(_ApiModel):
    config: PasteFlowCalibrationBoardConfigModel
    catalog: list[PasteFlowCalibrationPadPatternModel]
    board: PasteFlowCalibrationBoardSpecModel
    purge_pad: PasteFlowCalibrationBoundsModel
    purge_polygons: list[PasteFlowCalibrationPolygonModel]
    groups: list[PasteFlowCalibrationGroupLayoutModel]
    pad_count: int


class PasteFlowCalibrationBoardImportRequest(_ApiModel):
    document: dict[str, Any]


class PasteFlowCalibrationAddCustomPadRequest(_ApiModel):
    config: PasteFlowCalibrationBoardConfigModel
    custom_pad: PasteFlowCalibrationCustomPadDraftModel


@router.get("/options")
def get_paste_flow_calibration_board_options(
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> PasteFlowCalibrationBoardOptionsResponse:
    """初期レシピと検索可能なfootprint件数を返す."""

    config = _normalize_or_http_error(generator, PasteFlowCalibrationBoardConfig())
    catalog = _catalog_or_http_error(generator, config)
    return PasteFlowCalibrationBoardOptionsResponse(
        kind=PASTE_FLOW_CALIBRATION_BOARD_KIND,
        schema_version=PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION,
        footprint_count=_footprint_count_or_http_error(generator),
        custom_pad_shapes=[
            PasteFlowCalibrationCustomPadShapeModel.from_core(item)
            for item in PASTE_FLOW_CALIBRATION_CUSTOM_PAD_SHAPES
        ],
        config=PasteFlowCalibrationBoardConfigModel.from_core(config),
        catalog=[
            PasteFlowCalibrationPadPatternModel.from_core(item) for item in catalog
        ],
    )


@router.post("/custom-pads")
def add_paste_flow_calibration_custom_pad(
    body: PasteFlowCalibrationAddCustomPadRequest,
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> PasteFlowCalibrationConfigResponse:
    """任意寸法の基本SMDパッドを設定へ追加する."""

    try:
        config = generator.add_custom_pad(
            body.config.to_core(), body.custom_pad.to_core()
        )
    except PasteFlowCalibrationBoardConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PasteFlowCalibrationBoardEnvironmentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return PasteFlowCalibrationConfigResponse(
        config=PasteFlowCalibrationBoardConfigModel.from_core(config),
        catalog=[
            PasteFlowCalibrationPadPatternModel.from_core(item)
            for item in _catalog_or_http_error(generator, config)
        ],
    )


@router.get("/footprints")
def search_paste_flow_calibration_footprints(
    generator: PasteFlowCalibrationBoardGeneratorDep,
    query: str = Query(default="", max_length=120),
    limit: int = Query(default=30, ge=1, le=100),
) -> PasteFlowCalibrationFootprintSearchResponse:
    """インストール済みKiCad footprintを名前で検索する."""

    try:
        results = generator.search_footprints(query, limit)
        footprint_count = generator.footprint_count
    except PasteFlowCalibrationBoardConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PasteFlowCalibrationBoardEnvironmentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return PasteFlowCalibrationFootprintSearchResponse(
        query=query,
        footprint_count=footprint_count,
        results=[
            PasteFlowCalibrationFootprintModel.model_validate(attrs.asdict(item))
            for item in results
        ],
    )


@router.get("/pad-patterns")
def get_paste_flow_calibration_pad_patterns(
    generator: PasteFlowCalibrationBoardGeneratorDep,
    footprint_id: str = Query(min_length=1, max_length=300),
) -> PasteFlowCalibrationPadPatternsResponse:
    """選択footprintを回転同値なパッド種へ分類する."""

    try:
        catalog = generator.pad_patterns_for(footprint_id)
    except PasteFlowCalibrationBoardConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PasteFlowCalibrationBoardEnvironmentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return PasteFlowCalibrationPadPatternsResponse(
        footprint_id=footprint_id,
        catalog=[
            PasteFlowCalibrationPadPatternModel.from_core(item) for item in catalog
        ],
    )


@router.post("/preview")
def preview_paste_flow_calibration_board(
    body: PasteFlowCalibrationBoardConfigModel,
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> PasteFlowCalibrationBoardLayoutResponse:
    """実パッド形状から配置とpreview polygonを解決する."""

    config = _normalize_or_http_error(generator, body.to_core())
    layout = _layout_or_http_error(generator, config)
    payload = attrs.asdict(layout)
    payload["config"] = PasteFlowCalibrationBoardConfigModel.from_core(config)
    payload["catalog"] = [
        PasteFlowCalibrationPadPatternModel.from_core(item)
        for item in _catalog_or_http_error(generator, config)
    ]
    return PasteFlowCalibrationBoardLayoutResponse.model_validate(payload)


@router.post("/export")
def export_paste_flow_calibration_board_config(
    body: PasteFlowCalibrationBoardConfigModel,
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> Response:
    """配置可否に依らず、解決可能な設定JSONをダウンロードする."""

    config = _normalize_or_http_error(generator, body.to_core())
    return Response(
        generator.config_bytes(config),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{CONFIG_FILENAME}"'},
    )


@router.post("/import")
def import_paste_flow_calibration_board_config(
    body: PasteFlowCalibrationBoardImportRequest,
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> PasteFlowCalibrationConfigResponse:
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
    config = _normalize_or_http_error(generator, parsed)
    return PasteFlowCalibrationConfigResponse(
        config=PasteFlowCalibrationBoardConfigModel.from_core(config),
        catalog=[
            PasteFlowCalibrationPadPatternModel.from_core(item)
            for item in _catalog_or_http_error(generator, config)
        ],
    )


@router.post("/generate")
def generate_paste_flow_calibration_board(
    body: PasteFlowCalibrationBoardConfigModel,
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> Response:
    """配置可能な設定からKiCad基板を直接ダウンロードする."""

    config = _normalize_or_http_error(generator, body.to_core())
    _layout_or_http_error(generator, config)
    try:
        payload = generator.board_bytes(config)
    except PasteFlowCalibrationBoardEnvironmentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(
        payload,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{BOARD_FILENAME}"'},
    )


def _normalize_or_http_error(
    generator: PasteFlowCalibrationBoardGenerator,
    config: PasteFlowCalibrationBoardConfig,
) -> PasteFlowCalibrationBoardConfig:
    try:
        return generator.normalize_config(config)
    except PasteFlowCalibrationBoardConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PasteFlowCalibrationBoardEnvironmentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _catalog_or_http_error(
    generator: PasteFlowCalibrationBoardGenerator,
    config: PasteFlowCalibrationBoardConfig,
) -> tuple[PasteFlowCalibrationPadPattern, ...]:
    try:
        return generator.catalog_for_config(config)
    except PasteFlowCalibrationBoardConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PasteFlowCalibrationBoardEnvironmentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _footprint_count_or_http_error(
    generator: PasteFlowCalibrationBoardGenerator,
) -> int:
    try:
        return generator.footprint_count
    except PasteFlowCalibrationBoardEnvironmentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _layout_or_http_error(
    generator: PasteFlowCalibrationBoardGenerator,
    config: PasteFlowCalibrationBoardConfig,
) -> PasteFlowCalibrationBoardLayout:
    try:
        return generator.layout(config)
    except PasteFlowCalibrationBoardConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PasteFlowCalibrationBoardOverflowError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PasteFlowCalibrationBoardEnvironmentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
