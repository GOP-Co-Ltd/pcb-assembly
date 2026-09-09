"""乱数種の材料へ挟む役割ラベルの契約.

``(global_seed, epoch, ...)`` から乱数種を作る用途は 4 つある。

- ``augmentation``: 幾何 augmentation。sample 単位。``_derived_seed`` を通る
- ``placement``: padding 内の配置。sample 単位。``sha256_bytes`` を通る
- ``view-dropout``: view の間引き。batch 単位で、材料には batch の id 列が入る
- ``batch-plan``: batch の並べ替え。batch を跨ぐので sample の id は入らない

材料の作り方だけが共通で、種へ落とす関数は用途ごとに違う。役割ラベルを挟まないと別用途
どうしが同じ材料になり、同じ乱数列から出た値が相関する。実際に踏んだ例は
``memory/agents/orchestrator/paste-volume-data-task.md`` の M1。

規約が module をまたぐので、どの module のテストにも属さないここへ置く。

契約は 3 層で固定する。

1. :class:`TestRoleLabels` — 4 用途の材料が互いに一致しない（と、ラベルを外すと
   同じ比較関数が衝突を報告する自己検査）
2. :class:`TestMaterialsInUse` — 表に書いた材料が実装の出力と実際に一致する
   （と、ラベルを外した材料では一致しない自己検査）。``placement`` の観測点だけは
   実 sample を要求するので ``tests/ml/paste_volume/test_batch.py`` の
   ``TestPadding`` にあり、材料の組み立てはこの module から import して共有する
3. :class:`TestEveryUseIsLabelled` — 呼び出し元を走査して、種の材料に役割ラベルが
   入っていることを機械検証する（と、ラベル無しの呼び出しを注入すると報告する自己検査）

**3 が無いと 1 と 2 は手書きの 4 用途表になる。** 5 番目の用途がラベル無しで入っても
全緑になり、規則は黙って退化する。

種の材料は実装の内部なので、``_derived_seed`` も ``_placement_seed`` も import しない。
private を import すると期待値が f(x) と f(x) の比較になって検査が同語反復になる。
材料から種を作る規則は :func:`derived_seed` として test 側で組み直してある。
"""

from __future__ import annotations

import ast
import hashlib
import random
from pathlib import Path

import attrs

from ml.data.batch import BatchShape, ViewDropout, plan_pixel_budget_batches
from ml.data.image import AugmentationRange
from tests.ml.helpers import PROJECT_ROOT

SOURCE_ANCHOR = PROJECT_ROOT / "src"
ML_SOURCE_ROOT = SOURCE_ANCHOR / "ml"

GLOBAL_SEED = 7
EPOCH = 3
SAMPLE_ID = "0a1b2c3d4e5f:000042"

FULL_TURN_DEGREES = 360.0

# view-dropout の材料検査に使う view 数。
#
# **``count < AVAILABLE_VIEW_COUNT`` になる値を選ぶこと。** count が view 数と等しいと
# ``sample(range(n), n)`` が乱数列に関係なく全 index を返し、材料を区別しない観測点に
# なる。5 では ``randint(1, 5)`` が 5 を引き、任意材料の 21% が同じ ``(0, 1, 2, 3, 4)``
# を返していた（実測）。8 では count 2 / ``(1, 2)`` で、ラベルを衝突させると
# count 1 / ``(7,)`` になる。
AVAILABLE_VIEW_COUNT = 8

# 既知の役割ラベル。走査が材料の中から探す文字列でもある。
ROLE_LABELS = ("augmentation", "batch-plan", "placement", "view-dropout")

# 材料へ入れる役割ラベルのうち、sample の id も材料へ入れる用途。
#
# view-dropout は batch 単位だが、材料に入るのは batch の id 列なので、1 sample の
# batch では sample 単位の 2 用途と同じ形になる。ラベルを外したときに 3 用途が同じ
# 文字列へ潰れるのはこの形のとき。
SAMPLE_SCOPED_ROLES = ("augmentation", "placement", "view-dropout")


def derived_seed(material: str) -> int:
    """材料から 64 bit の種を作る規則.

    実装（``ml.data.image._derived_seed`` と ``ml.paste_volume.batch._placement_seed``）
    と同じ規則を、private を import せずに test 側で組み直したもの。private を呼ぶと
    期待値が実装そのものになり、材料の違いを見ていることにならない。
    """

    return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big")


