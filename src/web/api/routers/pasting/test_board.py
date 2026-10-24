"""テスト塗布基板の生成 API.

リクエストは ``pcbasm.pasting.test_board`` の attrs 値オブジェクトへ cattrs で変換し、
レスポンスは attrs 値オブジェクトから ``model_validate(obj, strict=False, from_attributes=True)`` で詰める（tuple は list になる）。
"""

from __future__ import annotations

from typing import Annotated, Self

import cattrs
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from pcbasm.pasting.test_board.config import (
    BOARD_KIND,
    BOARD_SCHEMA_VERSION,
    CUSTOM_PAD_SHAPES,
    BoardConfig,
    BoardKind,
    BoardSchemaVersion,
    CustomPadDraft,
    CustomPadShapeId,
    PreviewLayer,
    parse_board_document,
)
from pcbasm.pasting.test_board.generator import BoardPreview
from web.api.deps import BoardGeneratorDep
from web.contract.models import Point2dInfo, RectInfo

router = APIRouter(prefix="/api/pasting/test_board")

CONFIG_FILENAME = "pcbasm-paste-test-board.json"
BOARD_FILENAME = "pcbasm-paste-test-board.kicad_pcb"

_converter = cattrs.Converter()


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# --------------------------------------------------------------------------- #
# 基板設定（リクエストとレスポンスで同じ形。JS は受け取った設定をそのまま送り返す）
# --------------------------------------------------------------------------- #
class BoardSpecModel(_ApiModel):
    width_mm: float
    height_mm: float
    edge_margin_mm: float
    pad_gap_mm: float


class PurgePadSpecModel(_ApiModel):
    width_mm: float
    height_mm: float


class CustomPadSpecModel(_ApiModel):
    catalog_id: str
    name: str
    shape: CustomPadShapeId
    width_mm: float
    height_mm: float
    corner_radius_mm: float


class PatternSpecModel(_ApiModel):
    catalog_id: str
    rotation_span_deg: float
    rotation_count: int
    repeat_count: int


class BoardConfigModel(_ApiModel):
    board: BoardSpecModel
    purge_pad: PurgePadSpecModel
    custom_pads: list[CustomPadSpecModel]
    patterns: list[PatternSpecModel]


class CustomPadDraftModel(_ApiModel):
    """任意寸法パッドの追加入力（省略した寸法と名前はドメインの既定値に従う）."""

    shape: CustomPadShapeId
    width_mm: float
    height_mm: float | None = None
    corner_radius_mm: float | None = None
    name: str | None = None


# --------------------------------------------------------------------------- #
# レスポンス
# --------------------------------------------------------------------------- #
class PadPatternModel(_ApiModel):
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


class CustomPadShapeModel(_ApiModel):
    shape: CustomPadShapeId
    label: str
    uses_height: bool
    uses_corner_radius: bool


class FootprintInfoModel(_ApiModel):
    footprint_id: str
    label: str
    library: str
    footprint: str


class LayerPolygonModel(_ApiModel):
    layer: PreviewLayer
    points: list[Point2dInfo]


class PadLayoutModel(_ApiModel):
    catalog_id: str
    display_name: str
    reference: str
    bounds: RectInfo
    x: float
    y: float
    rotation_deg: float
    polygons: list[LayerPolygonModel]


class PatternLayoutModel(_ApiModel):
    catalog_id: str
    angles_deg: list[float]


class ResolvedConfigResponse(_ApiModel):
    config: BoardConfigModel
    catalog: list[PadPatternModel]


class PatternAdditionResponse(_ApiModel):
    config: BoardConfigModel
    catalog: list[PadPatternModel]
    added_count: int


class FootprintSearchResponse(_ApiModel):
    query: str
    footprint_count: int
    results: list[FootprintInfoModel]


class BoardOptionsResponse(_ApiModel):
    kind: BoardKind
    schema_version: BoardSchemaVersion
    footprint_count: int
    custom_pad_shapes: list[CustomPadShapeModel]
    config: BoardConfigModel
    catalog: list[PadPatternModel]


class BoardPreviewResponse(_ApiModel):
    """Preview の解決結果（設定・カタログ・layout を 1 階層に平坦化して返す）."""

    config: BoardConfigModel
    catalog: list[PadPatternModel]
    board: BoardSpecModel
    placement_area: RectInfo
    preview_bounds: RectInfo
    purge_pad: RectInfo
    purge_polygons: list[LayerPolygonModel]
    patterns: list[PatternLayoutModel]
    pads: list[PadLayoutModel]
    pad_count: int
    overflow_message: str | None

    @classmethod
    def from_core(cls, preview: BoardPreview) -> Self:
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
            from_attributes=True,
        )


