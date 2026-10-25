"""`web.api.routers.pasting.calibration` の仕様テスト.

質量キャリブレーション算出（GET /api/pasting/calibration/mass-flow）:

router は「machine.toml の ``solder_paste_density`` を引いて
:meth:`pcbasm.pasting.MassFlowEstimate.estimate` へ委譲する」配線のみを持つ。
算術・丸め・null ゲーティングの網羅は
tests/pcbasm/pasting/dispense_calibration/test_mass.py::TestMassFlowEstimate が担保する。

PCB 選択は不要（machine 設定だけを参照する）なので素の ``client`` を使う。

保存済み塗布量校正の一覧（GET /api/pasting/calibration/paste-volume）:

router が持つのは「保存先を列挙し、表示文字列を組み立てて返す」配線だけ。校正の
schema と読み書きは tests/pcbasm/pasting/paste_volume/test_calibration.py が担保する。

装置を動かさない読み取り専用なので素の ``client`` を使う。壊れたファイルが 1 つ
あるせいで一覧そのものが消えると、どれが壊れているのか WebUI から分からなくなるので、
200 + 要素ごとの ``error`` を固める。

データセット収集レイアウト preview（POST /api/pasting/calibration/dataset-layout）:

router は「ジョブ ParamSpec と同名の値を :class:`DotGridSpec` へ写し、
:func:`pcbasm.pasting.paste_volume.dataset.plan.preview_dot_grid` の結果をそのまま返す」配線だけを持つ。

セル配置・容量判定・量割り当て・派生カウントの網羅は
tests/pcbasm/pasting/paste_volume/dataset/test_plan.py が担保するので、ここでは配線と HTTP 契約
（strict な型・未知キー拒否・配置不能でも 200）だけを固める。

PCB 選択も machine 操作も要らない（設定値だけを見る読み取り専用計算）ので素の
``client`` を使う。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pcbasm.pasting.paste_volume.calibration import CALIBRATION_SUFFIX
from tests.support.backend import standalone_catalog
from tests.support.paths import PROJECT_ROOT, TESTING_DATA_DIR
from web.api.env import Env
from web.api.routers.pasting.calibration import DatasetLayoutRequest


class TestLoadingCalibration:
    """GET /api/pasting/calibration/mass-flow."""

    def test_density_comes_from_machine_config(self, client: TestClient):
        # mass=10, rotations=5, density=3.78（data/testing/config/machine.toml 由来）
        # → rotations_per_ul = 1.89。density 配線が正しいことのピン。
        response = client.get(
            "/api/pasting/calibration/mass-flow",
            params={"mass_mg": 10, "rotations": 5, "rate": 0.5, "accel": 0.5},
        )

        assert response.status_code == 200, response.text
        assert response.json()["rotations_per_ul"] == pytest.approx(1.89)

    @pytest.mark.parametrize(
        ("params", "null_keys"),
        [
            # rotations=0 → 回転由来 3 値のみ null（volume_ul は出る）
            (
                {"mass_mg": 10, "rotations": 0, "rate": 0.5, "accel": 0.5},
                {"rotations_per_ul", "max_dispense_rate", "dispense_accel"},
            ),
            # mass=0 → 4 値すべて null
            (
                {"mass_mg": 0, "rotations": 5, "rate": 0.5, "accel": 0.5},
                {
                    "volume_ul",
                    "rotations_per_ul",
                    "max_dispense_rate",
                    "dispense_accel",
                },
            ),
            # クエリ省略時は各値 0.0 扱い → 4 値すべて null
            (
                {},
                {
                    "volume_ul",
                    "rotations_per_ul",
                    "max_dispense_rate",
                    "dispense_accel",
                },
            ),
            # float パーサーが受理する非有限値も、500 やゼロの算出値にしない。
            *[
                (
                    {"mass_mg": value, "rotations": 5, "rate": 0.5, "accel": 0.5},
                    {
                        "volume_ul",
                        "rotations_per_ul",
                        "max_dispense_rate",
                        "dispense_accel",
                    },
                )
                for value in ("nan", "1e309", "1e-308")
            ],
            (
                {"mass_mg": 10, "rotations": "1e309", "rate": 0.5, "accel": 0.5},
                {"rotations_per_ul", "max_dispense_rate", "dispense_accel"},
            ),
            (
                {"mass_mg": 10, "rotations": 5, "rate": "1e-8", "accel": "1e-8"},
                {"max_dispense_rate", "dispense_accel"},
            ),
        ],
    )
    def test_unusable_inputs_null_derived_values(
        self, client: TestClient, params: dict[str, float | str], null_keys: set[str]
    ):
        response = client.get("/api/pasting/calibration/mass-flow", params=params)

        assert response.status_code == 200, response.text
        body = response.json()
        for key in (
            "volume_ul",
            "rotations_per_ul",
            "max_dispense_rate",
            "dispense_accel",
        ):
            if key in null_keys:
                assert body[key] is None, key
            else:
                assert body[key] is not None, key


CALIBRATION_V1 = TESTING_DATA_DIR / "schemas" / "paste_volume_calibration_v1.json"


def _write(root: Path, stem: str, *, document: dict | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    payload = (
        json.loads(CALIBRATION_V1.read_text(encoding="utf-8"))
        if document is None
        else document
    )
    path = root / f"{stem}{CALIBRATION_SUFFIX}"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def calibration_dir(fake_camera_settings: Env) -> Path:
    return fake_camera_settings.paste_volume_calibration_dir


class TestListCalibrations:
    """保存済み校正の一覧."""

    def test_returns_an_empty_list_when_nothing_is_saved(self, client: TestClient):
        response = client.get("/api/pasting/calibration/paste-volume")

        assert response.status_code == 200
        assert response.json() == {"calibrations": []}

    def test_lists_a_saved_calibration_by_file_name(
        self, client: TestClient, calibration_dir: Path
    ):
        _write(calibration_dir, "s3x70-n030-h020")

        response = client.get("/api/pasting/calibration/paste-volume")

        assert response.status_code == 200
        listed = response.json()["calibrations"]
        assert len(listed) == 1
        assert listed[0]["name"] == f"s3x70-n030-h020{CALIBRATION_SUFFIX}"
        assert listed[0]["error"] is None

    def test_orders_calibrations_by_file_name(
        self, client: TestClient, calibration_dir: Path
    ):
        _write(calibration_dir, "b-second")
        _write(calibration_dir, "a-first")

        listed = client.get("/api/pasting/calibration/paste-volume").json()[
            "calibrations"
        ]

        assert [item["name"] for item in listed] == [
            f"a-first{CALIBRATION_SUFFIX}",
            f"b-second{CALIBRATION_SUFFIX}",
        ]

    def test_builds_the_display_strings_on_the_server(
        self, client: TestClient, calibration_dir: Path
    ):
        """``<select>`` へそのまま入れられる形で返す（JS で連結させない）."""
        _write(calibration_dir, "s3x70")

        listed = client.get("/api/pasting/calibration/paste-volume").json()[
            "calibrations"
        ]

        assert listed[0]["label"]
        assert "ノズル" in listed[0]["conditions"]
        assert "総体積誤差" in listed[0]["diagnostics"]
        assert listed[0]["option_label"] == listed[0]["label"]
        assert listed[0]["conditions"] in listed[0]["details"]
        assert listed[0]["diagnostics"] in listed[0]["details"]

    def test_reports_a_broken_file_without_losing_the_listing(
        self, client: TestClient, calibration_dir: Path
    ):
        """壊れた 1 件で一覧を落とさず、そのラベルも空にしない（名前で判別できるように）."""
        _write(calibration_dir, "healthy")
        (calibration_dir / f"broken{CALIBRATION_SUFFIX}").write_text(
            "{ not json", encoding="utf-8"
        )

        response = client.get("/api/pasting/calibration/paste-volume")

        assert response.status_code == 200
        listed = {item["name"]: item for item in response.json()["calibrations"]}
        assert listed[f"healthy{CALIBRATION_SUFFIX}"]["error"] is None
        broken = listed[f"broken{CALIBRATION_SUFFIX}"]
        assert broken["error"] is not None
        assert broken["label"] is None
        assert f"broken{CALIBRATION_SUFFIX}" in broken["option_label"]
        assert broken["details"]

    def test_reports_an_unsupported_schema_version(
        self, client: TestClient, calibration_dir: Path
    ):
        document = json.loads(CALIBRATION_V1.read_text(encoding="utf-8"))
        document["schema_version"] = 99
        _write(calibration_dir, "future", document=document)

        listed = client.get("/api/pasting/calibration/paste-volume").json()[
            "calibrations"
        ]

        assert listed[0]["error"] is not None
        assert "schema_version" in listed[0]["error"]

    def test_ignores_files_without_the_calibration_suffix(
        self, client: TestClient, calibration_dir: Path
    ):
        calibration_dir.mkdir(parents=True, exist_ok=True)
        (calibration_dir / "notes.json").write_text("{}", encoding="utf-8")

        assert (
            client.get("/api/pasting/calibration/paste-volume").json()["calibrations"]
            == []
        )


# レイアウトに関係しない収集ジョブのパラメータ（preview へ送らない）
_NON_LAYOUT_PARAMS = frozenset(
    {
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

_LAYOUT_URL = "/api/pasting/calibration/dataset-layout"

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
    """POST /api/pasting/calibration/dataset-layout."""

    def test_derived_counts_come_from_the_server(self, client: TestClient):
        body = _post(client)

        # JS が sample_count * (周辺 view + 1) * 2 を再導出しないための契約。
        assert body["sample_count"] == 15
        assert body["target_count"] == 19
        assert body["views_per_cell"] == 5
        assert body["image_count"] == 19 * 5 * 2

    def test_over_capacity_is_reported_in_the_body_not_as_an_error_status(
        self, client: TestClient
    ):
        # preview を消さずに理由を出す（テスト塗布基板 preview と同方針）。
        body = _post(client, samples_per_volume=100)

        assert body["error"] is not None
        assert body["cells"] == []
        assert body["grid"] != []

    @pytest.mark.parametrize(
        ("override", "keeps_usable_area"),
        [
            pytest.param({"edge_margin": -1.0}, False, id="invalid-spec"),
            pytest.param({"view_offset": 0.0}, True, id="invalid-view-settings"),
        ],
    )
    def test_invalid_settings_are_reported_in_the_body(
        self, client: TestClient, override: dict[str, Any], keeps_usable_area: bool
    ):
        body = _post(client, **override)

        assert body["error"] is not None
        # 板の使用可能域そのものが引けない設定でだけ usable_area も落ちる
        assert (body["usable_area"] is not None) is keeps_usable_area

    def test_unknown_key_is_rejected(self, client: TestClient):
        response = client.post(_LAYOUT_URL, json={**_BODY, "nope": 1.0})

        assert response.status_code == 422

    def test_missing_key_is_rejected(self, client: TestClient):
        body = dict(_BODY)
        del body["cell_size"]

        response = client.post(_LAYOUT_URL, json=body)

        assert response.status_code == 422

    def test_implicit_type_conversion_is_rejected(self, client: TestClient):
        """Pydantic strict モードの配線ピン（型ごとの網羅は pydantic の機能）."""
        response = client.post(_LAYOUT_URL, json={**_BODY, "plate_width": "20.0"})

        assert response.status_code == 422


class TestLayoutFieldNamesStayInSync:
    """レイアウト項目名がジョブ・API・JS の 3 箇所でずれないことのピン.

    ジョブへレイアウト項目を足して API / JS を忘れると、preview が黙ってその項目を
    無視する（サーバは既定値で計算し、画面は正しそうに見えてしまう）。
    """

    def test_request_model_covers_every_layout_job_param(self, tmp_path: Path):
        definition = standalone_catalog(tmp_path).get("paste_volume_calibration")
        layout_params = {
            spec.name
            for spec in definition.params
            if spec.name not in _NON_LAYOUT_PARAMS
        }

        assert set(DatasetLayoutRequest.model_fields) == layout_params

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
