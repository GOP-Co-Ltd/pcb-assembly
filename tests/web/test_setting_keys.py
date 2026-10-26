"""WebUI が書く・表示する設定キーは、どれも設定ページの項目（setting_fields）にある.

書き込みは設定ページと同じ 1 経路を通り、項目に無いキーは拒否される。キーの書き間違いや、節を移したときの書き換え漏れをここで見つける。
"""

import re

import attrs

from pcbasm.config import MachineConfig
from pcbasm.config.fields import setting_fields
from pcbasm.paths import PROJECT_ROOT

# 設定ページの節（section の metadata の付いた節。項目の無い節の名前はファイル名などと紛れる）
_SECTIONS = "|".join(top.name for top in attrs.fields(MachineConfig) if top.metadata)
# 引用符で囲んだ「<節>.<キー>…」（末尾が "." の接頭辞も含む）
_KEY = re.compile(rf"[\"'`]((?:{_SECTIONS})\.[a-z_.]+)[\"'`]")


def _written_keys() -> set[str]:
    sources = (
        path
        for pattern in ("*.py", "*.js", "*.html")
        for path in (PROJECT_ROOT / "src" / "web").rglob(pattern)
    )
    return {
        match.removesuffix(".")
        for path in sources
        for match in _KEY.findall(path.read_text(encoding="utf-8"))
    }


def _is_setting_key(key: str) -> bool:
    """設定キーそのものか、設定キーの接頭辞（``camera.crop`` など）."""
    return any(
        field.key == key or field.key.startswith(f"{key}.")
        for field in setting_fields()
    )


class TestSettingKeys:
    def test_scanner_finds_the_written_keys(self):
        """読み取りが壊れて検査が空回りしていないこと."""
        keys = _written_keys()

        assert "solder.dispenser.max_dispense_rate" in keys
        assert "alignment.canny_low" in keys
        assert "camera.calibration_file" in keys

    def test_every_key_is_a_setting_field(self):
        assert {key for key in _written_keys() if not _is_setting_key(key)} == set()
