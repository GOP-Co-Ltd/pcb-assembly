"""`pcbasm.pnp.load_cell_calibration` の仕様テスト.

- counts_per_gram は、タール値との差 [counts] の絶対値を載せた重さ [g] で割ったもの
- 重さが正でないか、差が小さすぎれば（1000 counts 未満）校正しない
- 要約は、今の値との比と、差が負（押す向きと逆の荷重）のときの意味を出す
"""

import pytest

from pcbasm.config.fields import setting_fields
from pcbasm.pnp.load_cell_calibration import (
    COUNTS_PER_GRAM_KEY,
    counts_per_gram,
    describe,
)


class TestCountsPerGram:
    def test_divides_the_counts_by_the_grams(self):
        assert counts_per_gram(2667 * 100, 100) == 2667

    def test_negative_counts_use_the_magnitude(self):
        assert counts_per_gram(-2000 * 50, 50) == 2000

    @pytest.mark.parametrize("grams", [0.0, -10.0])
    def test_grams_must_be_positive(self, grams: float):
        with pytest.raises(ValueError, match="重さ"):
            counts_per_gram(100_000, grams)

    @pytest.mark.parametrize("counts", [999.0, -999.0, 0.0])
    def test_too_small_a_change_is_refused(self, counts: float):
        with pytest.raises(ValueError, match="小さすぎ"):
            counts_per_gram(counts, 1.0)

    def test_a_change_of_a_thousand_counts_is_enough(self):
        assert counts_per_gram(1000, 0.5) == 2000


class TestDescribe:
    def test_shows_the_value_and_the_ratio_to_the_current_one(self):
        text = describe(200_000, 100, 2500.0)

        assert "2000.00" in text
        assert "0.800" in text  # 今の値の 0.8 倍
        assert "逆" not in text

    def test_negative_counts_explain_the_direction(self):
        text = describe(-200_000, 100, None)

        assert "2000.00" in text
        assert "押す向きと逆" in text


class TestSettingKey:
    def test_is_a_setting_page_key(self):
        """Apply の書き込みは設定ページの項目しか受け付けない."""
        assert COUNTS_PER_GRAM_KEY in {field.key for field in setting_fields()}
