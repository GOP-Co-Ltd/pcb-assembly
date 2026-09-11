"""`web.api.routers.paste_volume`（保存済み塗布量校正の一覧 API）の仕様テスト.

router が持つのは「保存先を列挙し、表示文字列を組み立てて返す」配線だけ。校正の
schema と読み書きは tests/pcbasm/pasting/paste_volume/test_calibration.py が担保する。

装置を動かさない読み取り専用なので素の ``client`` を使う。壊れたファイルが 1 つ
あるせいで一覧そのものが消えると、どれが壊れているのか WebUI から分からなくなるので、
200 + 要素ごとの ``error`` を固める。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pcbasm.pasting.paste_volume.calibration import CALIBRATION_SUFFIX
from tests.helpers import TESTING_DATA_DIR
from web.api.settings import Settings

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
def calibration_dir(fake_camera_settings: Settings) -> Path:
    return fake_camera_settings.paste_volume_calibration_dir


class TestListCalibrations:
    """保存済み校正の一覧."""

    def test_returns_an_empty_list_when_nothing_is_saved(self, client: TestClient):
        response = client.get("/api/pasting/paste-volume/calibrations")

        assert response.status_code == 200
        assert response.json() == {"calibrations": []}

    def test_lists_a_saved_calibration_by_file_name(
        self, client: TestClient, calibration_dir: Path
    ):
        _write(calibration_dir, "s3x70-n030-h020")

        response = client.get("/api/pasting/paste-volume/calibrations")

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

        listed = client.get("/api/pasting/paste-volume/calibrations").json()[
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

        listed = client.get("/api/pasting/paste-volume/calibrations").json()[
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

        response = client.get("/api/pasting/paste-volume/calibrations")

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

        listed = client.get("/api/pasting/paste-volume/calibrations").json()[
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
            client.get("/api/pasting/paste-volume/calibrations").json()["calibrations"]
            == []
        )
