"""`web.api.models` の依存境界の回帰テスト.

計画書 docs/plans/web-api-ui-split.md「MR2」節が契約:

- `web/api/models.py` は API 境界の contract を集約し、**pydantic と標準ライブラリのみ**を
  import する。frontend を別プロセス（`web.ui`）へ分離したあとも、そのまま import できる
  唯一の contract モジュールに保つため

依存境界の判定は import 文の静的検査（AST）で行う。実際に import して `sys.modules` を
覗く形では、他テストが先に読み込んだモジュールと区別できない。

この制約の代償として `JobSpecInfo` は `JobDefinition` のフィールド既定値を**手で複製**
している（`web.api.jobs.catalog` を import できないため）。片方だけ書き換わると
`/api/jobs` が値を埋める現在は無害でも、frontend が `JobSpecInfo` を直接構築する
MR4/MR5 で契約がずれるので、同期を `TestJobSpecInfoMirrorsJobDefinition` でピンする。
"""

import ast
import sys
from pathlib import Path

import attrs
import pytest

import web.api.models
from web.api.jobs.catalog import JobDefinition
from web.api.models import JobSpecInfo

_ALLOWED_THIRD_PARTY = frozenset({"pydantic"})

# JobSpecInfo と JobDefinition で共有する名前のうち、既定値の一致を要求しないもの
_NOT_MIRRORED = frozenset({"params"})  # ParamSpec / ParamSpecInfo で型が別


def _normalized(value: object) -> object:
    """列は tuple / list の別を無視して比較する（attrs は tuple、pydantic は list）."""
    return tuple(value) if isinstance(value, list | tuple) else value


def _optional_spec_info_names() -> set[str]:
    return {
        name
        for name, field in JobSpecInfo.model_fields.items()
        if not field.is_required()
    }


def _optional_definition_names() -> set[str]:
    return {
        field.name
        for field in attrs.fields(JobDefinition)
        if field.default is not attrs.NOTHING
    }


_MIRRORED_NAMES = sorted(
    (_optional_spec_info_names() & _optional_definition_names()) - _NOT_MIRRORED
)


def _imported_roots(source: str) -> set[str]:
    """Import 文からトップレベルのモジュール名を集める（相対 import は "web" 扱い）."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0 or node.module is None:
                roots.add("web")
            else:
                roots.add(node.module.split(".")[0])
    return roots


class TestContractModuleDependencies:
    """分割後も frontend からそのまま import できる依存関係を保つ."""

    def test_models_imports_only_pydantic_and_stdlib(self):
        source = Path(web.api.models.__file__).read_text(encoding="utf-8")

        roots = _imported_roots(source)

        # 検査対象が消えていないこと（空集合で無条件に通るのを防ぐ）
        assert "pydantic" in roots
        allowed = _ALLOWED_THIRD_PARTY | sys.stdlib_module_names
        assert roots <= allowed, f"contract 外の依存が入りました: {roots - allowed}"


class TestJobSpecInfoMirrorsJobDefinition:
    """手で複製している既定値が `JobDefinition` と一致し続ける.

    `models.py` は `web.api.jobs.catalog` を import できない（上の依存境界）ため、
    `JobSpecInfo` は `JobDefinition` の既定値を書き写している。省略時に見える値が
    ずれていないことを、両者の最小構築インスタンスで確認する。
    """

    def test_mirrored_field_names_are_not_empty(self):
        """複製対象が空になると下の parametrize が空回りする."""
        assert _MIRRORED_NAMES

    @pytest.mark.parametrize("name", _MIRRORED_NAMES)
    def test_omitted_field_takes_the_job_definition_default(self, name: str):
        spec = JobSpecInfo(name="probe", label="プローブ", tab="dev")
        definition = JobDefinition(
            name="probe", label="プローブ", tab="dev", run=lambda _: None
        )

        assert _normalized(getattr(spec, name)) == _normalized(
            getattr(definition, name)
        ), f"{name} の既定値が JobDefinition とずれています"
