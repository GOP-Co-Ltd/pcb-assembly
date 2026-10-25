"""文書が、それの説明するコードの表と食い違わない.

設定キーの一覧（docs/reference/config.md）は、生成器の出力と表の桁ぞろえのほかは一致する。
「機能 → コード」の地図（docs/develop/architecture.md）は、機能表のすべての機能を表の順に載せる。
文書の相対リンクは、リポジトリにあるファイルを指す。
"""

import re
from pathlib import Path

import pytest

from pcbasm.paths import PROJECT_ROOT
from tests.support.config_reference import render
from web.ui.features import FEATURES

DOCS = sorted(
    [
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "CLAUDE.md",
        PROJECT_ROOT / "CONTRIBUTING.md",
        PROJECT_ROOT / "github-runner" / "README.md",
        *(PROJECT_ROOT / "data").glob("*/README.md"),
        *(PROJECT_ROOT / "docs").rglob("*.md"),
        *(PROJECT_ROOT / "src").rglob("README.md"),
        *(PROJECT_ROOT / "deploy").rglob("README.md"),
    ]
)

_LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")


def _unpadded(text: str) -> list[str]:
    """行の並びから、``make format`` が表に足す桁ぞろえの空白と ``-`` を除く."""
    lines = []
    for line in text.splitlines():
        if line.startswith("| -"):
            line = re.sub("-+", "---", line)
        if line.startswith("|"):
            line = re.sub(" +", " ", line)
        lines.append(line)
    return lines


class TestConfigReference:
    def test_is_the_generated_text(self):
        # ずれたら tests/support/config_reference.py の手順で作り直す
        text = (PROJECT_ROOT / "docs" / "reference" / "config.md").read_text()

        assert _unpadded(text) == _unpadded(render())


class TestFeatureMap:
    def test_lists_every_feature_in_the_table_order(self):
        text = (PROJECT_ROOT / "docs" / "develop" / "architecture.md").read_text()
        section = text.split("## 機能 → コードの地図", 1)[1].split("\n## ", 1)[0]
        slugs = re.findall(r"^\| `(\w+)` +\|", section, re.MULTILINE)

        assert slugs == [feature.slug for feature in FEATURES]


class TestLinks:
    @pytest.mark.parametrize(
        "doc", DOCS, ids=lambda path: str(path.relative_to(PROJECT_ROOT))
    )
    def test_relative_links_point_to_files_in_the_repository(self, doc: Path):
        targets = [
            target
            for target in _LINK.findall(doc.read_text())
            if "://" not in target and not target.startswith("mailto:")
        ]
        missing = [target for target in targets if not (doc.parent / target).exists()]

        assert missing == []
