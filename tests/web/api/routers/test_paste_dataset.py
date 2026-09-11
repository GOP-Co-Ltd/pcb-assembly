"""`web.api.routers.paste_dataset`（データセット収集レイアウト preview API）の仕様テスト.

router は「ジョブ ParamSpec と同名の値を :class:`DotGridSpec` へ写し、
:func:`pcbasm.pasting.dataset.plan.preview_dot_grid` の結果をそのまま返す」配線だけを持つ。

セル配置・容量判定・量割り当て・派生カウントの網羅は
tests/pcbasm/pasting/dataset/test_plan.py が担保するので、ここでは配線と HTTP 契約
（strict な型・未知キー拒否・配置不能でも 200）だけを固める。

PCB 選択も machine 操作も要らない（設定値だけを見る読み取り専用計算）ので素の
``client`` を使う。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers import PROJECT_ROOT
from web.api.jobs.catalog import JobCatalog
from web.api.jobs.pasting import register_pasting_jobs
from web.api.routers.paste_dataset import DatasetLayoutRequest

# レイアウトに関係しない収集ジョブのパラメータ（preview へ送らない）
_NON_LAYOUT_PARAMS = frozenset(
    {
        "tolerance",
        "paste_height",
        "paste_id",
        "paste_lot",
        # 円検出のハイパラと校正の保存名・検証先。いずれも配置に影響しない
        "min_contrast",
        "contrast_percentile",
        "threshold_floor_ratio",
        "open_kernel_px",
        "min_area_px",
        "require_blank_zero",
        "save_name",
        "volume_calibration",
        # 塗布パス先頭のインタラクティブローディング設定。配置に影響しない
        "loading_amount",
        "loading_rotations",
        "loading_rate",
        "loading_accel",
        "loading_retract_rotations",
    }
)

_LAYOUT_URL = "/api/pasting/paste-dataset-layout"

# ジョブ ParamSpec と同名のレイアウト設定。板 20x20 / 余白 2 で 5x5 格子。
_BODY: dict[str, Any] = {
    "plate_width": 20.0,
    "plate_height": 20.0,
    "edge_margin": 2.0,
    "cell_size": 2.0,
    "cell_gap": 1.0,
    "crop_size": 2.0,
    "volume_min": 0.05,
    "volume_max": 0.2,
    "volume_divisions": 5,
    "samples_per_volume": 3,
    "blank_count": 4,
    "view_count": 4,
    "view_offset": 1.0,
    "shuffle_seed": 1234,
}


def _post(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post(_LAYOUT_URL, json={**_BODY, **overrides})

    assert response.status_code == 200, response.text
    return response.json()


class TestPasteDatasetLayout:
    """POST /api/pasting/paste-dataset-layout."""

    def test_returns_the_placed_cells_and_the_whole_grid(self, client: TestClient):
        body = _post(client)

        assert body["error"] is None
        assert len(body["cells"]) == 15
        assert len(body["blanks"]) == 4
        assert len(body["grid"]) == 25
        # 除外領域が無いので、格子の全セルが計測可能点として数えられる。
        assert body["capacity"] == 25

    def test_derived_counts_come_from_the_server(self, client: TestClient):
        body = _post(client)

        # JS が sample_count * (周辺 view + 1) * 2 を再導出しないための契約。
        assert body["sample_count"] == 15
        assert body["target_count"] == 19
        assert body["views_per_cell"] == 5
        assert body["image_count"] == 19 * 5 * 2

    def test_volumes_are_returned_for_the_legend(self, client: TestClient):
        body = _post(client)

        assert body["volumes_ul"] == pytest.approx([0.05, 0.0875, 0.125, 0.1625, 0.2])

    def test_each_cell_carries_its_commanded_volume(self, client: TestClient):
        body = _post(client)

        volumes = sorted(
            {round(cell["commanded_volume_ul"], 9) for cell in body["cells"]}
        )

        assert volumes == pytest.approx(body["volumes_ul"])

    def test_same_seed_reproduces_the_same_layout(self, client: TestClient):
        first = _post(client, shuffle_seed=1234)
        other = _post(client, shuffle_seed=1234)

        assert first["cells"] == other["cells"]

    def test_over_capacity_is_reported_in_the_body_not_as_an_error_status(
        self, client: TestClient
    ):
        # preview を消さずに理由を出す（テスト塗布基板 preview と同方針）。
        body = _post(client, samples_per_volume=100)

        assert body["error"] is not None
        assert body["cells"] == []
        assert body["grid"] != []

    def test_invalid_spec_is_reported_in_the_body(self, client: TestClient):
        body = _post(client, edge_margin=-1.0)

        assert body["error"] is not None
        assert body["usable_area"] is None

    def test_invalid_view_settings_are_reported_in_the_body(self, client: TestClient):
        body = _post(client, view_offset=0.0)

        assert body["error"] is not None

    def test_unknown_key_is_rejected(self, client: TestClient):
        response = client.post(_LAYOUT_URL, json={**_BODY, "nope": 1.0})

        assert response.status_code == 422

    def test_missing_key_is_rejected(self, client: TestClient):
        body = dict(_BODY)
        del body["cell_size"]

        response = client.post(_LAYOUT_URL, json=body)

        assert response.status_code == 422

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("plate_width", "20.0"),
            ("volume_divisions", 5.0),
            ("view_count", "4"),
            ("blank_count", True),
        ],
    )
    def test_implicit_type_conversion_is_rejected(
        self, client: TestClient, key: str, value: Any
    ):
        response = client.post(_LAYOUT_URL, json={**_BODY, key: value})

        assert response.status_code == 422


class TestLayoutFieldNamesStayInSync:
    """レイアウト項目名がジョブ・API・JS の 3 箇所でずれないことのピン.

    ジョブへレイアウト項目を足して API / JS を忘れると、preview が黙ってその項目を
    無視する（サーバは既定値で計算し、画面は正しそうに見えてしまう）。
    """

    def _job_layout_params(self) -> set[str]:
        catalog = JobCatalog()
        register_pasting_jobs(catalog)
        definition = catalog.get("paste_volume_calibration")
        assert definition is not None
        return {
            spec.name
            for spec in definition.params
            if spec.name not in _NON_LAYOUT_PARAMS
        }

    def test_request_model_covers_every_layout_job_param(self):
        assert set(DatasetLayoutRequest.model_fields) == self._job_layout_params()

    def test_frontend_sends_every_layout_field(self):
        source = (
            PROJECT_ROOT
            / "src"
            / "web"
            / "ui"
            / "static"
            / "js"
            / "paste_dataset_layout.js"
        ).read_text(encoding="utf-8")

        for name in DatasetLayoutRequest.model_fields:
            assert f'"{name}"' in source, name
