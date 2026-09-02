"""Dataset-independent group split and balancing mechanics."""

from __future__ import annotations

import hashlib
import math
import random
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TypeVar

import attrs

T = TypeVar("T")


@attrs.frozen
class GroupSplit:
    train_group_ids: tuple[str, ...]
    validation_group_ids: tuple[str, ...]
    test_group_ids: tuple[str, ...]


@attrs.frozen
class LeaveOneGroupOutFold:
    held_out_group: str
    train_group_values: tuple[str, ...]
    train_group_ids: tuple[str, ...]
    validation_group_ids: tuple[str, ...]
    held_out_group_ids: tuple[str, ...]
    seed: int


@attrs.frozen
class LeaveOneGroupOutPlan:
    available: bool
    reason: str | None
    folds: tuple[LeaveOneGroupOutFold, ...]


def split_groups(
    group_ids: Iterable[str],
    *,
    seed: int,
    train_ratio: float,
    validation_ratio: float,
    test_ratio: float,
    require_test: bool,
) -> GroupSplit:
    """Assign each complete group to one deterministic split."""

    ratios = (train_ratio, validation_ratio, test_ratio)
    if any(
        not math.isfinite(value) or value < 0 for value in ratios
    ) or not math.isclose(sum(ratios), 1.0, abs_tol=1e-9):
        raise ValueError("train/validation/test ratioの合計は1が必要です")
    groups = sorted(set(group_ids))
    random.Random(seed).shuffle(groups)
    validation_count = max(1, round(len(groups) * validation_ratio))
    test_count = max(1, round(len(groups) * test_ratio)) if require_test else 0
    if validation_count + test_count >= len(groups):
        validation_count = 1
        test_count = 1 if require_test else 0
    train_count = len(groups) - validation_count - test_count
    if train_count < 1:
        raise ValueError("train groupを1個以上確保できません")
    return GroupSplit(
        train_group_ids=tuple(sorted(groups[:train_count])),
        validation_group_ids=tuple(
            sorted(groups[train_count : train_count + validation_count])
        ),
        test_group_ids=tuple(sorted(groups[train_count + validation_count :])),
    )


def select_group_balanced(
    items: Sequence[T],
    item_ids: Iterable[str],
    *,
    item_id_of: Callable[[T], str],
    group_id_of: Callable[[T], str],
    limit: int,
    seed: int,
) -> tuple[T, ...]:
    """Select deterministically by round-robin across complete groups."""

    if limit < 1:
        raise ValueError("sample limitは正の整数が必要です")
    by_id = {item_id_of(item): item for item in items}
    if len(by_id) != len(items):
        raise ValueError("sample_idが重複しています")
    requested = tuple(item_ids)
    if unknown := set(requested) - set(by_id):
        raise ValueError(f"未知sample_idがあります: {sorted(unknown)}")
    by_group: dict[str, list[T]] = defaultdict(list)
    for item_id in requested:
        item = by_id[item_id]
        by_group[group_id_of(item)].append(item)
    generator = random.Random(seed)
    for group_items in by_group.values():
        group_items.sort(key=item_id_of)
        generator.shuffle(group_items)
    selected: list[T] = []
    groups = sorted(by_group)
    target_count = min(limit, len(requested))
    while len(selected) < target_count:
        for group_id in groups:
            if not by_group[group_id]:
                continue
            selected.append(by_group[group_id].pop())
            if len(selected) == target_count:
                break
    return tuple(selected)


def build_leave_one_group_out_plan(
    group_values_by_id: Mapping[str, str],
    *,
    dimension: str,
    seed: int,
    validation_ratio: float = 0.15,
) -> LeaveOneGroupOutPlan:
    """Plan held-out value folds while keeping each group ID indivisible."""

    groups = sorted(set(group_values_by_id.values()))
    if len(groups) < 2:
        return LeaveOneGroupOutPlan(
            False,
            f"{dimension} groupが2種類未満のためleave-one-group-out評価不能です",
            (),
        )
    folds: list[LeaveOneGroupOutFold] = []
    unavailable_reasons: list[str] = []
    all_group_ids = set(group_values_by_id)
    for group in groups:
        held_out_group_ids = sorted(
            group_id for group_id, value in group_values_by_id.items() if value == group
        )
        remaining_group_ids = sorted(all_group_ids - set(held_out_group_ids))
        if len(remaining_group_ids) < 2:
            unavailable_reasons.append(
                f"held-out {group!r}後にtrain/validation用sessionが2個未満です"
            )
            continue
        fold_seed = int.from_bytes(
            hashlib.sha256(f"{seed}:{dimension}:{group}".encode()).digest()[:8],
            "big",
        )
        random.Random(fold_seed).shuffle(remaining_group_ids)
        validation_count = max(1, round(len(remaining_group_ids) * validation_ratio))
        if validation_count >= len(remaining_group_ids):
            validation_count = 1
        validation_group_ids = remaining_group_ids[:validation_count]
        train_group_ids = remaining_group_ids[validation_count:]
        folds.append(
            LeaveOneGroupOutFold(
                held_out_group=group,
                train_group_values=tuple(value for value in groups if value != group),
                train_group_ids=tuple(sorted(train_group_ids)),
                validation_group_ids=tuple(sorted(validation_group_ids)),
                held_out_group_ids=tuple(held_out_group_ids),
                seed=fold_seed,
            )
        )
    if unavailable_reasons:
        return LeaveOneGroupOutPlan(False, "; ".join(unavailable_reasons), ())
    return LeaveOneGroupOutPlan(True, None, tuple(folds))


__all__ = [
    "GroupSplit",
    "LeaveOneGroupOutFold",
    "LeaveOneGroupOutPlan",
    "build_leave_one_group_out_plan",
    "select_group_balanced",
    "split_groups",
]
