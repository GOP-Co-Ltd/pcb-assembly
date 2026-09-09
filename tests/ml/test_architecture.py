"""``src/ml`` の構造上の契約.

``ml`` は ML の責務を持つ package で、装置の制御コアである ``pcbasm`` とは責務を分ける。
ただし塗布量推定のドメイン層 ``ml.paste_volume`` だけは収集 dataset を読むため、装置ドメイン
側の**データ構造**を参照する。ここで固定するのはその線引きと、学習コンテナで import できる
ことの 2 点。

契約は 3 本ある。

1. ``ml`` コア（``ml.paste_volume`` 以外）は ``pcbasm`` / ``web`` を一切参照しない。
   再利用価値があるのはこの層なので、強度を落とさない
2. ``ml.paste_volume`` は装置ドメインのうち :data:`DOMAIN_DATA_PACKAGES` だけを参照する。
   収集 schema と純粋な値オブジェクトは可、制御ロジックは不可
3. ``src/ml`` と ``tests/ml`` のどこからも :data:`DEVICE_ONLY_MODULES` へ推移的に届かない

``ml.paste_volume`` と ``pcbasm.pasting`` は 1 つの関心事の両端なので、package 単位では
循環する（学習側が収集 schema を読み、装置側が export 済み成果物を読む）。それは意図した形で、
ここでは検査しない。検査するのは責務の線引きと、学習機で collect できることだけ。

3 番目を推移的に見るのは、``dataset.metadata`` から ``applicator`` を経て ``pcbasm.hal`` へ
届く 2 段の連鎖を、直接 import しか見ない検査が取り逃したため。CI は picamera2 のある
Raspberry Pi で走るので、実行時に落ちることにも頼れない。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ml.helpers import PROJECT_ROOT

SOURCE_ANCHOR = PROJECT_ROOT / "src"

ML_SOURCE_ROOT = SOURCE_ANCHOR / "ml"
ML_TEST_ROOT = PROJECT_ROOT / "tests" / "ml"

# 装置ドメイン側のドメイン層。``ml`` コアと違う規則を当てる唯一の subtree。
DOMAIN_LAYER = "paste_volume"

DOMAIN_PACKAGES = ("pcbasm", "web")

# ``ml.paste_volume`` が参照してよい装置ドメイン側の package。
#
# 収集 schema と純粋な値オブジェクトだけを挙げる。``pcbasm.pasting.applicator`` のような
# 制御ロジックが ML 層へ漏れると、責務の分割そのものが崩れる。module 単位でなく package
# 単位にしているのは、schema に DTO が 1 つ増えるたびに列挙を触らずに済ませるため。
DOMAIN_DATA_PACKAGES = (
    "pcbasm.geometry",
    "pcbasm.pasting.dataset",
    "pcbasm.pasting.dispense",
)

# 学習機・学習コンテナに存在しない依存へ到達する module。
DEVICE_ONLY_MODULES = ("pcbasm.hal", "pcbnew", "picamera2")

# ``tests/ml`` から参照してはならないテストヘルパー。``tests.helpers`` は pcbnew /
# picamera2 を module 冒頭で import する。
FORBIDDEN_TEST_HELPER = "tests.helpers"

# 収集 schema。``ml.paste_volume`` が必ず読むので、推移的到達の起点へ加える。
# ここが装置 HAL を引き戻した瞬間に学習機で collect できなくなる。
DATASET_SCHEMA_FILE = SOURCE_ANCHOR / "pcbasm" / "pasting" / "dataset" / "metadata.py"

# ``ml-runtime`` すら要求せず import できる層。MR ごとに追加する。
DEPENDENCY_FREE_MODULES = (
    "ml.artifact.atomic",
    "ml.artifact.document",
    "ml.artifact.fingerprint",
    "ml.artifact.package",
    "ml.config.composition",
    "ml.config.packaged",
    "ml.experiment.logger",
    "ml.experiment.provenance",
    "ml.export.manifest",
    "ml.export.promotion",
    "ml.serialization",
    "ml.tuning.study",
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
    "ml.model.multiview",
    "ml.training.checkpoint",
    "ml.training.data",
    "ml.training.loop",
    "ml.training.random_state",
    "ml.training.task",
    "ml.training.transaction",
)

# ``hydra`` / ``omegaconf`` は挙げない。案 C（Hydra なし）で install されないため、
# 挙げても「読み込まれていないこと」が常に成り立ち、assertion が空虚になる。
# 機構を守らないテストになるので、実際に install される依存だけを列挙する。
HEAVY_DEPENDENCIES = (
    "mlflow",
    "onnx",
    "onnxruntime",
    "onnxscript",
    "optuna",
    "torch",
    "torchvision",
)

# 学習と探索でしか要らない依存。``ml-runtime`` 層はこれらを読んではならない。
# ``hydra`` / ``omegaconf`` を挙げない理由は :data:`HEAVY_DEPENDENCIES` と同じ。
TRAINING_ONLY_DEPENDENCIES = (
    "mlflow",
    "onnx",
    "onnxscript",
    "optuna",
)


def _python_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*.py") if "__pycache__" not in path.parts
    )


def _core_files(root: Path) -> list[Path]:
    """``ml`` コア側の file。ドメイン層の subtree を除く."""

    return [path for path in _python_files(root) if DOMAIN_LAYER not in path.parts]


def _domain_layer_files(root: Path) -> list[Path]:
    """ドメイン層 ``paste_volume`` の subtree の file."""

    return _python_files(root / DOMAIN_LAYER)


def _anchor_for(path: Path) -> Path:
    """``path`` の dotted module 名を組む起点を返す.

    ``src/`` 配下は package root の親、``tests/`` 配下は repository root。取り違えると
    相対 import が黙って別 module へ解決されるので、呼び出し側に選ばせない。
    """

    return SOURCE_ANCHOR if path.is_relative_to(SOURCE_ANCHOR) else PROJECT_ROOT


def _module_name(path: Path) -> str:
    """File を絶対 module 名へ直す."""

    relative = path.relative_to(_anchor_for(path)).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _absolute_imports(path: Path) -> set[str]:
    """その file が import する module の絶対名を返す.

    **相対 import も解決する。** ``pcbasm/vision/__init__.py`` は re-export をすべて相対
    import で書いているので、捨てると ``pcbasm.vision`` の中身が丸ごと走査から漏れる。
    """

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


def _module_file(module: str) -> Path | None:
    """自前 module の実体 file を返す。見つからなければ ``None``."""

    relative = Path(*module.split("."))
    for anchor in (SOURCE_ANCHOR, PROJECT_ROOT):
        for candidate in (
            anchor / relative.with_suffix(".py"),
            anchor / relative / "__init__.py",
        ):
            if candidate.is_file():
                return candidate
    return None


def _reachable_modules(entries: list[Path]) -> set[str]:
    """起点から自前 module の import をたどり、到達する module 名を集める.

    package を import すると ``__init__`` が走るので、``a.b.c`` を見たら ``a`` と ``a.b``
    も到達したものとして数える。取り逃した連鎖は ``metadata`` が ``pcbasm.vision.image`` を
    読んだ結果 ``pcbasm.vision`` の ``__init__`` が走る形だった。
    """

    seen: set[str] = set()
    pending = list(entries)
    while pending:
        path = pending.pop()
        for module in _absolute_imports(path):
            parts = module.split(".")
            for depth in range(1, len(parts) + 1):
                ancestor = ".".join(parts[:depth])
                if ancestor in seen:
                    continue
                seen.add(ancestor)
                if (target := _module_file(ancestor)) is not None:
                    pending.append(target)
    return seen


def _reaches(module: str, forbidden: str) -> bool:
    return module == forbidden or module.startswith(f"{forbidden}.")


def _is_domain_package(imported: str) -> bool:
    return imported.partition(".")[0] in DOMAIN_PACKAGES


def _is_domain_data(imported: str) -> bool:
    """装置ドメイン側の module が、参照を許した「データ構造」の範囲に入るか."""

    return any(_reaches(imported, allowed) for allowed in DOMAIN_DATA_PACKAGES)


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


class TestLayerPartition:
    """コアとドメイン層の切り分けそのものが働いていること.

    以降の検査はどれも「参照しないこと」を見るので、走査対象が空だと素通りする。
    ドメイン層の名前を取り違えると片側が空になり、その側の契約が黙って消える。
    """

    @pytest.mark.parametrize("root", (ML_SOURCE_ROOT, ML_TEST_ROOT))
    def test_both_sides_have_modules_to_check(self, root: Path):
        assert _core_files(root) != []
        assert _domain_layer_files(root) != []

    @pytest.mark.parametrize("root", (ML_SOURCE_ROOT, ML_TEST_ROOT))
    def test_the_two_sides_partition_the_tree(self, root: Path):
        """コアとドメイン層で過不足なくツリーを覆うこと.

        重なると弱いほうの規則で素通りし、漏れるとその file がどの契約にも掛からない。
        """

        core = set(_core_files(root))
        domain = set(_domain_layer_files(root))

        assert core & domain == set()
        assert core | domain == set(_python_files(root))


class TestCoreDomainIndependence:
    """``ml`` コアは装置ドメインを知らない."""

    def test_no_core_module_imports_a_domain_package(self):
        offenders = [
            f"{_module_name(path)} -> {imported}"
            for path in _core_files(ML_SOURCE_ROOT)
            for imported in _absolute_imports(path)
            if _is_domain_package(imported)
        ]

        assert offenders == []

    def test_no_core_test_module_imports_a_domain_package(self):
        offenders = [
            f"{_module_name(path)} -> {imported}"
            for path in _core_files(ML_TEST_ROOT)
            for imported in _absolute_imports(path)
            if _is_domain_package(imported)
        ]

        assert offenders == []

    def test_no_test_module_imports_the_device_test_helper(self):
        """``tests/ml`` はドメイン層も含めて ``tests.helpers`` を参照しない.

        ``tests.helpers`` は module 冒頭で pcbnew と picamera2 を import するので、
        参照すると Raspberry Pi と KiCAD の無い学習機で ``tests/ml`` が collect できない。
        """

        offenders = [
            f"{_module_name(path)} -> {imported}"
            for path in _python_files(ML_TEST_ROOT)
            for imported in _absolute_imports(path)
            if _reaches(imported, FORBIDDEN_TEST_HELPER)
        ]

        assert offenders == []


class TestDomainLayerCoupling:
    """``ml.paste_volume`` は装置ドメインのデータ構造だけを参照する."""

    @pytest.mark.parametrize("root", (ML_SOURCE_ROOT, ML_TEST_ROOT))
    def test_it_imports_only_domain_data_structures(self, root: Path):
        offenders = [
            f"{_module_name(path)} -> {imported}"
            for path in _domain_layer_files(root)
            for imported in _absolute_imports(path)
            if _is_domain_package(imported) and not _is_domain_data(imported)
        ]

        assert offenders == []

    def test_it_actually_imports_a_domain_data_structure(self):
        """許可した参照が実在すること.

        ドメイン層が装置ドメインを 1 つも参照しなくなったら、上の検査は空虚になる。
        そのときは許可リストごと畳んでコアの規則へ寄せるべきで、緑のまま放置しない。
        """

        imported = {
            name
            for path in _domain_layer_files(ML_SOURCE_ROOT)
            for name in _absolute_imports(path)
            if _is_domain_package(name)
        }

        assert imported != set()
        assert all(_is_domain_data(name) for name in imported)

    def test_the_allowlist_separates_data_from_control_logic(self):
        """許可判定そのものが働いていること.

        許可リストが広すぎると上の検査は常に緑になる。制御ロジックを名指しで弾けることと、 prefix の部分一致で隣の
        package を巻き込まないことを別に見る。
        """

        assert _is_domain_data("pcbasm.pasting.dataset.metadata")
        assert _is_domain_data("pcbasm.geometry.packing")
        assert not _is_domain_data("pcbasm.pasting.applicator")
        assert not _is_domain_data("pcbasm.vision.image")
        assert not _is_domain_data("pcbasm.geometry_extra")


class TestTrainingContainerImports:
    """``src/ml`` と ``tests/ml`` が学習コンテナで collect できること.

    コンテナに pcbnew も picamera2 も無いので、装置 HAL へ届く import が 1 本でも入ると collect
    できなくなる。直接の import ではなく推移的な到達を見る。
    """

    @pytest.mark.parametrize("forbidden", DEVICE_ONLY_MODULES)
    def test_nothing_reaches_a_device_only_module(self, forbidden: str):
        entries = [
            *_python_files(ML_SOURCE_ROOT),
            *_python_files(ML_TEST_ROOT),
            DATASET_SCHEMA_FILE,
        ]

        offenders = {
            module
            for module in _reachable_modules(entries)
            if _reaches(module, forbidden)
        }

        assert offenders == set()

    def test_the_transitive_scan_follows_more_than_one_hop(self):
        """連鎖をたどる検査そのものが働いていること.

        上の検査は「到達しないこと」を見るので、走査を弱めるほど通りやすくなる。
        再帰を落としても緑のままなら、検査は直接 import しか見ない版へ黙って退化する。

        起点は ``dataset/recorder.py``。``applicator`` を経て ``pcbasm.hal`` へ届く 2 段の形で、
        直接は HAL を import していない。``applicator`` 自身を起点にすると 1 段で届くので、
        再帰の検証にならない。
        """

        recorder = SOURCE_ANCHOR / "pcbasm" / "pasting" / "dataset" / "recorder.py"

        assert not any(
            _reaches(module, "pcbasm.hal") for module in _absolute_imports(recorder)
        )
        assert any(
            _reaches(module, "pcbasm.hal") for module in _reachable_modules([recorder])
        )

    def test_the_scan_resolves_relative_imports(self):
        """相対 import も解決すること.

        ``pcbasm/vision/__init__.py`` は re-export を相対 import で書いている。捨てると
        ``pcbasm.vision`` の中身が丸ごと走査から漏れ、そこが HAL を引いても緑のままになる。
        """

        reachable = _reachable_modules(
            [SOURCE_ANCHOR / "pcbasm" / "vision" / "__init__.py"]
        )

        assert "pcbasm.vision.detection" in reachable
        assert "pcbasm.vision.overlay" in reachable

    def test_the_scan_counts_a_package_as_reached(self):
        """``a.b.c`` を見たら ``a.b`` にも到達したものとして数えること.

        package を import すると ``__init__`` が走る。取り逃した連鎖は ``metadata`` が
        ``pcbasm.vision.image`` を読んだ結果 ``pcbasm.vision`` の ``__init__`` が走る形だった。
        """

        reachable = _reachable_modules([DATASET_SCHEMA_FILE])

        assert "pcbasm.vision.image" in reachable
        assert "pcbasm.vision" in reachable

    def test_the_scan_crosses_from_the_tests_into_the_sources(self):
        """テスト側の起点から ``src/`` 側の module へたどれること.

        ``tests/ml`` の起点は repository root、``src/ml`` の起点は ``src/`` で、dotted 名の
        組み方が違う。取り違えるとテスト側から伸びる連鎖が丸ごと途切れる。

        起点は装置ドメインを直接 import していない ``test_session.py``。直接 import する
        ``helpers.py`` を起点にすると再帰を落としても通るので、連鎖の検証にならない。
        """

        entry = ML_TEST_ROOT / DOMAIN_LAYER / "test_session.py"

        assert not any(
            _is_domain_package(module) for module in _absolute_imports(entry)
        )
        assert "pcbasm.pasting.dataset.metadata" in _reachable_modules([entry])
        assert _module_file("tests.ml.paste_volume.helpers") is not None

    def test_the_scanner_sees_an_import_it_should_reject(self):
        """判定そのものが働いていることを確かめる.

        禁止 module を 1 つも含まないツリーでは、上の検査は常に緑になる。
        """

        assert _reaches("pcbasm.hal", "pcbasm.hal")
        assert _reaches("pcbasm.hal.camera", "pcbasm.hal")
        assert not _reaches("pcbasm.halt", "pcbasm.hal")


class TestDependencyFreeLayer:
    """成果物 I/O と設定変換は ML の重い依存なしに import できる."""

    def test_importing_them_does_not_load_heavy_dependencies(self):
        loaded = _loaded_dependencies(DEPENDENCY_FREE_MODULES, HEAVY_DEPENDENCIES)

        assert loaded == "[]"


class TestRuntimeLayer:
    """推論経路は ``ml-runtime`` だけで import できる.

    Raspberry Pi 5 へ MLflow / Optuna / ONNX を入れずに済ませるための契約。

    ``ml.tuning.search_space`` と ``ml.tuning.runner`` は optuna を import する
    ``ml-hpo`` 層なので、ここには入れない。

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
