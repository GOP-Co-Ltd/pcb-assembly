from __future__ import annotations

import json
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


class TestMlDependencyGroups:
    def test_ml_dependencies_are_not_in_normal_runtime(self):
        config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
        normal_dependencies = " ".join(config["project"]["dependencies"])

        for package in ("torch", "torchvision", "onnxruntime", "hydra", "mlflow"):
            assert package not in normal_dependencies

    def test_dependency_groups_preserve_pipeline_boundaries(self):
        config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
        groups = config["dependency-groups"]

        assert {"ml-runtime", "ml-train", "ml-hpo", "ml-export"} <= set(groups)
        assert "torch>=2.12,<2.13" in groups["ml-runtime"]
        assert "torchvision>=0.27,<0.28" in groups["ml-runtime"]
        assert {"include-group": "ml-runtime"} in groups["ml-train"]
        assert {"include-group": "ml-train"} in groups["ml-hpo"]
        assert "hydra-optuna-sweeper==1.4.0.dev9" in groups["ml-hpo"]
        assert {"include-group": "ml-runtime"} in groups["ml-export"]
        assert "mlflow>=3.15,<4" in groups["ml-export"]

    def test_arm_torch_index_is_limited_to_linux_aarch64(self):
        config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
        marker = "sys_platform == 'linux' and platform_machine == 'aarch64'"

        assert config["tool"]["uv"]["sources"]["torch"] == [
            {"index": "pytorch-cpu", "marker": marker}
        ]
        assert config["tool"]["uv"]["sources"]["torchvision"] == [
            {"index": "pytorch-cpu", "marker": marker}
        ]

    def test_hydra_scan_discovers_only_lightweight_project_sweeper(self):
        code = """
import json
import sys
from hydra.core.plugins import Plugins
from hydra.plugins.sweeper import Sweeper

plugins = [
    plugin
    for plugin in Plugins.instance().discover(Sweeper)
    if plugin.__module__ == "hydra_plugins.pcbasm_paste_volume"
]
forbidden = (
    "pcbasm.pasting.paste_volume.hpo_sweeper",
    "optuna",
    "torch",
)
print(json.dumps({
    "plugins": [f"{plugin.__module__}.{plugin.__name__}" for plugin in plugins],
    "forbidden": [
        prefix
        for prefix in forbidden
        if any(name == prefix or name.startswith(f"{prefix}.") for name in sys.modules)
    ],
}))
"""

        completed = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        assert result == {
            "plugins": ["hydra_plugins.pcbasm_paste_volume.PersistentOptunaSweeper"],
            "forbidden": [],
        }


class TestMlBuildTargets:
    def test_wheel_includes_core_and_hydra_plugin_packages(self):
        config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())

        assert config["build-system"]["requires"] == ["uv_build>=0.12.0,<0.13.0"]
        assert config["tool"]["uv"]["build-backend"]["module-name"] == [
            "pcbasm",
            "hydra_plugins.pcbasm_paste_volume",
        ]

    def test_built_wheel_contains_ml_cli_plugin_and_hydra_resources(self, tmp_path):
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
            check=True,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        wheel = next(tmp_path.glob("pcbasm-*.whl"))

        with zipfile.ZipFile(wheel) as archive:
            packaged = set(archive.namelist())

        assert {
            "hydra_plugins/pcbasm_paste_volume/__init__.py",
            "pcbasm/cli/paste_volume.py",
            "pcbasm/pasting/paste_volume/conf/train.yaml",
            "pcbasm/pasting/paste_volume/conf/hydra/sweeper/paste_volume_optuna.yaml",
        } <= packaged

    def test_normal_setup_does_not_install_all_ml_groups(self):
        makefile = (PROJECT_ROOT / "Makefile").read_text()
        setup_recipe = makefile.split("setup:", maxsplit=1)[1].split(
            "setup-ml:", maxsplit=1
        )[0]

        assert "--all-groups" not in setup_recipe

    def test_ml_setup_and_smoke_targets_are_available(self):
        makefile = (PROJECT_ROOT / "Makefile").read_text()

        assert "setup-ml:" in makefile
        assert "uv sync --locked --all-extras --all-groups" in makefile
        assert "ml-smoke:" in makefile
        assert (
            "uv run --locked --all-groups python -m pcbasm.cli.paste_volume smoke"
            in makefile
        )

    def test_setup_targets_require_the_lockfile(self):
        makefile = (PROJECT_ROOT / "Makefile").read_text()
        setup_recipe = makefile.split("setup:", maxsplit=1)[1].split(
            "setup-ml:", maxsplit=1
        )[0]

        assert "uv sync --locked --all-extras" in setup_recipe

    def test_ci_builds_wheel_when_makefile_changes(self):
        pipeline = (PROJECT_ROOT / ".gitlab-ci.yml").read_text()

        assert "          - Makefile\n" in pipeline
        assert "wheel:\n" in pipeline
        assert "uv build --wheel --out-dir dist" in pipeline
