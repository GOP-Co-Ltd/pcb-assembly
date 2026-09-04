"""``src/ml`` をドメイン非依存かつ依存軽量に保つ構造上の契約.

``ml`` は装置ドメイン (``pcbasm`` / ``web``) を知らない ML 基盤であり、
成果物 I/O と設定変換の層は学習用の重い依存を要求しない。
"""

import ast
import subprocess
import sys

from tests.helpers import PROJECT_ROOT

ML_SOURCE_ROOT = PROJECT_ROOT / "src" / "ml"

DOMAIN_PACKAGES = ("pcbasm", "web")

# ``ml-runtime`` すら要求せず import できる層。MR ごとに追加する。
DEPENDENCY_FREE_MODULES = (
    "ml.artifact.atomic",
    "ml.artifact.document",
    "ml.artifact.fingerprint",
    "ml.artifact.package",
    "ml.serialization",
)

HEAVY_DEPENDENCIES = (
    "hydra",
    "mlflow",
    "omegaconf",
    "onnx",
    "onnxruntime",
    "onnxscript",
    "optuna",
    "torch",
    "torchvision",
)


def _module_name(path) -> str:
    relative = path.relative_to(ML_SOURCE_ROOT.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _absolute_imports(path) -> set[str]:
    """相対 import を解決したうえで、import 先の絶対 module 名を返す."""

    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                imported.add(node.module or "")
                continue
            base = package.split(".")
            ascended = base[: len(base) - (node.level - 1)] if node.level > 1 else base
            imported.add(".".join([*ascended, node.module or ""]).rstrip("."))
    return imported


class TestDomainIndependence:
    """``ml`` から装置ドメインへの依存を機械的に禁止する."""

    def test_no_module_imports_a_domain_package(self):
        sources = sorted(ML_SOURCE_ROOT.rglob("*.py"))
        # 走査対象が空でも下の assert は通ってしまうため、探索範囲を先に固定する
        assert sources != []

        offenders: list[str] = []
        for path in sources:
            for imported in _absolute_imports(path):
                root = imported.partition(".")[0]
                if root in DOMAIN_PACKAGES:
                    offenders.append(f"{_module_name(path)} -> {imported}")

        assert offenders == []


class TestDependencyFreeLayer:
    """成果物 I/O と設定変換は ML の重い依存なしに import できる."""

    def test_importing_them_does_not_load_heavy_dependencies(self):
        # 他テストが torch 等を既に読み込んでいるため、素の interpreter で確認する
        code = (
            "import sys;"
            f"[__import__(name) for name in {DEPENDENCY_FREE_MODULES!r}];"
            f"print(sorted(set({HEAVY_DEPENDENCIES!r}) & set(sys.modules)))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            cwd=PROJECT_ROOT,
        )

        assert result.stdout.strip() == "[]"
