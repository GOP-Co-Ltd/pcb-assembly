"""`web.contract` の依存境界と、契約モデルが backend の定義を手で複製している既定値.

`web.contract` は pydantic と標準ライブラリだけを import する（frontend の `web.ui` がそのまま import できるように）。
その代償として `JobSpecInfo` は `JobDefinition` の既定値を書き写している。
片方だけ書き換わると、frontend が `JobSpecInfo` を省略形で構築したときに契約がずれる。
"""

import ast
import sys
from pathlib import Path

import attrs
import pytest

import web.contract
from web.api.jobs.catalog import JobDefinition
from web.contract.models import JobSpecInfo

_ALLOWED_ROOTS = frozenset({"pydantic"}) | sys.stdlib_module_names

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
    """Frontend からそのまま import できる依存関係を保つ."""

    def test_contract_imports_only_pydantic_and_stdlib(self):
        package = Path(web.contract.__file__).parent

        roots = set().union(
            *(
                _imported_roots(path.read_text(encoding="utf-8"))
                for path in package.glob("*.py")
            )
        )

        # 検査対象が消えていないこと（空集合で無条件に通るのを防ぐ）
        assert "pydantic" in roots
        assert (
            roots <= _ALLOWED_ROOTS
        ), f"契約外の依存が入りました: {roots - _ALLOWED_ROOTS}"


class TestJobSpecInfoMirrorsJobDefinition:
    """手で複製している既定値が `JobDefinition` と一致し続ける.

    省略時に見える値がずれていないことを、両者の最小構築インスタンスで確認する。
    """

    def test_mirrored_field_names_are_not_empty(self):
        """複製対象が空になると下の parametrize が空回りする."""
        assert _MIRRORED_NAMES

    @pytest.mark.parametrize("name", _MIRRORED_NAMES)
    def test_omitted_field_takes_the_job_definition_default(self, name: str):
        spec = JobSpecInfo(name="probe", label="プローブ")
        definition = JobDefinition(name="probe", label="プローブ", run=lambda _: None)

        assert _normalized(getattr(spec, name)) == _normalized(
            getattr(definition, name)
        ), f"{name} の既定値が JobDefinition とずれています"