def sample_scoped_material(
    role: str | None, *, global_seed: int, epoch: int, sample_id: str
) -> str:
    """Sample の id を材料へ入れる用途の種の材料。``role`` が ``None`` ならラベル無し."""

    label = "" if role is None else f"{role}:"
    return f"{global_seed}:{epoch}:{label}{sample_id}"


MATERIALS = {
    role: sample_scoped_material(
        role, global_seed=GLOBAL_SEED, epoch=EPOCH, sample_id=SAMPLE_ID
    )
    for role in SAMPLE_SCOPED_ROLES
} | {"batch-plan": f"{GLOBAL_SEED}:{EPOCH}:batch-plan"}

UNLABELLED_MATERIAL = sample_scoped_material(
    None, global_seed=GLOBAL_SEED, epoch=EPOCH, sample_id=SAMPLE_ID
)


def _distinct_material_count(materials: tuple[str, ...]) -> int:
    """材料のうち互いに異なるものの数を返す."""

    return len(set(materials))


class TestRoleLabels:
    """4 用途の材料が互いに一致しないこと."""

    def test_gives_every_role_a_distinct_material(self):
        materials = tuple(MATERIALS[role] for role in sorted(MATERIALS))

        assert _distinct_material_count(materials) == len(MATERIALS)

    def test_the_same_check_reports_a_collision_without_the_labels(self):
        """検査が働くことの自己検査.

        役割ラベルを外すと sample の id を材料へ入れる 3 用途は同じ材料へ潰れる。

        上の検査と同じ比較関数がそれを衝突として報告することを見る。

        報告できないなら、上の検査は材料が何であっても緑になる。
        """

        unlabelled = tuple(UNLABELLED_MATERIAL for _ in SAMPLE_SCOPED_ROLES)

        assert _distinct_material_count(unlabelled) == 1

    def test_the_table_covers_every_known_role(self):
        """表と走査が同じ役割ラベルの集合を見ていること."""

        assert sorted(MATERIALS) == sorted(ROLE_LABELS)


class TestMaterialsInUse:
    """表に書いた材料が、実装が実際に使っているものであること.

    材料が使われていなければ :class:`TestRoleLabels` は文字列表の自己完結した検査に
    なる。用途ごとに、その材料から作った乱数列が実装の出力と一致することを見る。

    ``placement`` は実 sample を要求するので、観測点は
    ``tests/ml/paste_volume/test_batch.py`` の ``TestPadding`` にある。
    """

    def test_augmentation_uses_its_labelled_material(self):
        parameters = AugmentationRange(
            minimum_scale=1.0, maximum_scale=1.0
        ).parameters_for(sample_id=SAMPLE_ID, global_seed=GLOBAL_SEED, epoch=EPOCH)

        expected = (
            random.Random(derived_seed(MATERIALS["augmentation"])).random()
            * FULL_TURN_DEGREES
        )

        assert parameters.rotation_degrees == expected

    def test_augmentation_does_not_use_the_unlabelled_material(self):
        """上の一致検査がラベルまで見ていることの自己検査."""

        parameters = AugmentationRange(
            minimum_scale=1.0, maximum_scale=1.0
        ).parameters_for(sample_id=SAMPLE_ID, global_seed=GLOBAL_SEED, epoch=EPOCH)

        unlabelled = (
            random.Random(derived_seed(UNLABELLED_MATERIAL)).random()
            * FULL_TURN_DEGREES
        )

        assert parameters.rotation_degrees != unlabelled

    def test_view_dropout_uses_its_labelled_material(self):
        indices = _view_indices()

        assert indices == (_expected_view_indices(MATERIALS["view-dropout"]),)

    def test_view_dropout_does_not_use_the_unlabelled_material(self):
        indices = _view_indices()

        assert indices != (_expected_view_indices(UNLABELLED_MATERIAL),)

    def test_view_dropout_keeps_fewer_views_than_are_available(self):
        """観測点が材料を区別できる形であることの自己検査.

        引いた view 数が ``AVAILABLE_VIEW_COUNT`` と等しいと、
        ``sample(range(n), n)`` は乱数列に関係なく ``(0, ..., n-1)`` を返し、
        上の 2 件は材料が何であっても緑になる。
        """

        kept = _view_indices()[0]

        assert 0 < len(kept) < AVAILABLE_VIEW_COUNT

    def test_batch_plan_uses_its_labelled_material(self):
        shapes = _one_bucket_shapes()

        plan = plan_pixel_budget_batches(
            shapes,
            max_batch_pixels=32 * 32,
            max_batch_size=1,
            stride=8,
            seed=GLOBAL_SEED,
            epoch=EPOCH,
        )

        assert plan == _expected_batch_plan(MATERIALS["batch-plan"], shapes)

    def test_batch_plan_does_not_use_a_material_without_its_label(self):
        shapes = _one_bucket_shapes()

        plan = plan_pixel_budget_batches(
            shapes,
            max_batch_pixels=32 * 32,
            max_batch_size=1,
            stride=8,
            seed=GLOBAL_SEED,
            epoch=EPOCH,
        )

        assert plan != _expected_batch_plan(f"{GLOBAL_SEED}:{EPOCH}", shapes)


