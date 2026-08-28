"""はんだペースト流量キャリブレーション基板の生成API."""

from __future__ import annotations

from typing import Any, Self

import attrs
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from pcbasm.pasting.paste_flow_calibration_board import (
    PASTE_FLOW_CALIBRATION_BOARD_KIND,
    PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION,
    PasteFlowCalibrationBoardConfig,
    PasteFlowCalibrationBoardConfigError,
    PasteFlowCalibrationBoardEnvironmentError,
    PasteFlowCalibrationBoardGenerator,
    PasteFlowCalibrationBoardLayout,
    PasteFlowCalibrationBoardOverflowError,
    PasteFlowCalibrationBoardSpec,
    PasteFlowCalibrationPattern,
    PasteFlowCalibrationPurgePadSpec,
    normalize_paste_flow_calibration_board_config,
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
    component_gap_mm: float = 1.0


class PasteFlowCalibrationPurgePadSpecModel(_ApiModel):
    width_mm: float = 2.0
    height_mm: float = 2.0


class PasteFlowCalibrationPatternModel(_ApiModel):
    catalog_id: str
    rotation_span_deg: float = 180.0
    rotation_count: int = 4
    repeat_count: int = 3


class PasteFlowCalibrationBoardConfigModel(_ApiModel):
    board: PasteFlowCalibrationBoardSpecModel = Field(
        default_factory=PasteFlowCalibrationBoardSpecModel
    )
    purge_pad: PasteFlowCalibrationPurgePadSpecModel = Field(
        default_factory=PasteFlowCalibrationPurgePadSpecModel
    )
    patterns: list[PasteFlowCalibrationPatternModel]

    def to_core(self) -> PasteFlowCalibrationBoardConfig:
        return PasteFlowCalibrationBoardConfig(
            board=PasteFlowCalibrationBoardSpec(**self.board.model_dump()),
            purge_pad=PasteFlowCalibrationPurgePadSpec(**self.purge_pad.model_dump()),
            patterns=tuple(
                PasteFlowCalibrationPattern(**pattern.model_dump())
                for pattern in self.patterns
            ),
        )

    @classmethod
    def from_core(cls, config: PasteFlowCalibrationBoardConfig) -> Self:
        return cls.model_validate(
            {
                "board": attrs.asdict(config.board),
                "purge_pad": attrs.asdict(config.purge_pad),
                "patterns": [attrs.asdict(pattern) for pattern in config.patterns],
            }
        )


class PasteFlowCalibrationCatalogItemModel(_ApiModel):
    catalog_id: str
    label: str
    family_id: str
    family_label: str
    default_rotation_span_deg: float
    default_rotation_count: int
    default_repeat_count: int
    initially_selected: bool


class PasteFlowCalibrationBoardOptionsResponse(_ApiModel):
    kind: str
    schema_version: int
    config: PasteFlowCalibrationBoardConfigModel
    catalog: list[PasteFlowCalibrationCatalogItemModel]


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


class PasteFlowCalibrationComponentLayoutModel(_ApiModel):
    catalog_id: str
    reference: str
    x: float
    y: float
    rotation_deg: float
    polygons: list[PasteFlowCalibrationPolygonModel]


class PasteFlowCalibrationGroupLayoutModel(_ApiModel):
    catalog_id: str
    label: str
    family_id: str
    family_label: str
    bounds: PasteFlowCalibrationBoundsModel
    cell_width_mm: float
    cell_height_mm: float
    angles_deg: list[float]
    repeat_count: int
    components: list[PasteFlowCalibrationComponentLayoutModel]


class PasteFlowCalibrationBoardLayoutResponse(_ApiModel):
    config: PasteFlowCalibrationBoardConfigModel
    board: PasteFlowCalibrationBoardSpecModel
    purge_pad: PasteFlowCalibrationBoundsModel
    purge_polygons: list[PasteFlowCalibrationPolygonModel]
    groups: list[PasteFlowCalibrationGroupLayoutModel]
    component_count: int


class PasteFlowCalibrationBoardImportRequest(_ApiModel):
    document: dict[str, Any]


@router.get("/options")
def get_paste_flow_calibration_board_options(
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> PasteFlowCalibrationBoardOptionsResponse:
    """固定カタログと初期設定を返す（footprint読込はまだ行わない）."""

    return PasteFlowCalibrationBoardOptionsResponse(
        kind=PASTE_FLOW_CALIBRATION_BOARD_KIND,
        schema_version=PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION,
        config=PasteFlowCalibrationBoardConfigModel.from_core(
            PasteFlowCalibrationBoardConfig()
        ),
        catalog=[
            PasteFlowCalibrationCatalogItemModel(
                catalog_id=item.catalog_id,
                label=item.label,
                family_id=item.family_id,
                family_label=item.family_label,
                default_rotation_span_deg=item.default_rotation_span_deg,
                default_rotation_count=item.default_rotation_count,
                default_repeat_count=item.default_repeat_count,
                initially_selected=item.initially_selected,
            )
            for item in generator.catalog
        ],
    )


@router.post("/preview")
def preview_paste_flow_calibration_board(
    body: PasteFlowCalibrationBoardConfigModel,
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> PasteFlowCalibrationBoardLayoutResponse:
    """実footprint形状から配置とpreview polygonを解決する."""

    config = _config_or_http_error(body.to_core())
    layout = _layout_or_http_error(generator, config)
    payload = attrs.asdict(layout)
    payload["config"] = PasteFlowCalibrationBoardConfigModel.from_core(config)
    return PasteFlowCalibrationBoardLayoutResponse.model_validate(payload)


@router.post("/export")
def export_paste_flow_calibration_board_config(
    body: PasteFlowCalibrationBoardConfigModel,
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> Response:
    """配置可否に依らず、構造的に正しい設定JSONをダウンロードする."""

    config = _config_or_http_error(body.to_core())
    return Response(
        generator.config_bytes(config),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{CONFIG_FILENAME}"'},
    )


@router.post("/import")
def import_paste_flow_calibration_board_config(
    body: PasteFlowCalibrationBoardImportRequest,
) -> PasteFlowCalibrationBoardConfigModel:
    """自己識別情報を含むJSONを検証・正規化して返す（保存はしない）."""

    config = parse_paste_flow_calibration_board_document(body.document)
    if config is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "はんだペースト流量キャリブレーション基板の設定JSONではないか、"
                "内容が不正です"
            ),
        )
    return PasteFlowCalibrationBoardConfigModel.from_core(config)


@router.post("/generate")
def generate_paste_flow_calibration_board(
    body: PasteFlowCalibrationBoardConfigModel,
    generator: PasteFlowCalibrationBoardGeneratorDep,
) -> Response:
    """配置可能な設定からKiCad基板を直接ダウンロードする."""

    config = body.to_core()
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


def _config_or_http_error(
    config: PasteFlowCalibrationBoardConfig,
) -> PasteFlowCalibrationBoardConfig:
    try:
        return normalize_paste_flow_calibration_board_config(config)
    except PasteFlowCalibrationBoardConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
