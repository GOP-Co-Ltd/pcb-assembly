"""契約モデルが backend の定義を手で複製している既定値.

`web.contract` は `web.api` を import できない（`tests/test_architecture.py`）ので、`JobSpecInfo` は `JobDefinition` の既定値を書き写している。
片方だけ書き換わると、frontend が `JobSpecInfo` を省略形で構築したときに契約がずれる。
"""

import attrs
import pytest

from web.api.jobs.runtime.catalog import JobDefinition
from web.contract.models import JobSpecInfo

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