def _view_indices() -> tuple[tuple[int, ...], ...]:
    """1 sample の batch で ``ViewDropout`` が残す view 番号列."""

    indices, reason = ViewDropout(minimum_view_count=1).view_indices_for(
        (SAMPLE_ID,),
        available_view_count=AVAILABLE_VIEW_COUNT,
        global_seed=GLOBAL_SEED,
        epoch=EPOCH,
    )
    assert indices is not None, reason
    return indices


def _expected_view_indices(material: str) -> tuple[int, ...]:
    """``ViewDropout.view_indices_for`` が材料から引く view 番号列を組み直す.

    引く枚数そのものが材料に依存するので、番号列の中に枚数も含まれている。
    """

    generator = random.Random(material)
    count = generator.randint(1, AVAILABLE_VIEW_COUNT)
    return tuple(sorted(generator.sample(range(AVAILABLE_VIEW_COUNT), count)))


def _one_bucket_shapes() -> tuple[BatchShape, ...]:
    """寸法と view 数が同じ sample。bucket が 1 個になり並べ替えだけが効く."""

    return tuple(
        BatchShape(sample_id=f"sample-{number:02d}", height=32, width=32, view_count=1)
        for number in range(8)
    )


def _expected_batch_plan(
    material: str, shapes: tuple[BatchShape, ...]
) -> tuple[tuple[str, ...], ...]:
    """``plan_pixel_budget_batches`` が材料から作る並びを組み直す.

    bucket が 1 個なので key の shuffle は乱数を消費しない。``max_batch_size=1`` なので
    batch は並べ替えた順に 1 件ずつ切り出される。
    """

    order = sorted(shape.sample_id for shape in shapes)
    random.Random(material).shuffle(order)
    return tuple((sample_id,) for sample_id in order)


# 種の材料を受け取る呼び出し。材料が文字列（f-string か文字列 literal）のものだけを見る。
# 整数を受け取る ``random.Random(seed)`` は、ラベル済みの材料から作った種の下流なので
# 対象にしない。
SEED_CALLEES = ("Random", "_derived_seed", "sha256_bytes")

# 材料へ ``epoch`` を混ぜる f-string は、呼び出し先が何であっても対象にする。
# 用途が増えるとき最初に書かれるのはこの形なので、関数名の一覧より先に効く。
EPOCH_HINT = "epoch"

# 役割ラベルの規則の対象外にする材料。
#
# ``ml.data.split`` の fold seed は ``(global_seed, epoch, sample_id)`` 系列ではなく、
# 材料へ入る ``dimension`` そのものが用途の区別になっている。
# 空にしないこと。:meth:`TestEveryUseIsLabelled.
# test_every_exempted_material_is_still_in_the_sources` が実在を確かめる。
EXEMPT_MATERIALS = (("ml.data.split", "f'{seed}:{dimension}:{value}'"),)


@attrs.frozen
class SeedMaterialSite:
    """乱数種の材料を組み立てている 1 箇所."""

    module: str
    line: int
    source: str
    labels: frozenset[str]
    """材料の中に見つかった既知の役割ラベル。空なら規則違反."""


