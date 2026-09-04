"""Group 単位の split と、その永続化・検証の公開契約."""

from pathlib import Path

import pytest

from ml.data.split import (
    SplitManifest,
    SplitRatios,
    build_leave_one_group_out_plan,
    build_split_manifest,
    load_split_manifest,
    save_split_manifest,
)

FINGERPRINT = "sha256:0123456789abcdef"
RATIOS = SplitRatios(train=0.7, validation=0.15, test=0.15)


def _sample_groups(group_count: int, samples_per_group: int = 3) -> dict[str, str]:
    return {
        f"session-{group:02d}.pad-{index}": f"session-{group:02d}"
        for group in range(group_count)
        for index in range(samples_per_group)
    }


def _manifest(sample_groups: dict[str, str], **overrides) -> SplitManifest:
    manifest, error = build_split_manifest(
        sample_groups,
        **{
            "dataset_fingerprint": FINGERPRINT,
            "seed": 42,
            "ratios": RATIOS,
            "require_test": True,
        }
        | overrides,
    )

    assert error is None
    assert manifest is not None
    return manifest


class TestSplitRatios:
    """Train / validation / test の配分."""

    def test_accepts_a_normalized_triple(self):
        assert RATIOS.validate() is None

    @pytest.mark.parametrize(
        ("ratios", "expected"),
        [
            (SplitRatios(train=0.7, validation=0.15, test=0.10), "合計"),
            (SplitRatios(train=-0.1, validation=0.6, test=0.5), "train"),
            (SplitRatios(train=float("nan"), validation=0.5, test=0.5), "train"),
        ],
    )
    def test_rejects_an_unusable_triple(self, ratios: SplitRatios, expected: str):
        error = ratios.validate()

        assert error is not None
        assert expected in error


class TestBuildSplitManifest:
    """Group を最小単位に分け、leakage を作らない."""

    def test_covers_every_sample_exactly_once(self):
        sample_groups = _sample_groups(10)

        manifest = _manifest(sample_groups)

        assigned = (
            manifest.train_sample_ids
            + manifest.validation_sample_ids
            + manifest.test_sample_ids
        )
        assert sorted(assigned) == sorted(sample_groups)

    def test_never_splits_a_group_across_two_splits(self):
        sample_groups = _sample_groups(10)

        manifest = _manifest(sample_groups)

        for split in ("train", "validation", "test"):
            ids = manifest.sample_ids_for(split)
            groups = {sample_groups[sample_id] for sample_id in ids}
            others = {
                sample_groups[sample_id]
                for other in ("train", "validation", "test")
                if other != split
                for sample_id in manifest.sample_ids_for(other)
            }
            assert groups & others == set()

    def test_is_reproducible_for_the_same_seed(self):
        sample_groups = _sample_groups(10)

        assert _manifest(sample_groups) == _manifest(sample_groups)

    def test_differs_for_a_different_seed(self):
        sample_groups = _sample_groups(12)

        assert _manifest(sample_groups, seed=1) != _manifest(sample_groups, seed=2)

    def test_is_independent_of_the_mapping_order(self):
        sample_groups = _sample_groups(10)
        reordered = dict(reversed(list(sample_groups.items())))

        assert _manifest(sample_groups) == _manifest(reordered)

    def test_leaves_test_empty_when_not_required(self):
        manifest = _manifest(_sample_groups(4), require_test=False)

        assert manifest.test_sample_ids == ()
        assert manifest.validation_sample_ids != ()

    def test_every_split_receives_at_least_one_group(self):
        manifest = _manifest(_sample_groups(3))

        assert manifest.train_sample_ids != ()
        assert manifest.validation_sample_ids != ()
        assert manifest.test_sample_ids != ()

    @pytest.mark.parametrize(
        ("group_count", "require_test", "expected"),
        [(2, True, "group"), (1, False, "group"), (0, True, "group")],
    )
    def test_reports_when_there_are_too_few_groups(
        self, group_count: int, require_test: bool, expected: str
    ):
        manifest, error = build_split_manifest(
            _sample_groups(group_count),
            dataset_fingerprint=FINGERPRINT,
            seed=42,
            ratios=RATIOS,
            require_test=require_test,
        )

        assert manifest is None
        assert error is not None
        assert expected in error


