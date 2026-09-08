"""データセット収集のレイアウト preview API（装置を動かさない読み取り専用計算）.

セル配置・容量判定・量割り当て・派生カウント（総点数・撮影枚数）は
:func:`pcbasm.pasting.dataset.plan.preview_dot_grid` が唯一の出典で、ここは
ジョブ ParamSpec 名の受け取りとレスポンス変換だけを担う。

配置不能な設定でも 200 で返し、理由は body の ``error`` に載せる（WebUI 側で配置図を
消さずに理由を出せるようにするため）。装置を動かさないので操作権は要求しない。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from pcbasm.pasting.dataset.plan import DotGridPreview, preview_dot_grid
from web.api.attrs_models import mirror_model
from web.api.jobs.pasting.dataset import grid_spec_from_params

router = APIRouter(prefix="/api/pasting", tags=["pasting"])

DatasetLayoutResponse = mirror_model(DotGridPreview, name="DatasetLayoutResponse")


class DatasetLayoutRequest(BaseModel):
    """レイアウト設定（フィールド名は収集ジョブの ``ParamSpec`` と一致させる）.

    WebUI はジョブフォームの入力値をそのまま送れる。``shuffle_seed`` は解決せずに
    渡すので、``0`` の preview は決定論的な配置になる（実行時は別シードで再配置される）。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    plate_width: float
    plate_height: float
    edge_margin: float
    cell_size: float
    cell_gap: float
    crop_size: float
    purge_cell_size: float
    volume_min: float
    volume_max: float
    volume_divisions: int
    samples_per_volume: int
    blank_count: int
    view_count: int
    view_offset: float
    shuffle_seed: int


@router.post("/paste-dataset-layout")
def calculate_paste_dataset_layout(
    body: DatasetLayoutRequest,
) -> DatasetLayoutResponse:  # type: ignore[valid-type]
    """銅板のセル配置・使用セル・撮影枚数を返す（POST だが読み取り専用計算）."""
    preview = preview_dot_grid(
        grid_spec_from_params(body.model_dump()),
        view_count=body.view_count,
        view_offset_mm=body.view_offset,
    )
    return DatasetLayoutResponse.model_validate(preview, strict=False)