def seed_material_sites(paths: list[Path]) -> list[SeedMaterialSite]:
    """渡した file から、乱数種の材料を組み立てている箇所を集める.

    走査で見るのは 2 つの形。``SEED_CALLEES`` へ文字列を渡す呼び出しと、
    ``epoch`` を混ぜる f-string。前者は種を作る関数の側から、後者は材料の形の側から
    同じ規則を掛ける。``.encode()`` は剥がしてから材料を見る。

    役割ラベルは材料の literal 部分と、材料へ差し込んだ module 直下の文字列定数の
    両方から探す。``_AUGMENTATION_ROLE`` のように定数名で挟む形が実装の既定なので、
    literal だけを見ると全ての用途を取り逃がす。
    """

    sites: list[SeedMaterialSite] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_string_constants(tree)
        found: dict[tuple[int, str], ast.expr] = {}
        for node in ast.walk(tree):
            material = _seed_material_of(node)
            if material is not None:
                found[(material.lineno, ast.unparse(material))] = material
        for (line, source), material in sorted(found.items()):
            resolved = {
                constants[name]
                for name in _interpolated_names(material)
                if name in constants
            }
            text = _literal_text(material)
            sites.append(
                SeedMaterialSite(
                    module=_module_name(path),
                    line=line,
                    source=source,
                    labels=frozenset(
                        label
                        for label in ROLE_LABELS
                        if label in text or label in resolved
                    ),
                )
            )
    return sites


def _ml_source_files() -> list[Path]:
    return sorted(
        path for path in ML_SOURCE_ROOT.rglob("*.py") if "__pycache__" not in path.parts
    )


def _module_name(path: Path) -> str:
    if path.is_relative_to(SOURCE_ANCHOR):
        return ".".join(path.relative_to(SOURCE_ANCHOR).with_suffix("").parts)
    return path.stem


def _seed_material_of(node: ast.AST) -> ast.expr | None:
    """その node が組み立てている種の材料を返す。材料でなければ ``None``."""

    if isinstance(node, ast.Call) and node.args and _callee_name(node) in SEED_CALLEES:
        return _string_expression(node.args[0])
    if isinstance(node, ast.JoinedStr) and any(
        EPOCH_HINT in name for name in _interpolated_names(node)
    ):
        return node
    return None


def _callee_name(call: ast.Call) -> str:
    """呼び出し先の末尾の名前。``random.Random`` でも ``Random`` を返す."""

    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return call.func.id if isinstance(call.func, ast.Name) else ""


def _string_expression(node: ast.expr) -> ast.expr | None:
    """``.encode()`` を剥がして、文字列を組み立てている式を返す."""

    while (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "encode"
    ):
        node = node.func.value
    if isinstance(node, ast.JoinedStr):
        return node
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node
    return None


def _interpolated_names(node: ast.expr) -> set[str]:
    """F-string へ差し込んだ名前の集合."""

    if not isinstance(node, ast.JoinedStr):
        return set()
    return {
        name.id
        for value in node.values
        if isinstance(value, ast.FormattedValue)
        for name in ast.walk(value.value)
        if isinstance(name, ast.Name)
    }


