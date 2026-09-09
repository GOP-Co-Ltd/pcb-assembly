"""``ml.training`` 実装の公開契約.

Trainer は dataset の中身も loss の形も知らない。知っているのは「epoch ごとに sample ID の 並びが
決まり、それを batch へ実体化できる」ことと「batch を渡すと微分可能な 0 次元 loss と
観測値が返る」ことだけ。その契約をここで固定する。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

import pytest
import torch
from torch import Tensor, nn

from ml.data.image import AugmentationRange, ImageConstraints, ImageShape
from ml.data.split import SplitManifest, SplitRatios
from ml.evaluation.compile_parity import (
    FLOAT32_PARITY_TOLERANCES,
    CompileOptions,
    CompileParityResult,
)
from ml.model.loss import weighted_gaussian_negative_log_likelihood
from ml.model.multiview import MultiViewGaussianRegressor
from ml.paste_volume.batch import PasteVolumeBatch, PasteVolumeCollator
from ml.paste_volume.index import PasteVolumeSampleIndex
from ml.paste_volume.model import (
    INPUT_CHANNELS,
    PasteVolumeModelConfig,
    build_paste_volume_model,
)
from ml.paste_volume.task import (
    PasteVolumeTask,
    PasteVolumeTrainingConfig,
    PasteVolumeTrainingData,
)
from tests.ml.helpers import skip_if_no_inductor
from tests.ml.paste_volume.helpers import (
    CROP_SIZE_PX,
    PASTE_VOLUME_DATASET_DIR,
    PIXEL_PER_MM,
    VIEW_COUNT,
    SyntheticCell,
    skip_if_no_real_sessions,
    write_session,
)

CONSTRAINTS = ImageConstraints()
DEVICE = torch.device("cpu")

# split を 3 つとも埋めるには group が 3 個以上要る
CELLS = tuple(
    SyntheticCell(
        index=number,
        commanded_volume_ul=None if number == 4 else 0.05 * number,
        x_mm=0.5 + 2.2 * number,
    )
    for number in range(1, 13)
)


def _index(
    tmp_path: Path,
    *,
    sessions: int = 1,
    cells: tuple[SyntheticCell, ...] = CELLS,
    machine: str = "m",
) -> PasteVolumeSampleIndex:
    roots = [
        write_session(
            tmp_path / f"session-{number}", cells=cells, machine_id=f"{machine}{number}"
        )
        for number in range(sessions)
    ]
    index, reason = PasteVolumeSampleIndex.from_roots(roots, constraints=CONSTRAINTS)
    assert index is not None, reason
    return index


def _cell_config(**overrides: object) -> PasteVolumeTrainingConfig:
    """Cell 次元の設定。既定は session 次元なので明示する.

    既定を cell にすると呼び出し側が黙って session の漏れる split を選ぶ。

    cell 単位 split を見るテストは毎回そう書く。
    """

    return PasteVolumeTrainingConfig(split_dimension="cell", **overrides)  # type: ignore[arg-type]


def _session_config(**overrides: object) -> PasteVolumeTrainingConfig:
    """Session 次元（leave-one-session-out）の設定."""

    return PasteVolumeTrainingConfig(split_dimension="session", **overrides)  # type: ignore[arg-type]


def _data(
    tmp_path: Path,
    *,
    index: PasteVolumeSampleIndex | None = None,
    collator: PasteVolumeCollator | None = None,
    **overrides: object,
) -> PasteVolumeTrainingData:
    data, reason = PasteVolumeTrainingData.build(
        index if index is not None else _index(tmp_path),
        collator=collator or PasteVolumeCollator(global_seed=5),
        config=_cell_config(**overrides),
    )
    assert data is not None, reason
    return data


def _area_bucket(shape: ImageShape) -> int:
    """``plan_pixel_budget_batches`` が bucket を切る単位（log2 面積の 1.0 刻み）."""

    return round(math.log2(shape.height * shape.width))


def _sessions_of(index: PasteVolumeSampleIndex, sample_ids: Sequence[str]) -> set[str]:
    """その sample 群が由来する session fingerprint の集合.

    「held-out が train へ現れない」を測る観測点。同じ関数で漏れている manifest も
    測り、検査が働くことを対で示す。
    """

    return {index.entry_for(sample_id).session_fingerprint for sample_id in sample_ids}


def _fold_sizes(
    index: PasteVolumeSampleIndex, data: PasteVolumeTrainingData
) -> tuple[int, int, int]:
    """(test, validation, train) それぞれに入った session の本数."""

    return (
        len(_sessions_of(index, data.sample_ids_for("test"))),
        len(_sessions_of(index, data.sample_ids_for("validation"))),
        len(_sessions_of(index, data.sample_ids_for("train"))),
    )


@pytest.fixture(scope="module")
def real_index() -> PasteVolumeSampleIndex:
    """実収集 session の index。全画像を decode するので module で 1 度だけ作る."""

    index, reason = PasteVolumeSampleIndex.from_roots(
        [PASTE_VOLUME_DATASET_DIR], constraints=CONSTRAINTS
    )
    assert index is not None, reason
    return index


class TestBuild:
    """組み立てと設定の検証."""

    def test_splits_every_sample_into_exactly_one_split(self, tmp_path: Path):
        data = _data(tmp_path)

        assigned = [
            sample_id
            for split in ("train", "validation", "test")
            for sample_id in data.sample_ids_for(split)  # type: ignore[arg-type]
        ]

        assert sorted(assigned) == sorted(
            entry.sample_id for entry in data.index.entries
        )
        assert len(set(assigned)) == len(assigned)

    def test_never_splits_one_physical_cell(self, tmp_path: Path):
        """同じ座標の cell は必ず同じ split.

        銅板の背景テクスチャが train と test へ分かれて漏れるのを防ぐ。
        """

        data = _data(tmp_path)

        assert (
            data.split_manifest.validate(
                data.index.sample_groups(dimension="cell"),
                dataset_fingerprint=data.dataset_fingerprint,
            )
            is None
        )

    def test_exposes_the_dataset_fingerprint_of_the_index(self, tmp_path: Path):
        data = _data(tmp_path)

        assert data.dataset_fingerprint == data.index.dataset_fingerprint

    def test_reports_an_augmentation_range_the_images_cannot_take(self, tmp_path: Path):
        """縮小しすぎて下限を割る設定を build で弾く.

        ここで弾けば、どの epoch でも前処理がサイズを理由に sample を落とすことは
        起こり得なくなる。materialize の途中で落ちると plan_epoch の計画と食い違う。
        """

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path),
            collator=PasteVolumeCollator(
                augmentation=AugmentationRange(minimum_scale=0.01, maximum_scale=1.0)
            ),
            config=_cell_config(),
        )

        assert data is None
        assert reason is not None
        assert "minimum_size" in reason or "minimum_scale" in reason

    def test_reports_a_dataset_with_too_few_cell_groups(self, tmp_path: Path):
        """物理 cell が 3 個に満たないと split を作れない.

        group が不可分なので、train / validation / test を埋めるには最低 3 個要る。
        比率をどう振っても各 split へ最低 1 group が入るため、空の split は作られない。
        """

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path, cells=CELLS[:2]),
            collator=PasteVolumeCollator(),
            config=_cell_config(),
        )

        assert data is None
        assert reason is not None
        assert "group" in reason

    def test_reports_a_split_dimension_outside_the_contract(self, tmp_path: Path):
        """契約外の次元は例外ではなく理由で返す.

        通すと ``sample_groups`` の ``match`` を素通りして ``None`` が返り、
        ``TypeError: cannot unpack non-iterable NoneType`` になる。TOML や argv から
        組む経路は型に守られないので、ここが唯一の入口検査になる。
        """

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path),
            collator=PasteVolumeCollator(),
            config=PasteVolumeTrainingConfig(split_dimension="machine"),  # type: ignore[arg-type]
        )

        assert data is None
        assert reason is not None
        assert "split_dimension" in reason

    @pytest.mark.parametrize("ratio", (0.0, 1.0, -0.1, 1.5, math.nan, math.inf))
    def test_reports_a_validation_ratio_outside_the_open_unit_interval(
        self, tmp_path: Path, ratio: float
    ):
        """Session 次元の validation 比は 0 と 1 の間.

        0 だと validation が空、1 だと train が空になる。TOML から来るユーザー入力の
        境界なので、ここで理由を返す。
        """

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path, sessions=3),
            collator=PasteVolumeCollator(),
            config=_session_config(
                held_out_session="session-0", validation_ratio=ratio
            ),
        )

        assert data is None
        assert reason is not None
        assert "validation_ratio" in reason

    def test_reports_a_ratio_that_is_not_usable(self, tmp_path: Path):
        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path),
            collator=PasteVolumeCollator(),
            config=_cell_config(ratios=SplitRatios(0.9, 0.9, 0.9)),
        )

        assert data is None
        assert reason is not None

    def test_reports_a_collator_that_cannot_keep_any_view(self, tmp_path: Path):
        """詰め込み設定そのものの整合も build で見る.

        view を 1 枚も残さない設定は augmentation の範囲検証では捕まらない。
        """

        from ml.data.batch import ViewDropout

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path),
            collator=PasteVolumeCollator(
                view_dropout=ViewDropout(minimum_view_count=0)
            ),
            config=_cell_config(),
        )

        assert data is None
        assert reason is not None

    def test_reports_a_batch_size_that_cannot_hold_a_sample(self, tmp_path: Path):
        """学習データ設定そのものの整合も build で見る.

        batch size は split の生成にも augmentation にも関わらないので、ここで見なければ
        plan_epoch が呼ばれるまで気づけない。
        """

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path),
            collator=PasteVolumeCollator(),
            config=_cell_config(max_batch_size=0),
        )

        assert data is None
        assert reason is not None
        assert "max_batch_size" in reason

    def test_keeps_both_sessions_of_one_cell_in_the_same_split(self, tmp_path: Path):
        """同じ物理 cell の複数 session ぶんが同じ split に入る.

        1 session だけだと cell と sample が 1 対 1 になり、sample 単位で分けても
        差が出ない。銅板の背景が漏れるのはむしろ複数 session のときなので、そこで見る。
        """

        index = _index(tmp_path, sessions=2)
        data = _data(tmp_path, index=index)

        by_cell: dict[str, set[str]] = {}
        for split in ("train", "validation", "test"):
            for sample_id in data.sample_ids_for(split):  # type: ignore[arg-type]
                cell = index.entry_for(sample_id).cell_key
                by_cell.setdefault(cell, set()).add(split)

        assert len(index.entries) == 2 * len(CELLS)
        assert all(len(splits) == 1 for splits in by_cell.values())

    def test_reports_constraints_that_differ_from_the_index(self, tmp_path: Path):
        """読み出し側と詰め込み側で制約が違えば弾く.

        index はその制約で使えない cell を隔離している。collator が違う制約を使うと、 緩ければ
        materialize で落ち、厳しければ母集団が黙って減る。
        """

        import attrs

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path),
            collator=PasteVolumeCollator(
                constraints=attrs.evolve(CONSTRAINTS, maximum_size=256)
            ),
            config=_cell_config(),
        )

        assert data is None
        assert reason is not None
        assert "constraints" in reason

    def test_rejects_a_manifest_that_splits_a_physical_cell(self, tmp_path: Path):
        """内容が一致していても group を割っている manifest を弾く.

        sample 単位で作った manifest は fingerprint 検査を通ってしまう。cell が split
        をまたいでいないかは別に確かめる必要がある。
        """

        path = tmp_path / "split.json"
        index = _index(tmp_path, sessions=2)
        manifest, _ = SplitManifest.build(
            {
                sample_id: sample_id
                for sample_id in index.sample_groups(dimension="cell")
            },
            dataset_fingerprint=index.dataset_fingerprint,
            seed=0,
            ratios=SplitRatios(0.7, 0.15, 0.15),
            require_test=True,
        )
        assert manifest is not None
        manifest.save(path)

        data, reason = PasteVolumeTrainingData.build(
            index,
            collator=PasteVolumeCollator(),
            config=_cell_config(),
            split_manifest_path=path,
        )

        assert data is None
        assert reason is not None

    def test_reuses_a_saved_split_manifest(self, tmp_path: Path):
        """既にある manifest を読み直し、作り直さない."""

        path = tmp_path / "split.json"
        first = PasteVolumeTrainingData.build(
            _index(tmp_path),
            collator=PasteVolumeCollator(),
            config=_cell_config(),
            split_manifest_path=path,
        )[0]
        assert first is not None

        second, reason = PasteVolumeTrainingData.build(
            _index(tmp_path),
            collator=PasteVolumeCollator(),
            config=_cell_config(split_seed=999),
            split_manifest_path=path,
        )

        assert second is not None, reason
        assert second.sample_ids_for("train") == first.sample_ids_for("train")

    def test_rejects_a_manifest_built_for_another_dataset(self, tmp_path: Path):
        path = tmp_path / "split.json"
        other = _index(tmp_path / "other", machine="other")
        manifest, _ = SplitManifest.build(
            other.sample_groups(dimension="cell"),
            dataset_fingerprint=other.dataset_fingerprint,
            seed=0,
            ratios=SplitRatios(0.7, 0.15, 0.15),
            require_test=True,
        )
        assert manifest is not None
        manifest.save(path)

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path / "mine"),
            collator=PasteVolumeCollator(),
            config=_cell_config(),
            split_manifest_path=path,
        )

        assert data is None
        assert reason is not None


class TestSessionSplit:
    """Session 単位の leave-one-session-out.

    塗布量の係数 k は session ごとの 1 定数。

    cell 単位で分けると model が session を言い当てて k を憶え、見かけの精度が出る。

    session をまたいだ汎化はこの次元でしか測れない。
    """

    def test_puts_the_held_out_session_in_test_and_nowhere_else(self, tmp_path: Path):
        index = _index(tmp_path, sessions=3)
        data, reason = PasteVolumeTrainingData.build(
            index,
            collator=PasteVolumeCollator(),
            config=_session_config(held_out_session="session-0"),
        )
        assert data is not None, reason
        held_out, _ = index.resolve_session("session-0")

        assert _sessions_of(index, data.sample_ids_for("test")) == {held_out}
        assert held_out not in _sessions_of(index, data.sample_ids_for("train"))
        assert held_out not in _sessions_of(index, data.sample_ids_for("validation"))

    def test_the_same_observation_finds_a_session_that_does_leak(self, tmp_path: Path):
        """検査が働くことの自己検査.

        上は「現れない」型の assert なので、``_sessions_of`` が壊れると held-out が
        混ざっていても緑になる。held-out の sample を 1 件だけ train へ移した manifest を
        同じ関数で測り、漏れをちゃんと報告することを見る。
        """

        index = _index(tmp_path, sessions=3)
        data, reason = PasteVolumeTrainingData.build(
            index,
            collator=PasteVolumeCollator(),
            config=_session_config(held_out_session="session-0"),
        )
        assert data is not None, reason
        held_out, _ = index.resolve_session("session-0")
        manifest = data.split_manifest
        leaked = manifest.test_sample_ids[0]

        assert held_out in _sessions_of(index, (*manifest.train_sample_ids, leaked))

    def test_splits_the_remaining_sessions_into_train_and_validation(
        self, tmp_path: Path
    ):
        """Held-out 以外の session が train と validation へ分かれる.

        group は session なので、1 session が両方に現れることはない。
        """

        index = _index(tmp_path, sessions=3)
        data, reason = PasteVolumeTrainingData.build(
            index,
            collator=PasteVolumeCollator(),
            config=_session_config(held_out_session="session-0"),
        )
        assert data is not None, reason

        train = _sessions_of(index, data.sample_ids_for("train"))
        validation = _sessions_of(index, data.sample_ids_for("validation"))

        assert len(train) == 1
        assert len(validation) == 1
        assert not train & validation

    def test_covers_every_sample_exactly_once(self, tmp_path: Path):
        index = _index(tmp_path, sessions=3)
        data, reason = PasteVolumeTrainingData.build(
            index,
            collator=PasteVolumeCollator(),
            config=_session_config(held_out_session="session-1"),
        )
        assert data is not None, reason

        assigned = [
            sample_id
            for split in ("train", "validation", "test")
            for sample_id in data.sample_ids_for(split)  # type: ignore[arg-type]
        ]

        assert sorted(assigned) == sorted(entry.sample_id for entry in index.entries)

    def test_gives_every_session_its_own_fold(self, tmp_path: Path):
        """どの session も 1 度ずつ held-out になれる."""

        index = _index(tmp_path, sessions=3)
        by_fold: dict[str, set[str]] = {}
        for number in range(3):
            label = f"session-{number}"
            data, reason = PasteVolumeTrainingData.build(
                index,
                collator=PasteVolumeCollator(),
                config=_session_config(held_out_session=label),
            )
            assert data is not None, reason
            by_fold[label] = _sessions_of(index, data.sample_ids_for("test"))

        assert len({frozenset(values) for values in by_fold.values()}) == 3

    def test_exposes_the_split_dimension(self, tmp_path: Path):
        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path, sessions=3),
            collator=PasteVolumeCollator(),
            config=_session_config(held_out_session="session-0"),
        )
        assert data is not None, reason

        assert data.split_dimension == "session"

    def test_reports_a_session_split_without_a_held_out_session(self, tmp_path: Path):
        """Held-out を省くと拒否する.

        既定で 5 fold のどれかを選んでしまうと、run の記録から「どの session を外したのか」が読めなくなる。
        """

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path, sessions=3),
            collator=PasteVolumeCollator(),
            config=_session_config(),
        )

        assert data is None
        assert reason is not None
        assert "held_out_session" in reason

    def test_reports_a_held_out_session_in_the_cell_dimension(self, tmp_path: Path):
        """Cell 次元で held-out を渡すと拒否する。黙って無視しない."""

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path, sessions=3),
            collator=PasteVolumeCollator(),
            config=_cell_config(held_out_session="session-0"),
        )

        assert data is None
        assert reason is not None
        assert "held_out_session" in reason

    def test_reports_a_held_out_session_that_matches_nothing(self, tmp_path: Path):
        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path, sessions=3),
            collator=PasteVolumeCollator(),
            config=_session_config(held_out_session="session-9"),
        )

        assert data is None
        assert reason is not None
        assert "一致する session がありません" in reason

    def test_reports_a_dataset_with_too_few_sessions(self, tmp_path: Path):
        """Session が 2 本だと leave-one-session-out が成り立たない.

        held-out を除いた残りが 1 本になり、train と validation を別の session で
        埋められない。sample 単位へ fallback せず理由を返すこと。
        """

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path, sessions=2),
            collator=PasteVolumeCollator(),
            config=_session_config(held_out_session="session-0"),
        )

        assert data is None
        assert reason is not None
        assert "group が 2 個未満" in reason

    def test_forms_one_held_out_one_validation_and_three_train_from_five(
        self, tmp_path: Path
    ):
        """合成 5 session で (test, validation, train) = (1, 1, 3) になること.

        実データで同じ構成を見ているテストは ``skip_if_no_real_sessions`` の opt-in で、
        収集 session の無い環境では丸ごと skip する。CI で残る観測点をここに置く。
        """

        index = _index(tmp_path, sessions=5, cells=CELLS[:3])
        data, reason = PasteVolumeTrainingData.build(
            index,
            collator=PasteVolumeCollator(),
            config=_session_config(held_out_session="session-0"),
        )
        assert data is not None, reason

        assert _fold_sizes(index, data) == (1, 1, 3)

    def test_the_validation_count_grows_only_once_the_ratio_clears_one_session(
        self, tmp_path: Path
    ):
        """Validation の本数が比率ではなく床で決まっている範囲を示す.

        ``max(1, round(n * validation_ratio))`` なので、既定の 0.15 では n が 12 まで
        1 に張り付き、13 で初めて 2 になる。上の (1, 1, 3) が比率の結果ではないことを、
        比率が効く側と対で見る。
        """

        index = _index(tmp_path, sessions=13, cells=CELLS[:1])
        data, reason = PasteVolumeTrainingData.build(
            index,
            collator=PasteVolumeCollator(),
            config=_session_config(held_out_session="session-0"),
        )
        assert data is not None, reason

        assert _fold_sizes(index, data) == (1, 2, 10)

    def test_records_a_fold_specific_seed_in_the_manifest(self, tmp_path: Path):
        """Manifest の seed 欄が fold ごとに違うこと.

        設定の ``split_seed`` をそのまま入れると 5 fold の manifest が seed 欄で
        区別できず、別 fold のものを取り違えても値からは分からない。
        """

        index = _index(tmp_path, sessions=3)
        seeds = set()
        for number in range(3):
            data, reason = PasteVolumeTrainingData.build(
                index,
                collator=PasteVolumeCollator(),
                config=_session_config(
                    held_out_session=f"session-{number}", split_seed=0
                ),
            )
            assert data is not None, reason
            seeds.add(data.split_manifest.seed)

        assert len(seeds) == 3
        assert 0 not in seeds

    def test_rejects_a_manifest_saved_for_another_held_out_session(
        self, tmp_path: Path
    ):
        """別の fold の split.json を黙って再利用しない.

        ``SplitManifest`` は次元も held-out も持たず、session group は 1 group が
        1 split に収まっているので ``validate`` は何も言わない。通すと、要求した
        session が train に入ったまま run が進み、report まで誰も気づけない。
        ``run_directory`` の既定は fold 間で共有され得る。
        """

        path = tmp_path / "split.json"
        index = _index(tmp_path, sessions=3)
        first, reason = PasteVolumeTrainingData.build(
            index,
            collator=PasteVolumeCollator(),
            config=_session_config(held_out_session="session-0"),
            split_manifest_path=path,
        )
        assert first is not None, reason

        data, reason = PasteVolumeTrainingData.build(
            index,
            collator=PasteVolumeCollator(),
            config=_session_config(held_out_session="session-2"),
            split_manifest_path=path,
        )

        assert data is None
        assert reason is not None
        assert "held_out_session" in reason

    def test_reuses_a_manifest_saved_for_the_same_held_out_session(
        self, tmp_path: Path
    ):
        """検査が広すぎないことの対.

        同じ fold を要求した読み直しは通り、作り直さずに同じ割り当てを返す。
        """

        path = tmp_path / "split.json"
        index = _index(tmp_path, sessions=3)
        first, reason = PasteVolumeTrainingData.build(
            index,
            collator=PasteVolumeCollator(),
            config=_session_config(held_out_session="session-0"),
            split_manifest_path=path,
        )
        assert first is not None, reason

        second, reason = PasteVolumeTrainingData.build(
            index,
            collator=PasteVolumeCollator(),
            config=_session_config(held_out_session="session-0"),
            split_manifest_path=path,
        )

        assert second is not None, reason
        assert second.sample_ids_for("test") == first.sample_ids_for("test")

    def test_rejects_a_cell_manifest_read_back_in_the_session_dimension(
        self, tmp_path: Path
    ):
        """Cell group で作った manifest を session 次元で読み直すと落ちる.

        2 つの次元は直交していて、cell group はどれも全 session の sample を含む。
        ``SplitManifest.validate`` が「group が複数 split にまたがっています」で拒む
        ので、次元を切り替えずに session LOSO へ移ることはできない。
        """

        path = tmp_path / "split.json"
        index = _index(tmp_path, sessions=3)
        first, reason = PasteVolumeTrainingData.build(
            index,
            collator=PasteVolumeCollator(),
            config=_cell_config(),
            split_manifest_path=path,
        )
        assert first is not None, reason

        data, reason = PasteVolumeTrainingData.build(
            index,
            collator=PasteVolumeCollator(),
            config=_session_config(held_out_session="session-0"),
            split_manifest_path=path,
        )

        assert data is None
        assert reason is not None
        assert "複数 split" in reason


class TestPlanEpoch:
    """毎 epoch の計画が純関数であること."""

    def test_is_stable_for_the_same_split_and_epoch(self, tmp_path: Path):
        """同じ引数なら常に同じ計画.

        Trainer は checkpoint に載せた batch plan との厳密一致を要求する。
        """

        data = _data(tmp_path)

        assert data.plan_epoch(split="train", epoch=3) == data.plan_epoch(
            split="train", epoch=3
        )

    def test_does_not_read_the_global_random_state(self, tmp_path: Path):
        data = _data(tmp_path)

        before = data.plan_epoch(split="train", epoch=1)
        torch.rand(23)

        assert data.plan_epoch(split="train", epoch=1) == before

    def test_changes_between_epochs(self, tmp_path: Path):
        data = _data(tmp_path)

        plans = {data.plan_epoch(split="train", epoch=epoch) for epoch in range(8)}

        assert len(plans) > 1

    def test_covers_the_split_exactly_once(self, tmp_path: Path):
        data = _data(tmp_path)

        planned = [
            sample_id
            for batch in data.plan_epoch(split="train", epoch=0)
            for sample_id in batch
        ]

        assert sorted(planned) == sorted(data.sample_ids_for("train"))

    def test_respects_the_batch_size_limit(self, tmp_path: Path):
        data = _data(tmp_path, max_batch_size=2)

        for batch in data.plan_epoch(split="train", epoch=0):
            assert 1 <= len(batch) <= 2

    def test_groups_a_batch_by_the_augmented_size(self, tmp_path: Path):
        """同じ batch の中身が変換後の大きさで揃う.

        計画を augmentation 前の寸法で立てると、実際には 26 px と 106 px の sample が 同じ
        batch へ入る。padding が batch 内 max へ合わせるので落ちず、pixel budget が 意味を失う。
        """

        data = _data(tmp_path)

        for epoch in range(3):
            for batch in data.plan_epoch(split="train", epoch=epoch):
                buckets = {
                    _area_bucket(
                        data.collator.preprocessed_shape(
                            data.index.entry_for(sample_id), training=True, epoch=epoch
                        )
                    )
                    for sample_id in batch
                }

                assert len(buckets) == 1

    def test_shuffles_the_evaluation_plan_between_epochs(self, tmp_path: Path):
        """評価 split の計画も epoch で変わる.

        評価は augmentation を掛けないので寸法は毎 epoch 同じ。計画が変わるのは 並べ替えの種に epoch
        を混ぜているからで、そこだけを見る観測点。
        """

        data = _data(tmp_path, index=_index(tmp_path, sessions=3), max_batch_size=2)

        plans = {data.plan_epoch(split="validation", epoch=epoch) for epoch in range(8)}

        assert len(plans) > 1

    def test_counts_every_view_against_the_pixel_budget(self, tmp_path: Path):
        """画素の上限は view 数ぶん掛かる.

        view 数を数えないと 5 倍の画素を 1 sample 分として見積もり、batch が膨らむ。 幾何
        augmentation を止めて、効いているのが view 数だけになるようにする。
        """

        still = PasteVolumeCollator(
            augmentation=AugmentationRange(
                rotation_enabled=False, minimum_scale=1.0, maximum_scale=1.0
            )
        )
        data = _data(tmp_path, collator=still, max_batch_pixels=40_000)

        for batch in data.plan_epoch(split="train", epoch=0):
            assert len(batch) <= 2


class TestMaterialize:
    """計画した sample を batch へ実体化すること."""

    def test_builds_a_batch_for_the_planned_sample_ids(self, tmp_path: Path):
        data = _data(tmp_path)
        planned = data.plan_epoch(split="train", epoch=0)[0]

        batch = data.materialize(
            planned, split="train", epoch=0, training=True, device=DEVICE
        )

        assert batch.sample_ids == tuple(planned)
        assert batch.images.shape[0] == len(planned)
        assert batch.images.shape[2] == 6

    def test_keeps_every_view_when_evaluating(self, tmp_path: Path):
        data = _data(tmp_path)
        planned = data.plan_epoch(split="validation", epoch=0)[0]

        batch = data.materialize(
            planned, split="validation", epoch=0, training=False, device=DEVICE
        )

        assert batch.images.shape[1] == VIEW_COUNT
        assert batch.images.shape[3:] == (CROP_SIZE_PX + 3, CROP_SIZE_PX + 3)

    def test_rejects_a_training_flag_that_contradicts_the_split(self, tmp_path: Path):
        """``training`` と split の食い違いを落とす.

        plan_epoch は split から augmentation の有無を決めて shape を求める。
        materialize が違う判断をすると、計画時と実際の shape がずれる。padding が batch 内 max
        へ合わせるので落ちず、黙って壊れるのがいちばん悪い。
        """

        data = _data(tmp_path)
        planned = data.plan_epoch(split="validation", epoch=0)[0]

        with pytest.raises(ValueError, match="training"):
            data.materialize(
                planned, split="validation", epoch=0, training=True, device=DEVICE
            )

    def test_the_planned_shape_bounds_the_materialized_batch(self, tmp_path: Path):
        """計画した shape と実際の shape が stride の中で一致する.

        ずれると bucket と pixel budget が意味を失う。
        """

        data = _data(tmp_path)
        planned = data.plan_epoch(split="train", epoch=2)[0]

        batch = data.materialize(
            planned, split="train", epoch=2, training=True, device=DEVICE
        )

        for row, sample_id in enumerate(planned):
            planned_shape = data.collator.preprocessed_shape(
                data.index.entry_for(sample_id), training=True, epoch=2
            )
            mask = batch.valid_pixel_mask[row, 0, 0]

            # 有効画素の外接矩形が、その sample の前処理後の寸法そのもの。padding 後の
            # canvas と比べると、planned と actual が同じ stride 窓に入る限り一致して
            # しまい、最大 stride-1 px のずれを見逃す
            assert int(mask.any(dim=1).sum()) == planned_shape.height
            assert int(mask.any(dim=0).sum()) == planned_shape.width

        stride = CONSTRAINTS.stride
        assert batch.images.shape[3] % stride == 0
        assert batch.images.shape[4] % stride == 0


class TestRealSessions:
    """実収集 session を通した確認（無ければ skip）.

    ``data/paste-volume-datasets/`` は git 管理外なので CI には無い。
    """

    @skip_if_no_real_sessions
    def test_builds_and_materializes_from_the_collected_sessions(
        self, real_index: PasteVolumeSampleIndex
    ):
        assert not real_index.rejections

        data, reason = PasteVolumeTrainingData.build(
            real_index,
            collator=PasteVolumeCollator(),
            config=_cell_config(),
        )
        assert data is not None, reason

        planned = data.plan_epoch(split="train", epoch=0)[0]
        batch = data.materialize(
            planned, split="train", epoch=0, training=True, device=DEVICE
        )

        assert batch.images.ndim == 5
        assert batch.images.shape[2] == 6
        assert batch.target.shape == (len(planned), 1)

    @skip_if_no_real_sessions
    def test_forms_five_folds_of_one_held_out_one_validation_three_train(
        self, real_index: PasteVolumeSampleIndex
    ):
        """収集済み 5 session が held-out 1 / validation 1 / train 3 の 5 fold になる.

        合成 session では session 数を自由に決められる。実データの本数でしか「5 fold」は確かめられない。
        """

        values = real_index.session_values()
        assert len(values) == 5

        composition: list[tuple[int, int, int]] = []
        for value in values:
            data, reason = PasteVolumeTrainingData.build(
                real_index,
                collator=PasteVolumeCollator(),
                config=_session_config(held_out_session=value),
            )
            assert data is not None, reason
            test = _sessions_of(real_index, data.sample_ids_for("test"))
            validation = _sessions_of(real_index, data.sample_ids_for("validation"))
            train = _sessions_of(real_index, data.sample_ids_for("train"))

            assert test == {value}
            assert not train & test
            assert not validation & test
            assert not train & validation
            composition.append((len(test), len(validation), len(train)))

        assert composition == [(1, 1, 3)] * 5


# --- ここから下は PasteVolumeTask（TrainingTask 実装）の契約 ---

# 合成 batch の規模。1 batch を繰り返し学習して過学習させる
TASK_SAMPLE_COUNT = 8
TASK_IMAGE_SIZE = 32

# 別 bucket として続けて流す 2 つ目の形
OTHER_IMAGE_HEIGHT = 24
OTHER_IMAGE_WIDTH = 40

OVERFIT_STEPS = 200
OVERFIT_BLOCK_COUNT = 5

# Trainer と同じ optimizer 設定（`ml.training.loop` の AdamW と `clip_grad_norm_`）
OVERFIT_LEARNING_RATE = 1e-3
OVERFIT_WEIGHT_DECAY = 1e-4
OVERFIT_GRADIENT_CLIP_NORM = 1.0

# 学習後の平均絶対誤差に許す、初期値に対する比
OVERFIT_ERROR_RATIO = 0.1

# 合成 batch の真値の幅。実データの体積域 0.032〜0.363 uL の内側に置く
TARGET_MINIMUM_UL = 0.10
TARGET_MAXIMUM_UL = 0.30

# `reduce` が返す metric の本数。MeanSaturationDiagnostic 6 + GaussianRegressionMetrics 14
# + ZeroTargetMetrics 6。接頭辞を外すと 4 本が衝突して 22 本になる
REDUCED_METRIC_COUNT = 26

PADDING_PIXEL_NAME = "_encoder._encoder._padding_pixel"


def _model(seed: int = 0) -> MultiViewGaussianRegressor:
    torch.manual_seed(seed)
    model, error = build_paste_volume_model(PasteVolumeModelConfig())
    assert error is None
    assert model is not None
    return model


def _task_batch(
    *,
    sample_count: int = TASK_SAMPLE_COUNT,
    height: int = TASK_IMAGE_SIZE,
    width: int = TASK_IMAGE_SIZE,
    invalid_columns: int = 0,
    targets: Sequence[float] | None = None,
    seed: int = 11,
) -> PasteVolumeBatch:
    """真値と結びついた合成 batch を組む.

    post 側 3 channel に、真値の大きい sample ほど広い明領域を置く。

    振幅だけを変えても encoder 冒頭の GroupNorm が sample ごとに正規化して消すので、 空間構造で差を付けないと
    8 sample を見分けられない（実測: 振幅だけの差では 200 step 後も平均絶対誤差が初期値から動かない）。
    """

    generator = torch.Generator().manual_seed(seed)
    images = 0.1 + 0.05 * torch.rand(
        (sample_count, VIEW_COUNT, INPUT_CHANNELS, height, width), generator=generator
    )
    for index in range(sample_count):
        side = 2 + 3 * index
        images[index, :, 3:, :side, :side] = 0.9
    valid_pixel_mask = torch.ones(
        (sample_count, VIEW_COUNT, 1, height, width), dtype=torch.bool
    )
    if invalid_columns:
        valid_pixel_mask[:, :, :, :, width - invalid_columns :] = False
    values = (
        list(targets)
        if targets is not None
        else [
            TARGET_MINIMUM_UL
            + (TARGET_MAXIMUM_UL - TARGET_MINIMUM_UL) * index / (sample_count - 1)
            for index in range(sample_count)
        ]
    )
    return PasteVolumeBatch(
        images=images,
        valid_pixel_mask=valid_pixel_mask,
        conditioning=torch.full((sample_count, 1), math.log(PIXEL_PER_MM)),
        target=torch.tensor(values, dtype=torch.float32).unsqueeze(1),
        sample_weight=torch.ones((sample_count, 1)),
        sample_ids=tuple(f"synthetic-{index:03d}" for index in range(sample_count)),
    )


def _padding_pixel_gradient(model: nn.Module) -> float:
    gradient = dict(model.named_parameters())[PADDING_PIXEL_NAME].grad
    assert gradient is not None
    return float(gradient.abs().max().item())


def _overfit(
    task: PasteVolumeTask, batch: PasteVolumeBatch
) -> tuple[list[float], list[float]]:
    """1 batch を繰り返し学習し、step ごとの loss と平均絶対誤差を返す.

    optimizer は ``ml.training.loop`` と同じ AdamW・weight decay・勾配 clip にそろえる。

    学習率だけは cosine で減衰させる。負の対数尤度は残差が縮むほど ``exp(-log 分散)`` が
    大きくなって条件が悪くなるので、固定学習率では最後まで振動が残る（実測: 5 seed のうち
    2 seed で最終の平均絶対誤差が初期値の 1/10 に収まらない）。
    """

    model = task.model
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=OVERFIT_LEARNING_RATE,
        weight_decay=OVERFIT_WEIGHT_DECAY,
    )
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=OVERFIT_STEPS
    )
    losses: list[float] = []
    errors: list[float] = []
    for _step in range(OVERFIT_STEPS):
        result = task.training_step(batch)
        optimizer.zero_grad(set_to_none=True)
        result.loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), OVERFIT_GRADIENT_CLIP_NORM)
        optimizer.step()
        scheduler.step()
        losses.append(float(result.loss.detach().item()))
        errors.append(
            float((result.observation.mean - batch.target).abs().mean().item())
        )
    return losses, errors


def _block_means(values: Sequence[float]) -> list[float]:
    width = len(values) // OVERFIT_BLOCK_COUNT
    return [
        sum(values[index * width : (index + 1) * width]) / width
        for index in range(OVERFIT_BLOCK_COUNT)
    ]


class TestPasteVolumeTaskSteps:
    """1 batch を loss と観測値へ変換する契約."""

    def test_returns_a_differentiable_loss_and_a_detached_observation(self):
        task = PasteVolumeTask(_model())
        batch = _task_batch()

        result = task.training_step(batch)

        assert result.validate() is None
        assert result.sample_count == TASK_SAMPLE_COUNT
        assert result.loss.requires_grad
        assert result.observation.mean.shape == (TASK_SAMPLE_COUNT, 1)
        assert not result.observation.mean.requires_grad
        assert torch.equal(result.observation.target, batch.target)
        assert torch.equal(result.observation.sample_weight, batch.sample_weight)

    def test_shares_the_loss_of_the_ml_core(self):
        """Loss の値が ``ml.model.loss`` の 1 本と一致すること."""

        task = PasteVolumeTask(_model())
        batch = _task_batch()

        result = task.training_step(batch)

        expected = weighted_gaussian_negative_log_likelihood(
            result.observation.mean,
            result.observation.log_variance,
            batch.target,
            batch.sample_weight,
        )
        assert float(result.loss.detach().item()) == pytest.approx(
            float(expected.item())
        )

    def test_evaluates_without_building_a_graph(self):
        """評価経路が勾配を作らないこと.

        学習経路の ``loss.requires_grad`` を見る検査と対になっていて、両方が緑のときだけ
        「学習では作り、評価では作らない」が言える。
        """

        task = PasteVolumeTask(_model())

        observation = task.evaluation_step(_task_batch())

        assert not observation.mean.requires_grad
        assert not observation.log_variance.requires_grad

    def test_exposes_the_module_that_owns_the_state(self):
        model = _model()

        assert PasteVolumeTask(model).model is model


class TestPasteVolumeTaskOverfitting:
    """8 sample を覚えきれること."""

    def test_drives_the_negative_log_likelihood_and_the_error_down(self):
        batch = _task_batch()
        task = PasteVolumeTask(_model())

        losses, errors = _overfit(task, batch)

        # 初期の平均絶対誤差は mean_bias_initial 一定の予測そのもの。観測が真値と
        # 予測を突き合わせていることを、学習前の 1 点で固定する
        assert errors[0] == pytest.approx(
            float((batch.target - 0.15).abs().mean().item())
        )
        blocks = _block_means(losses)
        assert blocks == sorted(blocks, reverse=True)
        assert blocks[-1] < blocks[0]
        assert errors[-1] < errors[0] * OVERFIT_ERROR_RATIO


class TestPasteVolumeTaskVariableShapes:
    """Bucket の違う batch を続けて通せること."""

    def test_runs_two_buckets_in_a_row(self):
        task = PasteVolumeTask(_model())

        first = task.training_step(_task_batch(sample_count=4))
        second = task.training_step(
            _task_batch(
                sample_count=3,
                height=OTHER_IMAGE_HEIGHT,
                width=OTHER_IMAGE_WIDTH,
            )
        )

        assert first.validate() is None
        assert second.validate() is None
        assert first.sample_count == 4
        assert second.sample_count == 3
        assert bool(torch.isfinite(first.loss).item())
        assert bool(torch.isfinite(second.loss).item())


class TestPasteVolumeTaskPaddingGradient:
    """Padding 領域の学習可能な画素へ勾配が流れること."""

    def test_flows_gradient_into_the_learnable_padding_pixel(self):
        task = PasteVolumeTask(_model())

        task.training_step(
            _task_batch(sample_count=2, invalid_columns=4)
        ).loss.backward()

        assert _padding_pixel_gradient(task.model) > 0.0

    def test_the_same_observation_sees_no_gradient_without_padding(self):
        """自己検査。padding が生じない batch では同じ観測点が 0 になること.

        全 有効 mask では ``torch.where`` が padding 画素を 1 つも選ばないので、勾配は
        存在しても全要素 0 になる。観測が padding の有無を映していることの裏取り。
        """

        task = PasteVolumeTask(_model())

        task.training_step(_task_batch(sample_count=2)).loss.backward()

        assert _padding_pixel_gradient(task.model) == 0.0


class TestPasteVolumeTaskReduce:
    """観測値の集計."""

    def test_returns_an_empty_mapping_without_observations(self):
        assert PasteVolumeTask(_model()).reduce([]) == {}

    def test_reports_the_overall_metrics_and_both_diagnostics(self):
        task = PasteVolumeTask(_model())
        observation = task.evaluation_step(
            _task_batch(sample_count=4, targets=[0.0, 0.1, 0.2, 0.3])
        )

        values = task.reduce([observation])

        assert "relative_error_score" in values
        assert "saturated_positive_fraction" in values
        assert "zero_target_mean_absolute_error" in values

    def test_keeps_the_zero_target_metrics_apart_from_the_overall_ones(self):
        """真値 0 の集団の metric が全体の metric を上書きしないこと.

        ``ZeroTargetMetrics`` は 4 つの field 名を ``GaussianRegressionMetrics`` と
        共有する。接頭辞が外れると本数が 22 本へ減る。
        """

        task = PasteVolumeTask(_model())
        observation = task.evaluation_step(
            _task_batch(sample_count=4, targets=[0.0, 0.1, 0.2, 0.3])
        )

        values = task.reduce([observation])

        assert len(values) == REDUCED_METRIC_COUNT
        assert (
            values["mean_absolute_error"] != values["zero_target_mean_absolute_error"]
        )

    def test_reports_the_diagnostics_when_no_regression_metric_can_be_measured(self):
        """Blank だけの split でも診断が残ること.

        真値 0 の sample は ``GaussianRegressionMetrics`` に一切現れないので、空の写像を
        返すと運用者は Trainer の「monitor がありません」しか受け取れない。
        """

        task = PasteVolumeTask(_model())
        observation = task.evaluation_step(
            _task_batch(sample_count=4, targets=[0.0, 0.0, 0.0, 0.0])
        )

        values = task.reduce([observation])

        assert "relative_error_score" not in values
        assert values["zero_target_count"] == 4
        assert values["zero_target_sample_count"] == 4
        assert "zero_target_mean_absolute_error" in values

    def test_concatenates_every_observation(self):
        task = PasteVolumeTask(_model())
        observations = [
            task.evaluation_step(_task_batch(sample_count=2, targets=[0.1, 0.2])),
            task.evaluation_step(_task_batch(sample_count=3)),
        ]

        values = task.reduce(observations)

        assert values["sample_count"] == 5


class TestPasteVolumeTaskCompile:
    """``torch.compile`` を差し込む経路."""

    def test_keeps_the_state_dict_keys_after_compiling_the_forward(self):
        model = _model()
        task = PasteVolumeTask(model)

        task.compile_forward(CompileOptions())

        assert task.model is model
        assert [key for key in model.state_dict() if "_orig_mod" in key] == []

    def test_rejects_compile_options_that_do_not_validate(self):
        task = PasteVolumeTask(_model())

        with pytest.raises(ValueError, match="backend"):
            task.compile_forward(CompileOptions(backend=""))

    @skip_if_no_inductor
    def test_the_inductor_backend_matches_eager_forward_loss_and_gradient(self):
        batch = _task_batch(sample_count=2, invalid_columns=4)

        def loss(outputs: tuple[Tensor, ...]) -> Tensor:
            mean, log_variance = outputs
            return weighted_gaussian_negative_log_likelihood(
                mean, log_variance, batch.target, batch.sample_weight
            )

        result, reason = CompileParityResult.measure(
            _model(),
            (batch.images, batch.valid_pixel_mask, batch.conditioning),
            loss=loss,
            tolerances=FLOAT32_PARITY_TOLERANCES,
            options=CompileOptions(backend="inductor", fullgraph=True),
        )

        assert reason is None
        assert result is not None
        assert result.passed is True
        # 突き合わせた勾配が 1 本も無いと within_tolerance が空全称で真になる
        assert result.checked_gradient_count == sum(1 for _ in _model().parameters())
