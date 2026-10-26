"""ソースの import が層の規則に従う.

単位は ``pcbasm.<名前>`` と ``web.<名前>``、それに下の層から import される ``_SUBUNITS``。単位の中の import と標準ライブラリはいつでもよい。
pcbasm の単位は L0 の単位を常に import してよく、それより上は ``_RULES`` に挙げた単位だけを import してよい。
L0 の葉（atomic、paths、validators）は標準ライブラリだけに依存する。
web.ui、web.selfupdate、web.dev が import してよい pcbasm は、葉のうち atomic と paths に限る。
pcbnew を import してよいのは kicad だけ。web.contract と web.process は使う 3rd-party を限る。

``_ALLOWED_VIOLATIONS`` は、まだ残っている違反の一覧。後続の変更で 1 行ずつ消す。直した違反を一覧に残すとテストが落ちる。
"""

import ast
import sys
from pathlib import Path

from pcbasm.paths import PROJECT_ROOT

SRC = PROJECT_ROOT / "src"

# 単位の中にあり、下の層から import されるので独立した単位として検査するモジュール
_SUBUNITS = frozenset({"pcbasm.pasting.params"})

_L0 = frozenset(
    f"pcbasm.{name}"
    for name in (
        "geometry",
        "gcode",
        "errors",
        "document",
        "atomic",
        "paths",
        "plotting",
        "validators",
    )
)
# 標準ライブラリだけに依存する L0 の葉
_LEAVES = frozenset({"pcbasm.atomic", "pcbasm.paths", "pcbasm.validators"})
# web.ui、web.selfupdate、web.dev が import してよい pcbasm
_WEB_LEAVES = frozenset({"pcbasm.atomic", "pcbasm.paths"})
_ALIGN = frozenset(
    {"pcbasm.machine", "pcbasm.hal", "pcbasm.vision", "pcbasm.board", "pcbasm.config"}
)

# 単位 → L0 のほかに import してよい単位（接頭辞で照合する。単位の中の import はいつでもよい）
_RULES: dict[str, frozenset[str]] = {
    **{unit: frozenset() for unit in _L0},
    # [solder] の下位表の型
    "pcbasm.config": frozenset({"pcbasm.pasting.params"}),
    # config が import するので、L0 だけに依存する（循環させない）
    "pcbasm.pasting.params": frozenset(),
    "pcbasm.board": frozenset(),
    "pcbasm.vision": frozenset(),
    "pcbasm.hal": frozenset({"pcbasm.vision.image"}),
    "pcbasm.kicad": frozenset({"pcbasm.board"}),
    "pcbasm.machine": frozenset({"pcbasm.config", "pcbasm.hal", "pcbasm.vision"}),
    "pcbasm.migrate": frozenset({"pcbasm.config"}),
    "pcbasm.align": _ALIGN,
    "pcbasm.pasting": _ALIGN | {"pcbasm.align", "pcbasm.kicad"},
    # 塗布と同じ層。互いに import しない
    "pcbasm.pnp": _ALIGN | {"pcbasm.align", "pcbasm.kicad"},
    "pcbasm.commissioning": frozenset({"pcbasm"}),
    "web.process": frozenset(),
    "web.contract": frozenset(),
    # api と ui を子プロセスで起動するだけ
    "web.dev": _WEB_LEAVES,
    "web.selfupdate": frozenset({"web.contract", "web.process"}) | _WEB_LEAVES,
    "web.api": frozenset({"pcbasm", "web.contract", "web.selfupdate", "web.process"}),
    "web.ui": frozenset({"web.contract", "web.selfupdate", "web.process"})
    | _WEB_LEAVES,
}

# 3rd-party を限る単位 → 使ってよい 3rd-party
_THIRD_PARTY_ONLY: dict[str, frozenset[str]] = {
    **{unit: frozenset() for unit in _LEAVES},
    "web.process": frozenset(),
    "web.contract": frozenset({"pydantic"}),
}

# 3rd-party → それを import してよい唯一の単位
_THIRD_PARTY_OWNER = {"pcbnew": "pcbasm.kicad"}

# まだ残っている違反（import する単位, import される単位か 3rd-party）
_ALLOWED_VIOLATIONS: set[tuple[str, str]] = set()


def _module_name(path: Path) -> str:
    parts = path.relative_to(SRC).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


_MODULES = {_module_name(path): path for path in SRC.rglob("*.py")}


def _unit(module: str) -> str:
    return module if module in _SUBUNITS else ".".join(module.split(".")[:2])


def _imported(module: str, node: ast.Import | ast.ImportFrom) -> list[str]:
    """Import 文 1 つが読み込むモジュール名（``from M import N`` は N がモジュールなら M.N）."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        is_package = _MODULES[module].name == "__init__.py"
        package = module.split(".")[: None if is_package else -1]
        package = package[: len(package) - (node.level - 1)]
        base = ".".join([*package, base] if base else package)
    return [
        f"{base}.{alias.name}" if f"{base}.{alias.name}" in _MODULES else base
        for alias in node.names
    ]


def _matches(name: str, prefixes: frozenset[str]) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)


def _violation(unit: str, name: str) -> str | None:
    """規則に反するなら、反した相手（単位か 3rd-party の名前）を返す."""
    root = name.split(".")[0]
    if root in sys.stdlib_module_names:
        return None
    if root not in ("pcbasm", "web"):
        owner = _THIRD_PARTY_OWNER.get(root)
        if owner is not None and unit != owner:
            return root
        if unit in _THIRD_PARTY_ONLY and root not in _THIRD_PARTY_ONLY[unit]:
            return root
        return None
    allowed = _RULES[unit] | {unit}
    if unit.startswith("pcbasm.") and unit not in _LEAVES:
        allowed |= _L0
    return None if _matches(name, allowed) else _unit(name)


def _edges() -> set[tuple[str, str]]:
    """(import する単位, import される単位か 3rd-party) の組."""
    edges: set[tuple[str, str]] = set()
    for module, path in _MODULES.items():
        if "." not in module:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import | ast.ImportFrom):
                edges.update((_unit(module), name) for name in _imported(module, node))
    return edges


def _violations() -> set[tuple[str, str]]:
    return {
        (unit, target)
        for unit, name in _edges()
        if (target := _violation(unit, name)) is not None
    }


class TestLayers:
    """ソースの import が層の規則に従う."""

    def test_every_unit_has_a_rule(self):
        units = {_unit(module) for module in _MODULES if "." in module}

        assert units - _RULES.keys() == set()

    def test_scanner_sees_the_web_imports(self):
        """読み取りが壊れて違反を見落としていないこと."""
        edges = _edges()

        assert ("web.ui", "web.contract.models") in edges
        assert ("web.contract", "pydantic") in edges
        # 相対 import（vision/__init__.py の from .image import ...）
        assert ("pcbasm.vision", "pcbasm.vision.image") in edges
        # from M import <submodule>（machine/devices.py の from pcbasm import hal）
        assert ("pcbasm.machine", "pcbasm.hal") in edges

    def test_pnp_and_pasting_do_not_import_each_other(self):
        """部品実装と塗布は同じ層で、両方が使うものは下の層に置く."""
        assert not _matches("pcbasm.pasting", _RULES["pcbasm.pnp"])
        assert not _matches("pcbasm.pnp", _RULES["pcbasm.pasting"])

    def test_no_violation_outside_the_allowlist(self):
        assert _violations() - _ALLOWED_VIOLATIONS == set()

    def test_allowlist_has_no_resolved_violation(self):
        """直した違反は一覧から消す."""
        assert _ALLOWED_VIOLATIONS - _violations() == set()
