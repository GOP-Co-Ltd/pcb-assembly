"""Reusable ML package boundary contracts."""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
GENERIC_PACKAGES = (
    "artifacts",
    "cli",
    "data",
    "evaluation",
    "export",
    "infer",
    "model",
    "training",
    "tuning",
)
OPTIONAL_ROOTS = (
    "PIL",
    "hydra",
    "mlflow",
    "onnx",
    "onnxruntime",
    "onnxscript",
    "optuna",
    "torch",
    "torchvision",
)


class TestMlImportBoundary:
    def test_package_imports_are_lightweight(self):
        modules = ["ml", *(f"ml.{name}" for name in GENERIC_PACKAGES)]
        modules.extend(
            ("ml.paste_volume", "ml.paste_volume.cli", "ml.paste_volume.infer")
        )
        code = f"""
import importlib
import json
import sys

for name in {modules!r}:
    importlib.import_module(name)
roots = {OPTIONAL_ROOTS!r}
print(json.dumps(sorted(
    root for root in roots
    if any(name == root or name.startswith(root + ".") for name in sys.modules)
)))
"""

        completed = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )

        assert json.loads(completed.stdout) == []

    def test_removed_namespaces_have_no_importable_modules(self):
        removed = (
            "pcbasm.pasting.paste_volume",
            "pcbasm.cli.paste_volume",
            "ml.paste_volume.dependencies",
            "ml.paste_volume.experiment",
            "ml.paste_volume.export",
            "ml.paste_volume.hpo",
            "ml.paste_volume.inference",
        )

        assert {
            module for module in removed if importlib.util.find_spec(module) is not None
        } == set()


class TestGenericSourceBoundary:
    def test_generic_modules_do_not_import_domain_packages(self):
        source_root = PROJECT_ROOT / "src/ml"
        violations: list[str] = []
        for package in GENERIC_PACKAGES:
            package_root = source_root / package
            for path in sorted(package_root.rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    for imported in _imported_modules(node):
                        if imported == "pcbasm" or imported.startswith("pcbasm."):
                            violations.append(
                                f"{path.relative_to(PROJECT_ROOT)}: {imported}"
                            )
                        if imported == "ml.paste_volume" or imported.startswith(
                            "ml.paste_volume."
                        ):
                            violations.append(
                                f"{path.relative_to(PROJECT_ROOT)}: {imported}"
                            )

        assert violations == []


def _imported_modules(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom) and node.level == 0:
        if node.module is None:
            return ()
        return (
            node.module,
            *(f"{node.module}.{alias.name}" for alias in node.names),
        )
    return ()
