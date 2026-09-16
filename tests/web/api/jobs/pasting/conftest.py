"""塗布ジョブを登録した共通catalog。stateやmanagerは親のfixtureを使う。"""

from __future__ import annotations

import pytest

from web.api.jobs.catalog import JobCatalog
from web.api.jobs.pasting import register_pasting_jobs


@pytest.fixture
def catalog() -> JobCatalog:
    """Pasting ジョブのみ登録した catalog（jobs/conftest の manager が使う）."""
    catalog = JobCatalog()
    register_pasting_jobs(catalog)
    return catalog