class TestValidateSplitManifest:
    """既存 manifest を再利用する前の照合."""

    def test_accepts_a_manifest_built_from_the_same_dataset(self):
        sample_groups = _sample_groups(10)
        manifest = _manifest(sample_groups)

        error = manifest.validate(sample_groups, dataset_fingerprint=FINGERPRINT)

        assert error is None

    def test_rejects_a_different_dataset_fingerprint(self):
        sample_groups = _sample_groups(10)
        manifest = _manifest(sample_groups)

        error = manifest.validate(sample_groups, dataset_fingerprint="sha256:other")

        assert error is not None
        assert "fingerprint" in error

    def test_rejects_a_manifest_that_misses_a_sample(self):
        sample_groups = _sample_groups(10)
        manifest = _manifest(sample_groups)
        extended = sample_groups | {"session-99.pad-0": "session-99"}

        error = manifest.validate(extended, dataset_fingerprint=FINGERPRINT)

        assert error is not None
        assert "session-99.pad-0" in error

    def test_rejects_a_group_that_spans_two_splits(self):
        sample_groups = {"a.1": "a", "a.2": "a", "b.1": "b", "c.1": "c"}
        leaking = SplitManifest(
            dataset_fingerprint=FINGERPRINT,
            seed=1,
            train_sample_ids=("a.1", "b.1"),
            validation_sample_ids=("a.2",),
            test_sample_ids=("c.1",),
        )

        error = leaking.validate(sample_groups, dataset_fingerprint=FINGERPRINT)

        assert error is not None
        assert "a" in error

    def test_rejects_a_sample_listed_in_two_splits(self):
        sample_groups = {"a.1": "a", "b.1": "b", "c.1": "c"}
        duplicated = SplitManifest(
            dataset_fingerprint=FINGERPRINT,
            seed=1,
            train_sample_ids=("a.1", "b.1"),
            validation_sample_ids=("b.1",),
            test_sample_ids=("c.1",),
        )

        error = duplicated.validate(sample_groups, dataset_fingerprint=FINGERPRINT)

        assert error is not None
        assert "b.1" in error


class TestSplitManifestFile:
    """Split manifest の保存と読み戻し."""

    def test_round_trips_through_a_file(self, tmp_path: Path):
        manifest = _manifest(_sample_groups(10))
        path = tmp_path / "split.json"

        save_split_manifest(path, manifest)
        loaded, error = load_split_manifest(path, dataset_fingerprint=FINGERPRINT)

        assert error is None
        assert loaded == manifest

    def test_rejects_a_manifest_of_another_dataset(self, tmp_path: Path):
        path = tmp_path / "split.json"
        save_split_manifest(path, _manifest(_sample_groups(10)))

        loaded, error = load_split_manifest(path, dataset_fingerprint="sha256:other")

        assert loaded is None
        assert error is not None
        assert "fingerprint" in error

    def test_reports_a_missing_file(self, tmp_path: Path):
        loaded, error = load_split_manifest(
            tmp_path / "absent.json", dataset_fingerprint=FINGERPRINT
        )

        assert loaded is None
        assert error is not None
        assert "absent.json" in error


class TestLeaveOneGroupOutPlan:
    """次元ごとに 1 値を held-out にする交差検証計画."""

    def test_builds_one_fold_per_distinct_value(self):
        group_values = {
            "session-1": "machine-a",
            "session-2": "machine-a",
            "session-3": "machine-b",
            "session-4": "machine-c",
        }

        plan = build_leave_one_group_out_plan(group_values, dimension="machine", seed=1)

        assert plan.available
        assert plan.reason is None
        assert [fold.held_out_value for fold in plan.folds] == [
            "machine-a",
            "machine-b",
            "machine-c",
        ]

    def test_a_fold_holds_out_every_group_of_its_value(self):
        group_values = {
            "session-1": "machine-a",
            "session-2": "machine-a",
            "session-3": "machine-b",
            "session-4": "machine-c",
        }

        plan = build_leave_one_group_out_plan(group_values, dimension="machine", seed=1)

        held_out = next(
            fold for fold in plan.folds if fold.held_out_value == "machine-a"
        )
        assert held_out.held_out_group_ids == ("session-1", "session-2")
        assert set(held_out.train_group_ids) | set(held_out.validation_group_ids) == {
            "session-3",
            "session-4",
        }

    def test_is_reproducible_for_the_same_seed(self):
        group_values = {f"session-{index}": f"lot-{index % 4}" for index in range(12)}

        first = build_leave_one_group_out_plan(group_values, dimension="lot", seed=3)
        second = build_leave_one_group_out_plan(group_values, dimension="lot", seed=3)

        assert first == second

    def test_reports_evaluation_is_impossible_with_a_single_value(self):
        plan = build_leave_one_group_out_plan(
            {"session-1": "machine-a", "session-2": "machine-a"},
            dimension="machine",
            seed=1,
        )

        assert not plan.available
        assert plan.reason is not None
        assert "machine" in plan.reason
        assert plan.folds == ()

    def test_reports_when_a_fold_cannot_keep_train_and_validation(self):
        plan = build_leave_one_group_out_plan(
            {"session-1": "machine-a", "session-2": "machine-b"},
            dimension="machine",
            seed=1,
        )

        assert not plan.available
        assert plan.reason is not None
        assert plan.folds == ()
