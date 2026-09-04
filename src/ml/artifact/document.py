"""``kind`` と ``schema_version`` を持つ成果物 document.

成果物 JSON は必ずこのエンベロープを被る。読む側は、未知の種類や未対応の schema version
を推測で読まずに理由を返して止まる。エンベロープの 2 キーは 本体の attrs クラスのフィールドにはせず、この層だけが付け外しする。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import attrs
from cattrs import Converter

from ml.artifact.atomic import atomic_write_json
from ml.serialization import structure_strictly

_KIND_KEY = "kind"
_SCHEMA_VERSION_KEY = "schema_version"
_ENVELOPE_KEYS = (_KIND_KEY, _SCHEMA_VERSION_KEY)


@attrs.frozen
class DocumentKind:
    """成果物の種類と、その on-disk 形状の版."""

    kind: str
    schema_version: int

    def unstructure(self, value: object, *, converter: Converter) -> dict[str, object]:
        """エンベロープを被せた素の dict を返す."""

        payload = converter.unstructure(value)
        if not isinstance(payload, dict):
            raise ValueError(f"document 本体が JSON object になりません: {type(value)}")
        if conflicting := sorted(set(_ENVELOPE_KEYS) & set(payload)):
            raise ValueError(
                f"document 本体が envelope key を持っています: {conflicting}"
            )
        return {
            _KIND_KEY: self.kind,
            _SCHEMA_VERSION_KEY: self.schema_version,
            **payload,
        }

    def structure[T](
        self,
        data: Mapping[str, object],
        target: type[T],
        *,
        converter: Converter,
    ) -> tuple[T | None, str | None]:
        """エンベロープを検証してから本体を構造化する."""

        actual_kind = data.get(_KIND_KEY)
        if actual_kind != self.kind:
            return None, (
                f"未対応の document kind です: {actual_kind!r}（期待値 {self.kind!r}）"
            )
        actual_version = data.get(_SCHEMA_VERSION_KEY)
        if actual_version != self.schema_version:
            return None, (
                f"未対応の schema_version です: {actual_version!r}"
                f"（期待値 {self.schema_version}）"
            )
        body = {key: value for key, value in data.items() if key not in _ENVELOPE_KEYS}
        return structure_strictly(body, target, converter=converter)

    def save(self, path: Path, value: object, *, converter: Converter) -> None:
        """エンベロープ付き JSON を atomic に書き出す."""

        atomic_write_json(path, self.unstructure(value, converter=converter))

    def load[T](
        self,
        path: Path,
        target: type[T],
        *,
        converter: Converter,
    ) -> tuple[T | None, str | None]:
        """エンベロープ付き JSON を読み、失敗したら理由を返す."""

        source = Path(path)
        if not source.is_file():
            return None, f"document が見つかりません: {source}"
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            return None, f"document を読めません: {source}（{error}）"
        if not isinstance(data, dict):
            return None, f"document が JSON object ではありません: {source}"
        return self.structure(data, target, converter=converter)


__all__ = [
    "DocumentKind",
]
