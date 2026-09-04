"""Group を最小単位にした split と、その永続化・検証.

無作為な sample 単位 split は禁止する。

同じ収集 session の sample が train と test に分かれると汎化性能を過大評価する。

split は group（session など）を不可分の単位として扱う。

結果は manifest として保存し、fingerprint 照合のうえ再利用する。
"""

from __future__ import annotations

import hashlib
import math
import random
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import attrs

from ml.artifact.document import DocumentKind
from ml.serialization import make_strict_converter

type SplitName = Literal["train", "validation", "test"]

SPLIT_NAMES: tuple[SplitName, ...] = ("train", "validation", "test")
SPLIT_MANIFEST_DOCUMENT = DocumentKind(kind="ml-split-manifest", schema_version=1)

_CONVERTER = make_strict_converter()


@attrs.frozen
class SplitRatios:
    """Train / validation / test の配分."""

    train: float
    validation: float
    test: float

    def validate(self) -> str | None:
        """配分の整合を検証する."""

        for name in SPLIT_NAMES:
            value: float = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                return f"{name} ratio は 0 以上の有限値が必要です: {value}"
        total = self.train + self.validation + self.test
        if not math.isclose(total, 1.0, abs_tol=1e-9):
            return f"train/validation/test ratio の合計は 1 が必要です: {total}"
        return None


@attrs.frozen
class SplitManifest:
    """1 個の dataset に対して 1 度だけ作る split の記録."""

    dataset_fingerprint: str
    seed: int
    train_sample_ids: tuple[str, ...]
    validation_sample_ids: tuple[str, ...]
    test_sample_ids: tuple[str, ...]

    def sample_ids_for(self, split: SplitName) -> tuple[str, ...]:
        """指定した split に属する sample ID を返す."""

        match split:
            case "train":
                return self.train_sample_ids
            case "validation":
                return self.validation_sample_ids
            case "test":
                return self.test_sample_ids

    def validate(
        self,
        sample_groups: Mapping[str, str],
        *,
        dataset_fingerprint: str,
    ) -> str | None:
        """既存の split を再利用してよいかを検証する."""

        if self.dataset_fingerprint != dataset_fingerprint:
            return _fingerprint_mismatch(self, dataset_fingerprint)
        assigned = [
            sample_id
            for split in SPLIT_NAMES
            for sample_id in self.sample_ids_for(split)
        ]
        if duplicated := sorted(
            sample_id for sample_id, count in Counter(assigned).items() if count > 1
        ):
            return f"同じ sample が複数 split にあります: {duplicated}"
        missing = sorted(set(sample_groups) - set(assigned))
        unknown = sorted(set(assigned) - set(sample_groups))
        if missing or unknown:
            return (
                "split manifest が dataset と一致しません"
                f"（不足: {missing}、未知: {unknown}）"
            )
        split_of_group: dict[str, SplitName] = {}
        for split in SPLIT_NAMES:
            for sample_id in self.sample_ids_for(split):
                group = sample_groups[sample_id]
                if split_of_group.setdefault(group, split) != split:
                    return f"group が複数 split にまたがっています: {group}"
        return None


@attrs.frozen
class LeaveOneGroupOutFold:
    """1 個の値を held-out にした交差検証 fold."""

    held_out_value: str
    held_out_group_ids: tuple[str, ...]
    train_group_ids: tuple[str, ...]
    validation_group_ids: tuple[str, ...]
    seed: int


@attrs.frozen
class LeaveOneGroupOutPlan:
    """ある次元での交差検証計画.

    対象の値が 2 種類未満などで評価できない場合は、sample 単位 split へ
    fallback せず ``available=False`` と理由を返す。
    """

    available: bool
    reason: str | None
    folds: tuple[LeaveOneGroupOutFold, ...]