# --------------------------------------------------------------------------- #
# リクエストの封筒
# --------------------------------------------------------------------------- #
class BoardImportRequest(_ApiModel):
    document: dict[str, object]


class PatternAdditionRequest(_ApiModel):
    config: BoardConfigModel
    footprint_id: Annotated[str, Field(min_length=1, max_length=300)]


class AddCustomPadRequest(_ApiModel):
    config: BoardConfigModel
    custom_pad: CustomPadDraftModel


def _to_config(model: BoardConfigModel) -> BoardConfig:
    return _converter.structure(model.model_dump(), BoardConfig)


def _to_draft(model: CustomPadDraftModel) -> CustomPadDraft:
    return _converter.structure(model.model_dump(exclude_none=True), CustomPadDraft)


@router.get("/options")
def get_paste_test_board_options(
    generator: BoardGeneratorDep,
) -> BoardOptionsResponse:
    """初期レシピと検索可能な footprint 件数を返す."""

    resolved = generator.resolve_config(BoardConfig())
    return BoardOptionsResponse.model_validate(
        {
            "kind": BOARD_KIND,
            "schema_version": BOARD_SCHEMA_VERSION,
            "footprint_count": generator.footprint_count,
            "custom_pad_shapes": CUSTOM_PAD_SHAPES,
            "config": resolved.config,
            "catalog": resolved.catalog,
        },
        strict=False,
        from_attributes=True,
    )


@router.post("/custom-pads")
def add_paste_test_board_custom_pad(
    body: AddCustomPadRequest,
    generator: BoardGeneratorDep,
) -> ResolvedConfigResponse:
    """任意寸法の基本 SMD パッドを設定へ追加する."""

    resolved = generator.add_custom_pad(
        _to_config(body.config), _to_draft(body.custom_pad)
    )
    return ResolvedConfigResponse.model_validate(
        resolved, strict=False, from_attributes=True
    )


@router.get("/footprints")
def search_paste_test_board_footprints(
    generator: BoardGeneratorDep,
    query: str = Query(default="", max_length=120),
    limit: int = Query(default=30, ge=1, le=100),
) -> FootprintSearchResponse:
    """インストール済み KiCad footprint を名前で検索する."""

    return FootprintSearchResponse.model_validate(
        {
            "query": query,
            "footprint_count": generator.footprint_count,
            "results": generator.search_footprints(query, limit),
        },
        strict=False,
        from_attributes=True,
    )


@router.post("/patterns/from-footprint")
def add_paste_test_board_footprint_patterns(
    body: PatternAdditionRequest,
    generator: BoardGeneratorDep,
) -> PatternAdditionResponse:
    """選択 footprint の未追加パッド種を設定へ追加する."""

    addition = generator.add_footprint_patterns(
        _to_config(body.config), body.footprint_id
    )
    return PatternAdditionResponse.model_validate(
        addition, strict=False, from_attributes=True
    )


@router.post("/preview")
def preview_paste_test_board(
    body: BoardConfigModel,
    generator: BoardGeneratorDep,
) -> BoardPreviewResponse:
    """実パッド形状から配置と preview polygon を解決する."""

    return BoardPreviewResponse.from_core(generator.preview(_to_config(body)))


@router.post("/export")
def export_paste_test_board_config(
    body: BoardConfigModel,
    generator: BoardGeneratorDep,
) -> Response:
    """配置可否に依らず、解決可能な設定 JSON をダウンロードする."""

    return Response(
        generator.config_bytes(_to_config(body)),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{CONFIG_FILENAME}"'},
    )


@router.post("/import")
def import_paste_test_board_config(
    body: BoardImportRequest,
    generator: BoardGeneratorDep,
) -> ResolvedConfigResponse:
    """自己識別情報を含む JSON を検証・正規化して返す（保存はしない）."""

    parsed, error = parse_board_document(body.document)
    if parsed is None:
        raise HTTPException(status_code=400, detail=error)
    return ResolvedConfigResponse.model_validate(
        generator.resolve_config(parsed), strict=False, from_attributes=True
    )


@router.post("/generate")
def generate_paste_test_board(
    body: BoardConfigModel,
    generator: BoardGeneratorDep,
) -> Response:
    """配置可能な設定から KiCad 基板を直接ダウンロードする（収まらなければ 422）."""

    payload, overflow_message = generator.board_bytes(_to_config(body))
    if payload is None:
        raise HTTPException(status_code=422, detail=overflow_message)
    return Response(
        payload,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{BOARD_FILENAME}"'},
    )
