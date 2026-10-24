"""WebUI が書く・表示する設定キーは、どれも MachineConfig の属性パスとして存在する.

TOML のパスと属性のパスは一致するので、キーの書き間違いや、節を移したときの書き換え漏れをここで見つける。
"""

import re
import types
from typing import Any, Union, get_args, get_origin

import attrs

from pcbasm.config import MachineConfig
from pcbasm.paths import PROJECT_ROOT

_SECTIONS = "|".join(attrs.fields_dict(MachineConfig))
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


def _attrs_class(annotation: Any) -> type | None:
    """フィールドの型から attrs クラスを取り出す（``X | None`` も見る）."""
    if attrs.has(annotation):
        return annotation
    if get_origin(annotation) in (Union, types.UnionType):
        return next((arg for arg in get_args(annotation) if attrs.has(arg)), None)
    return None


def _resolves(path: str) -> bool:
    cls: type | None = MachineConfig
    for name in path.split("."):
        if cls is None:
            return False
        fields = attrs.fields_dict(attrs.resolve_types(cls))
        if name not in fields:
            return False
        cls = _attrs_class(fields[name].type)
    return True


class TestSettingKeys:
    def test_scanner_finds_the_written_keys(self):
        """読み取りが壊れて検査が空回りしていないこと."""
        keys = _written_keys()

        assert "paste_dispenser.motion.max_dispense_rate" in keys
        assert "alignment.canny_low" in keys
        assert "camera.calibration_file" in keys

    def test_every_key_is_an_attribute_path_of_machine_config(self):
        assert {key for key in _written_keys() if not _resolves(key)} == set()
