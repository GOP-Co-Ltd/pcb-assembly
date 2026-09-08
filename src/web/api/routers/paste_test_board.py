"""テスト塗布基板の生成API.

リクエスト/レスポンスの形は ``pcbasm.pasting.testboard`` の attrs 値オブジェクトを
:func:`web.api.attrs_models.mirror_model` で写す（JSON のキー名・型は attrs 側が唯一の出典）。
ここに残す明示モデルは、複数のドメイン値を束ねるレスポンスとリクエストの封筒だけ。
"""

from __future__ import annotations

from typing import Annotated, Self

import cattrs
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from pcbasm.geometry.packing import Rect
from pcbasm.pasting.testboard.catalog import PadPattern
from pcbasm.pasting.testboard.config import (
    BOARD_KIND,
    BOARD_SCHEMA_VERSION,
    CUSTOM_PAD_SHAPES,
    BoardConfig,
    BoardKind,
    BoardSchemaVersion,
    BoardSpec,
    CustomPadDraft,
    CustomPadShape,
    parse_board_document,
)
from pcbasm.pasting.testboard.generator import (
    BoardPreview,
    PatternAddition,
    ResolvedConfig,
)
from pcbasm.pasting.testboard.layout import (
    LayerPolygon,
    PadLayout,
    PatternLayout,
)
from pcbasm.pcb.footprint import FootprintInfo
from web.api.attrs_models import mirror_model
from web.api.dependencies import BoardGeneratorDep

router = APIRouter(
    prefix="/api/pasting/paste-test-board",
    tags=["pasting"],
)

CONFIG_FILENAME = "pcbasm-paste-test-board.json"
BOARD_FILENAME = "pcbasm-paste-test-board.kicad_pcb"

_converter = cattrs.Converter()

# attrs → pydantic の機械写し（同じ attrs クラスは同じモデルに解決される）
BoardConfigModel = mirror_model(BoardConfig)
CustomPadDraftModel = mirror_model(CustomPadDraft)
ResolvedConfigResponse = mirror_model(ResolvedConfig, name="ResolvedConfigResponse")
PatternAdditionResponse = mirror_model(PatternAddition, name="PatternAdditionResponse")


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, from_attributes=True)


class FootprintSearchResponse(_ApiModel):
    query: str
    footprint_count: int
    results: list[mirror_model(FootprintInfo)]  # type: ignore[valid-type]


class BoardOptionsResponse(_ApiModel):
    kind: BoardKind
    schema_version: BoardSchemaVersion
    footprint_count: int
    custom_pad_shapes: list[mirror_model(CustomPadShape)]  # type: ignore[valid-type]
    config: BoardConfigModel  # type: ignore[valid-type]
    catalog: list[mirror_model(PadPattern)]  # type: ignore[valid-type]


class BoardPreviewResponse(_ApiModel):
    """Preview の解決結果（設定・カタログ・layout を 1 階層に平坦化して返す）."""

    config: BoardConfigModel  # type: ignore[valid-type]
    catalog: list[mirror_model(PadPattern)]  # type: ignore[valid-type]
    board: mirror_model(BoardSpec)  # type: ignore[valid-type]
    placement_area: mirror_model(Rect)  # type: ignore[valid-type]
    preview_bounds: mirror_model(Rect)  # type: ignore[valid-type]
    purge_pad: mirror_model(Rect)  # type: ignore[valid-type]
    purge_polygons: list[mirror_model(LayerPolygon)]  # type: ignore[valid-type]
    patterns: list[mirror_model(PatternLayout)]  # type: ignore[valid-type]
    pads: list[mirror_model(PadLayout)]  # type: ignore[valid-type]
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
        )


class BoardImportRequest(_ApiModel):
    document: dict[str, object]


class PatternAdditionRequest(_ApiModel):
    config: BoardConfigModel  # type: ignore[valid-type]
    footprint_id: Annotated[str, Field(min_length=1, max_length=300)]


