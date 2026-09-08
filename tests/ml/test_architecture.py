"""``src/ml`` をドメイン非依存かつ依存軽量に保つ構造上の契約.

``ml`` は装置ドメイン (``pcbasm`` / ``web``) を知らない ML 基盤であり、
成果物 I/O と設定変換の層は学習用の重い依存を要求しない。
"""

import ast
import subprocess
import sys

from tests.ml.helpers import PROJECT_ROOT

ML_SOURCE_ROOT = PROJECT_ROOT / "src" / "ml"
ML_TEST_ROOT = PROJECT_ROOT / "tests" / "ml"

DOMAIN_PACKAGES = ("pcbasm", "web")

# ``tests/ml`` から参照してはならない module。``tests.helpers`` は pcbnew /
# picamera2 を、装置ドメインの package は picamera2 を module 冒頭で import する。
FORBIDDEN_TEST_IMPORTS = ("tests.helpers", *DOMAIN_PACKAGES)

# ``ml-runtime`` すら要求せず import できる層。MR ごとに追加する。
DEPENDENCY_FREE_MODULES = (
    "ml.artifact.atomic",
    "ml.artifact.document",
    "ml.artifact.fingerprint",
    "ml.artifact.package",
    "ml.experiment.logger",
    "ml.experiment.provenance",
    "ml.export.manifest",
    "ml.export.promotion",
    "ml.serialization",
)

# ``ml-runtime`` だけを install した Raspberry Pi 5 で import できる層。MR ごとに追加する。
RUNTIME_MODULES = (
    "ml.data.batch",
    "ml.data.image",
    "ml.data.split",
    "ml.evaluation.compile_parity",
    "ml.evaluation.regression",
    "ml.evaluation.slices",
    "ml.export.parity",
    "ml.model.blocks",
    "ml.model.heads",
    "ml.model.inspection",
    "ml.model.loss",
    "ml.training.checkpoint",
    "ml.training.data",
    "ml.training.loop",
    "ml.training.random_state",
    "ml.training.task",
    "ml.training.transaction",
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

# 学習と探索でしか要らない依存。``ml-runtime`` 層はこれらを読んではならない。
TRAINING_ONLY_DEPENDENCIES = (
    "hydra",
    "mlflow",
    "omegaconf",
    "onnx",
    "onnxscript",
    "optuna",
)


def _loaded_dependencies(modules: tuple[str, ...], forbidden: tuple[str, ...]) -> str:
    # 他テストが torch 等を既に読み込んでいるため、素の interpreter で確認する
    code = (
        "import sys;"
        f"[__import__(name) for name in {modules!r}];"
        f"print(sorted(set({forbidden!r}) & set(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=PROJECT_ROOT,
    )
    return result.stdout.strip()


def _reaches_device_domain(imported: str) -> bool:
    """``imported`` が装置ドメイン側の module を指すか."""

    return any(
        imported == forbidden or imported.startswith(f"{forbidden}.")
        for forbidden in FORBIDDEN_TEST_IMPORTS
    )


def _module_name(path, anchor) -> str:
    """``anchor`` を package root の親として ``path`` の絶対 module 名を組む."""

    relative = path.relative_to(anchor).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _absolute_imports(path, anchor) -> set[str]:
    """相対 import を解決したうえで、import 先の絶対 module 名を返す.

    相対 import を解決するので ``from ..helpers import X`` のような迂回も拾う。
    """

    module = _module_name(path, anchor)
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
            for imported in _absolute_imports(path, ML_SOURCE_ROOT.parent):
                root = imported.partition(".")[0]
                if root in DOMAIN_PACKAGES:
                    name = _module_name(path, ML_SOURCE_ROOT.parent)
                    offenders.append(f"{name} -> {imported}")

        assert offenders == []

    def test_no_test_module_reaches_the_device_domain(self):
        """``tests/ml`` は装置ドメインとそのテストヘルパーを参照しない.

        ``tests.helpers`` は pcbnew / picamera2 を、``pcbasm`` は picamera2 を要求する。
        参照すると Raspberry Pi と KiCAD の無い学習機で ``tests/ml`` が collect
        できなくなる。相対 import での迂回も同じ扱いにする。
        """
        sources = sorted(ML_TEST_ROOT.rglob("*.py"))
        # 走査対象が空でも下の assert は通ってしまうため、探索範囲を先に固定する
        assert sources != []

        offenders: list[str] = []
        for path in sources:
            for imported in _absolute_imports(path, PROJECT_ROOT):
                if _reaches_device_domain(imported):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)} -> {imported}")

        assert offenders == []


class TestDependencyFreeLayer:
    """成果物 I/O と設定変換は ML の重い依存なしに import できる."""

    def test_importing_them_does_not_load_heavy_dependencies(self):
        loaded = _loaded_dependencies(DEPENDENCY_FREE_MODULES, HEAVY_DEPENDENCIES)

        assert loaded == "[]"


class TestRuntimeLayer:
    """推論経路は ``ml-runtime`` だけで import できる.

    Raspberry Pi 5 へ MLflow / Hydra / Optuna / ONNX を入れずに済ませるための契約。
    torch と torchvision は隠さない（隠すと関数内 import が散り、型が失われる）。
    """

    def test_importing_them_does_not_load_training_only_dependencies(self):
        loaded = _loaded_dependencies(RUNTIME_MODULES, TRAINING_ONLY_DEPENDENCIES)

        assert loaded == "[]"


# Raspberry Pi 5 の実運転推論だけで使う層。onnxruntime と numpy しか読んではならない。
#
# process 起動から初回予測までの cold latency に ``import torch`` が数秒を直接足すため、
# 推論経路に torch を持ち込まないことを機械検証する。
INFERENCE_ONLY_MODULES = (
    "ml.export.benchmark",
    "ml.export.runtime",
)

INFERENCE_FORBIDDEN_DEPENDENCIES = (
    "onnx",
    "onnxscript",
    "torch",
    "torchvision",
)


class TestInferenceOnlyLayer:
    """推論経路は onnxruntime と numpy だけで import できる.

    ``onnxruntime`` は単体では ``onnx`` を読み込まない。量子化 API
    (``onnxruntime.quantization``) を触った瞬間に読み込むので、両者を同じ module へ
    置かないことをここで固定する。
    """

    def test_importing_them_does_not_load_onnx_or_torch(self):
        loaded = _loaded_dependencies(
            INFERENCE_ONLY_MODULES, INFERENCE_FORBIDDEN_DEPENDENCIES
        )

        assert loaded == "[]"
