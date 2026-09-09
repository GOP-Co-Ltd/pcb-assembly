"""乱数種の材料へ挟む役割ラベルの契約.

``(global_seed, epoch, sample_id)`` から乱数種を作る用途は 4 つある（幾何 augmentation・
padding 内の配置・view の間引き・batch の並べ替え）。役割ラベルを挟まないと別用途どうしが
同じ材料になり、同じ乱数列から出た値が相関する。実際に踏んだ例は
``memory/agents/orchestrator/paste-volume-data-task.md`` の M1。

規約が module をまたぐので、どの module のテストにも属さないここへ置く。材料の組み立ては
実装の内部だが、契約そのものが「材料をどう作るか」なので private を直接見る。

**「一致しない」型の assert なので、検査が働くことを対で示す。** 役割ラベルを外した材料
どうしが同じ比較関数で衝突すること、および各用途の材料を突き合わせる assert が
ラベルを外すと成り立たなくなることを、それぞれ別のテストで固定する。
"""

from __future__ import annotations

import random

from ml.data.batch import BatchShape, ViewDropout, plan_pixel_budget_batches
from ml.data.image import (
    AugmentationRange,
    _derived_seed,  # pyright: ignore[reportPrivateUsage]
)
from ml.paste_volume.batch import (
    _placement_seed,  # pyright: ignore[reportPrivateUsage]
)

GLOBAL_SEED = 7
EPOCH = 3
SAMPLE_ID = "0a1b2c3d4e5f:000042"

FULL_TURN_DEGREES = 360.0
AVAILABLE_VIEW_COUNT = 5

# 各用途が実際に使う材料。batch-plan だけ sample を跨いだ並べ替えなので sample_id を含まない。
MATERIALS = {
    "augmentation": f"{GLOBAL_SEED}:{EPOCH}:augmentation:{SAMPLE_ID}",
    "placement": f"{GLOBAL_SEED}:{EPOCH}:placement:{SAMPLE_ID}",
    "view-dropout": f"{GLOBAL_SEED}:{EPOCH}:view-dropout:{SAMPLE_ID}",
    "batch-plan": f"{GLOBAL_SEED}:{EPOCH}:batch-plan",
}

# 役割ラベルを外した材料。sample 単位の 3 用途は区別が消えて同じ文字列になる。
SAMPLE_SCOPED_ROLES = ("augmentation", "placement", "view-dropout")
UNLABELLED_MATERIAL = f"{GLOBAL_SEED}:{EPOCH}:{SAMPLE_ID}"


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

        役割ラベルを外すと sample 単位の 3 用途は同じ材料へ潰れる。

        上の検査と同じ比較関数がそれを衝突として報告することを見る。

        報告できないなら、上の検査は材料が何であっても緑になる。
        """

        unlabelled = tuple(UNLABELLED_MATERIAL for _ in SAMPLE_SCOPED_ROLES)

        assert _distinct_material_count(unlabelled) == 1


class TestMaterialsInUse:
    """表に書いた材料が、実装が実際に使っているものであること.

    材料が使われていなければ ``TestRoleLabels`` は文字列表の自己完結した検査になる。
    用途ごとに、その材料から作った乱数列が実装の出力と一致することを見る。
    """

    def test_augmentation_uses_its_labelled_material(self):
        parameters = AugmentationRange(
            minimum_scale=1.0, maximum_scale=1.0
        ).parameters_for(sample_id=SAMPLE_ID, global_seed=GLOBAL_SEED, epoch=EPOCH)

        expected = (
            random.Random(_derived_seed(MATERIALS["augmentation"])).random()
            * FULL_TURN_DEGREES
        )

        assert parameters.rotation_degrees == expected

    def test_augmentation_does_not_use_the_unlabelled_material(self):
        """上の一致検査がラベルまで見ていることの自己検査."""

        parameters = AugmentationRange(
            minimum_scale=1.0, maximum_scale=1.0
        ).parameters_for(sample_id=SAMPLE_ID, global_seed=GLOBAL_SEED, epoch=EPOCH)

        unlabelled = (
            random.Random(_derived_seed(UNLABELLED_MATERIAL)).random()
            * FULL_TURN_DEGREES
        )

        assert parameters.rotation_degrees != unlabelled

    def test_placement_uses_its_labelled_material(self):
        assert _placement_seed(GLOBAL_SEED, EPOCH, SAMPLE_ID) == _derived_seed(
            MATERIALS["placement"]
        )

    def test_placement_does_not_use_the_unlabelled_material(self):
        assert _placement_seed(GLOBAL_SEED, EPOCH, SAMPLE_ID) != _derived_seed(
            UNLABELLED_MATERIAL
        )

    def test_view_dropout_uses_its_labelled_material(self):
        indices, reason = ViewDropout(minimum_view_count=1).view_indices_for(
            (SAMPLE_ID,),
            available_view_count=AVAILABLE_VIEW_COUNT,
            global_seed=GLOBAL_SEED,
            epoch=EPOCH,
        )
        assert indices is not None, reason

        assert indices == (_expected_view_indices(MATERIALS["view-dropout"]),)

    def test_view_dropout_does_not_use_the_unlabelled_material(self):
        indices, reason = ViewDropout(minimum_view_count=1).view_indices_for(
            (SAMPLE_ID,),
            available_view_count=AVAILABLE_VIEW_COUNT,
            global_seed=GLOBAL_SEED,
            epoch=EPOCH,
        )
        assert indices is not None, reason

        assert indices != (_expected_view_indices(UNLABELLED_MATERIAL),)

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


def _expected_view_indices(material: str) -> tuple[int, ...]:
    """``ViewDropout.view_indices_for`` が材料から引く view 番号列を組み直す."""

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