def _literal_text(node: ast.expr) -> str:
    """材料のうち、literal として書かれている部分をつないだ文字列."""

    if isinstance(node, ast.Constant):
        return str(node.value)
    if not isinstance(node, ast.JoinedStr):
        return ""
    return "".join(
        value.value
        for value in node.values
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    )


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module 直下の文字列定数。材料へ定数名で挟んだラベルを解決するのに使う."""

    values: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        if not (
            isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        ):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        values.update(
            {
                target.id: node.value.value
                for target in targets
                if isinstance(target, ast.Name)
            }
        )
    return values


class TestEveryUseIsLabelled:
    """役割ラベルの規則を、手書きの表ではなく呼び出し元の走査で機械検証する.

    :class:`TestRoleLabels` と :class:`TestMaterialsInUse` は 4 用途を手で並べた表なので、
    5 番目の用途は原理的に見えない。実測でも「``AugmentationRange`` の scale だけを
    ラベル無しの材料から引く」変異は ``tests/ml`` 全体で全緑だった。機械検証されない
    規則は必ず退化するので、走査を対に置く。
    """

    def test_every_seed_material_in_the_sources_carries_a_role_label(self):
        offenders = [
            f"{site.module}:{site.line} {site.source}"
            for site in seed_material_sites(_ml_source_files())
            if not site.labels and (site.module, site.source) not in EXEMPT_MATERIALS
        ]

        assert offenders == []

    def test_the_scan_finds_every_known_role(self):
        """走査が痩せていないこと.

        上は「違反が無い」型の assert なので、走査が何も拾わなくなっても緑になる。 4
        用途ぜんぶを実際に見つけていることを別に見る。
        """

        found = {
            label
            for site in seed_material_sites(_ml_source_files())
            for label in site.labels
        }

        assert found == set(ROLE_LABELS)

    def test_the_scan_covers_both_the_core_and_the_domain_layer(self):
        """走査対象が ``ml`` コアと ``ml.paste_volume`` の両方に届いていること."""

        modules = {site.module for site in seed_material_sites(_ml_source_files())}

        assert any(module.startswith("ml.paste_volume") for module in modules)
        assert any(not module.startswith("ml.paste_volume") for module in modules)

    def test_the_same_scan_reports_a_use_without_a_label(self, tmp_path: Path):
        """走査が働くことの自己検査.

        ラベル無しの用途を 1 つ注入し、同じ走査がそれを役割ラベル無しとして報告する
        ことを見る。報告できないなら、上の検査は ``src/ml`` が何であっても緑になる。
        """

        injected = tmp_path / "unlabelled.py"
        injected.write_text(
            "import random\n"
            "\n"
            "def choose(global_seed, epoch, sample_id):\n"
            '    return random.Random(f"{global_seed}:{epoch}:{sample_id}").random()\n',
            encoding="utf-8",
        )

        sites = seed_material_sites([injected])

        assert [site.labels for site in sites] == [frozenset()]

    def test_the_scan_resolves_a_label_held_in_a_module_constant(self, tmp_path: Path):
        """定数名で挟んだラベルを解決し、ラベルでない定数は通さないこと.

        実装は ``_AUGMENTATION_ROLE`` / ``_PLACEMENT_ROLE`` の形でラベルを持つので、
        解決できないと全ての用途が違反として報告され、上の検査は誤検出で赤くなる。
        逆に定数の中身を見ないと、どんな定数でもラベル扱いになって素通りする。
        """

        module = tmp_path / "constants.py"
        module.write_text(
            "import random\n"
            '\n_ROLE = "placement"\n'
            '_OTHER = "whatever"\n'
            "\n"
            "def labelled(global_seed, epoch, sample_id):\n"
            '    return random.Random(f"{global_seed}:{epoch}:{_ROLE}:{sample_id}")\n'
            "\n"
            "def unlabelled(global_seed, epoch, sample_id):\n"
            '    return random.Random(f"{global_seed}:{epoch}:{_OTHER}:{sample_id}")\n',
            encoding="utf-8",
        )

        sites = seed_material_sites([module])

        assert [sorted(site.labels) for site in sites] == [["placement"], []]

    def test_the_scan_sees_a_material_hidden_behind_encode(self, tmp_path: Path):
        """``.encode()`` を剥がして材料を見ること.

        ``_placement_seed`` は ``sha256_bytes(f"...".encode())`` の形なので、剥がさないと
        ``placement`` の用途が走査から丸ごと落ちる。
        """

        module = tmp_path / "encoded.py"
        module.write_text(
            "from ml.artifact.fingerprint import sha256_bytes\n"
            "\n"
            "def seed(global_seed, sample_id):\n"
            '    return sha256_bytes(f"{global_seed}:placement:{sample_id}".encode())\n',
            encoding="utf-8",
        )

        sites = seed_material_sites([module])

        assert [sorted(site.labels) for site in sites] == [["placement"]]

    def test_every_exempted_material_is_still_in_the_sources(self):
        """対象外リストが古びていないこと.

        消えた材料が残っていると、同じ形の新しい違反を黙って免除してしまう。
        """

        scanned = {
            (site.module, site.source)
            for site in seed_material_sites(_ml_source_files())
        }

        assert set(EXEMPT_MATERIALS) <= scanned
