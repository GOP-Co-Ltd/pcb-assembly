"""Golden の比べ方（assert_transcript と assert_document）の仕様."""

import json
from pathlib import Path

import pytest

from pcbasm.gcode import GCode
from pcbasm.hal.fake import SentGCode
from tests.support.golden import UPDATE_ENV, assert_document, assert_transcript


def _golden(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "golden.gcode"
    path.write_text(text, encoding="utf-8")
    return path


def _sent(gcode: str, timeout: float | None = 60.0) -> list[SentGCode]:
    return [SentGCode(GCode(gcode), timeout)]


class TestAssertTranscript:
    @pytest.fixture(autouse=True)
    def compare(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(UPDATE_ENV, raising=False)

    def test_numbers_within_tolerance_match(self, tmp_path: Path):
        golden = _golden(tmp_path, "# send timeout=60.0\nG1 X0.3 F6000.0\n")

        assert_transcript(_sent("G1 X0.30000000000000004 F6000.0"), golden)

    @pytest.mark.parametrize(
        "gcode",
        [
            "G1 X0.300001 F6000.0",  # 数値が 1e-9 を超えてずれる
            "G1 Y0.3 F6000.0",  # 語が違う
            "G1 F6000.0 X0.3",  # 語の並びが違う
            "G0 X0.3 F6000.0",  # 命令が違う
            "G1 X0.3 F6000.0\nM400",  # 行が多い
        ],
    )
    def test_any_other_difference_fails(self, tmp_path: Path, gcode: str):
        golden = _golden(tmp_path, "# send timeout=60.0\nG1 X0.3 F6000.0\n")

        with pytest.raises(pytest.fail.Exception):
            assert_transcript(_sent(gcode), golden)

    def test_fewer_lines_fail(self, tmp_path: Path):
        golden = _golden(tmp_path, "# send timeout=60.0\nG1 X0.3 F6000.0\nM400\n")

        with pytest.raises(pytest.fail.Exception):
            assert_transcript(_sent("G1 X0.3 F6000.0"), golden)

    def test_missing_golden_fails(self, tmp_path: Path):
        with pytest.raises(pytest.fail.Exception):
            assert_transcript(_sent("M400"), tmp_path / "missing.gcode")

    def test_timeout_heading_must_match_exactly(self, tmp_path: Path):
        golden = _golden(tmp_path, "# send timeout=None\nM400\n")

        with pytest.raises(pytest.fail.Exception):
            assert_transcript(_sent("M400", 60.0), golden)

    def test_update_env_rewrites_golden(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        golden = _golden(tmp_path, "# send timeout=None\nM84\n")
        monkeypatch.setenv(UPDATE_ENV, "1")

        assert_transcript(_sent("M400"), golden)

        assert golden.read_text(encoding="utf-8") == "# send timeout=60.0\nM400\n"


class TestAssertDocument:
    DOCUMENT = {"model": {"coefficients": [0.3, 2]}, "accepted": True, "name": "a"}

    @pytest.fixture(autouse=True)
    def compare(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(UPDATE_ENV, raising=False)

    @pytest.fixture
    def golden(self, tmp_path: Path) -> Path:
        path = tmp_path / "golden.json"
        path.write_text(json.dumps(self.DOCUMENT), encoding="utf-8")
        return path

    def test_numbers_within_tolerance_match(self, golden: Path):
        document = {
            **self.DOCUMENT,
            "model": {"coefficients": [0.30000000000000004, 2.0]},
        }

        assert_document(document, golden)

    @pytest.mark.parametrize(
        "change",
        [
            {"model": {"coefficients": [0.300001, 2]}},  # 数値が 1e-9 を超えてずれる
            {"model": {"coefficients": [0.3]}},  # 要素が足りない
            {"model": {"coefficients": [0.3, 2], "extra": 1}},  # キーが多い
            {"accepted": 1},  # bool と数値は区別する
            {"name": "b"},  # 文字列が違う
        ],
    )
    def test_any_other_difference_fails(self, golden: Path, change: dict[str, object]):
        with pytest.raises(pytest.fail.Exception):
            assert_document({**self.DOCUMENT, **change}, golden)

    def test_update_env_rewrites_golden(
        self, golden: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(UPDATE_ENV, "1")

        assert_document({"name": "b"}, golden)

        assert json.loads(golden.read_text(encoding="utf-8")) == {"name": "b"}