def build_split_manifest(
    sample_groups: Mapping[str, str],
    *,
    dataset_fingerprint: str,
    seed: int,
    ratios: SplitRatios,
    require_test: bool,
) -> tuple[SplitManifest | None, str | None]:
    """Group を不可分の単位として split を 1 度だけ生成する.

    ``sample_groups`` は sample ID から group ID への対応。並び順に依存せず、
    同じ ``seed`` なら同じ結果になる。
    """

    if error := ratios.validate():
        return None, error
    groups = sorted(set(sample_groups.values()))
    required = 3 if require_test else 2
    if len(groups) < required:
        return None, (
            f"split には最低 {required} 個の group が必要です: {len(groups)} 個"
        )

    shuffled = list(groups)
    random.Random(seed).shuffle(shuffled)
    validation_count = max(1, round(len(groups) * ratios.validation))
    test_count = max(1, round(len(groups) * ratios.test)) if require_test else 0
    if validation_count + test_count >= len(groups):
        validation_count = 1
        test_count = 1 if require_test else 0
    train_count = len(groups) - validation_count - test_count

    boundaries = (train_count, train_count + validation_count)
    assigned = {
        "train": frozenset(shuffled[: boundaries[0]]),
        "validation": frozenset(shuffled[boundaries[0] : boundaries[1]]),
        "test": frozenset(shuffled[boundaries[1] :]),
    }
    return (
        SplitManifest(
            dataset_fingerprint=dataset_fingerprint,
            seed=seed,
            train_sample_ids=_sample_ids_in(sample_groups, assigned["train"]),
            validation_sample_ids=_sample_ids_in(sample_groups, assigned["validation"]),
            test_sample_ids=_sample_ids_in(sample_groups, assigned["test"]),
        ),
        None,
    )


def save_split_manifest(path: Path, manifest: SplitManifest) -> None:
    """Split manifest を atomic に書き出す."""

    SPLIT_MANIFEST_DOCUMENT.save(path, manifest, converter=_CONVERTER)


def load_split_manifest(
    path: Path, *, dataset_fingerprint: str
) -> tuple[SplitManifest | None, str | None]:
    """Split manifest を読み、dataset fingerprint の一致を要求する."""

    manifest, error = SPLIT_MANIFEST_DOCUMENT.load(
        path, SplitManifest, converter=_CONVERTER
    )
    if manifest is None:
        return None, error
    if manifest.dataset_fingerprint != dataset_fingerprint:
        return None, _fingerprint_mismatch(manifest, dataset_fingerprint)
    return manifest, None


def build_leave_one_group_out_plan(
    group_values: Mapping[str, str],
    *,
    dimension: str,
    seed: int,
    validation_ratio: float = 0.15,
) -> LeaveOneGroupOutPlan:
    """次元の値ごとに 1 fold を作る交差検証計画を返す.

    ``group_values`` は group ID からその次元の値（machine ID など）への対応。
    held-out した残りを train と validation へ分けるが、group は分割しない。
    """

    values = sorted(set(group_values.values()))
    if len(values) < 2:
        return LeaveOneGroupOutPlan(
            available=False,
            reason=(
                f"{dimension} の値が 2 種類未満のため "
                "leave-one-group-out 評価はできません"
            ),
            folds=(),
        )

    folds: list[LeaveOneGroupOutFold] = []
    unavailable: list[str] = []
    for value in values:
        held_out = sorted(
            group for group, actual in group_values.items() if actual == value
        )
        remaining = sorted(set(group_values) - set(held_out))
        if len(remaining) < 2:
            unavailable.append(
                f"{value!r} を held-out にすると train/validation 用の group が 2 個未満です"
            )
            continue
        fold_seed = _derived_seed(f"{seed}:{dimension}:{value}")
        shuffled = list(remaining)
        random.Random(fold_seed).shuffle(shuffled)
        validation_count = min(
            max(1, round(len(shuffled) * validation_ratio)), len(shuffled) - 1
        )
        folds.append(
            LeaveOneGroupOutFold(
                held_out_value=value,
                held_out_group_ids=tuple(held_out),
                train_group_ids=tuple(sorted(shuffled[validation_count:])),
                validation_group_ids=tuple(sorted(shuffled[:validation_count])),
                seed=fold_seed,
            )
        )
    if unavailable:
        return LeaveOneGroupOutPlan(
            available=False, reason="; ".join(unavailable), folds=()
        )
    return LeaveOneGroupOutPlan(available=True, reason=None, folds=tuple(folds))


def _fingerprint_mismatch(manifest: SplitManifest, expected: str) -> str:
    return (
        "split manifest の dataset fingerprint が一致しません: "
        f"{manifest.dataset_fingerprint!r}（期待値 {expected!r}）"
    )


def _sample_ids_in(
    sample_groups: Mapping[str, str], groups: frozenset[str]
) -> tuple[str, ...]:
    return tuple(
        sorted(
            sample_id for sample_id, group in sample_groups.items() if group in groups
        )
    )


def _derived_seed(material: str) -> int:
    return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big")


__all__ = [
    "SPLIT_MANIFEST_DOCUMENT",
    "SPLIT_NAMES",
    "LeaveOneGroupOutFold",
    "LeaveOneGroupOutPlan",
    "SplitManifest",
    "SplitName",
    "SplitRatios",
    "build_leave_one_group_out_plan",
    "build_split_manifest",
    "load_split_manifest",
    "save_split_manifest",
]