class AddCustomPadRequest(_ApiModel):
    config: BoardConfigModel  # type: ignore[valid-type]
    custom_pad: CustomPadDraftModel  # type: ignore[valid-type]


def _to_config(model: BaseModel) -> BoardConfig:
    return _converter.structure(model.model_dump(), BoardConfig)


def _to_draft(model: BaseModel) -> CustomPadDraft:
    return _converter.structure(model.model_dump(), CustomPadDraft)


@router.get("/options")
def get_paste_test_board_options(
    generator: BoardGeneratorDep,
) -> BoardOptionsResponse:
    """初期レシピと検索可能なfootprint件数を返す."""

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
    )


@router.post("/custom-pads")
def add_paste_test_board_custom_pad(
    body: AddCustomPadRequest,
    generator: BoardGeneratorDep,
) -> ResolvedConfigResponse:  # type: ignore[valid-type]
    """任意寸法の基本SMDパッドを設定へ追加する."""

    resolved = generator.add_custom_pad(
        _to_config(body.config), _to_draft(body.custom_pad)
    )
    return ResolvedConfigResponse.model_validate(resolved, strict=False)


@router.get("/footprints")
def search_paste_test_board_footprints(
    generator: BoardGeneratorDep,
    query: str = Query(default="", max_length=120),
    limit: int = Query(default=30, ge=1, le=100),
) -> FootprintSearchResponse:
    """インストール済みKiCad footprintを名前で検索する."""

    return FootprintSearchResponse.model_validate(
        {
            "query": query,
            "footprint_count": generator.footprint_count,
            "results": generator.search_footprints(query, limit),
        },
        strict=False,
    )


@router.post("/patterns/from-footprint")
def add_paste_test_board_footprint_patterns(
    body: PatternAdditionRequest,
    generator: BoardGeneratorDep,
) -> PatternAdditionResponse:  # type: ignore[valid-type]
    """選択footprintの未追加パッド種を設定へ追加する."""

    addition = generator.add_footprint_patterns(
        _to_config(body.config), body.footprint_id
    )
    return PatternAdditionResponse.model_validate(addition, strict=False)


@router.post("/preview")
def preview_paste_test_board(
    body: BoardConfigModel,  # type: ignore[valid-type]
    generator: BoardGeneratorDep,
) -> BoardPreviewResponse:
    """実パッド形状から配置とpreview polygonを解決する."""

    return BoardPreviewResponse.from_core(generator.preview(_to_config(body)))


@router.post("/export")
def export_paste_test_board_config(
    body: BoardConfigModel,  # type: ignore[valid-type]
    generator: BoardGeneratorDep,
) -> Response:
    """配置可否に依らず、解決可能な設定JSONをダウンロードする."""

    return Response(
        generator.config_bytes(_to_config(body)),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{CONFIG_FILENAME}"'},
    )


@router.post("/import")
def import_paste_test_board_config(
    body: BoardImportRequest,
    generator: BoardGeneratorDep,
) -> ResolvedConfigResponse:  # type: ignore[valid-type]
    """自己識別情報を含むJSONを検証・正規化して返す（保存はしない）."""

    parsed = parse_board_document(body.document)
    if parsed is None:
        raise HTTPException(
            status_code=400,
            detail=("テスト塗布基板の設定JSONではないか、" "内容が不正です"),
        )
    return ResolvedConfigResponse.model_validate(
        generator.resolve_config(parsed), strict=False
    )


@router.post("/generate")
def generate_paste_test_board(
    body: BoardConfigModel,  # type: ignore[valid-type]
    generator: BoardGeneratorDep,
) -> Response:
    """配置可能な設定からKiCad基板を直接ダウンロードする（収まらなければ 422）."""

    payload, overflow_message = generator.board_bytes(_to_config(body))
    if payload is None:
        raise HTTPException(status_code=422, detail=overflow_message)
    return Response(
        payload,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{BOARD_FILENAME}"'},
    )
