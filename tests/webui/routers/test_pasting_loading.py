"""`webui.routers.pasting_loading`（質量キャリブレーション算出 API）の仕様テスト.

router は「現在マシンの ``solder_paste_density`` を引いて
:func:`pcbasm.pasting.estimate_mass_flow` へ委譲する」配線のみを持つ。
算術・丸め・null ゲーティングの網羅は
tests/pcbasm/pasting/test_calibration.py::TestEstimateMassFlow が担保する。

PCB 選択は不要（machine 設定だけを参照する）なので素の ``client`` を使う。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class TestLoadingCalibration:
    """GET /api/pasting/loading/calibration."""

    def test_density_comes_from_selected_machine(self, client: TestClient):
        # mass=10, rotations=5, density=3.78（test-fixture machine.toml 由来）
        # → rotations_per_ul = 1.89。density 配線が正しいことのピン。
        response = client.get(
            "/api/pasting/loading/calibration",
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
        ],
    )
    def test_non_positive_inputs_null_derived_values(
        self, client: TestClient, params: dict[str, float], null_keys: set[str]
    ):
        response = client.get("/api/pasting/loading/calibration", params=params)

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
