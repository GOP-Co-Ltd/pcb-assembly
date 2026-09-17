"""Make clean は生成物だけを削除し、利用者のファイルと環境を維持する。"""

import subprocess
from pathlib import Path

import pytest

from pcbasm.utils import PROJECT_ROOT


class TestCleanTarget:
    @pytest.mark.parametrize("target_file_exists", [False, True])
    def test_cleanup_matches_artifacts_and_handles_paths_without_word_splitting(
        self, tmp_path: Path, target_file_exists: bool
    ):
        project = tmp_path / "project workspace"
        preserved = [
            "source.py",
            "notes.pyconfig",
            "my__pycache__notes.txt",
            "results.pytest_cache.txt",
            "guide.ipynb_checkpoints.md",
            "nested",
            ".git/objects/keep.pyc",
            ".venv/lib/package/__pycache__/keep.pyc",
        ]
        if target_file_exists:
            preserved.append("clean")
        generated = [
            "dist/package.whl",
            ".coverage",
            ".DS_Store",
            "nested cache/module.pyc",
            "nested cache/module.pyo",
            "nested cache/__pycache__/module.cpython-313.pyc",
            "nested cache/.pytest_cache/v/cache/nodeids",
            "nested cache/.ipynb_checkpoints/notebook.ipynb",
            "line\nbreak.pyc",
        ]
        for name in preserved + generated:
            path = project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        outside = tmp_path / "outside"
        outside.mkdir()
        external_cache = outside / "keep.pyc"
        external_cache.write_bytes(b"external")
        (project / "external link").symlink_to(outside, target_is_directory=True)

        # 2 回目も成功する。実リポジトリの clean は実行しない。
        for _ in range(2):
            result = subprocess.run(
                ["make", "-f", str(PROJECT_ROOT / "Makefile"), "clean"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            assert {name: (project / name).read_bytes() for name in preserved} == {
                name: b"fixture" for name in preserved
            }
            assert not any((project / name).exists() for name in generated)
            assert external_cache.read_bytes() == b"external"
