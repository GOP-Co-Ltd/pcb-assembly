"""移行 M: machine.toml の死にキーを除き、節を「TOML のパス = 属性のパス」にそろえる.

tomlkit で書き換えるので、値の行に付いたコメントは移動先へ一緒に動く。
除くキーの行のコメントは消えるが、書き換える前の内容を ``history/machine-pre-M.toml``（ローテーションで消えない）に残す。
"""

from __future__ import annotations

from pathlib import Path

import tomlkit
from tomlkit.container import Container
from tomlkit.exceptions import ParseError
from tomlkit.items import Comment, Table

from pcbasm.atomic import write_text_atomic

NAME = "M"

# [paste_dispenser] の直下から [paste_dispenser.defaults] へ移す塗布パラメータ
_DEFAULTS_KEYS = (
    "dispense_mode",
    "line_direction",
    "ul_per_mm2",
    "paste_height",
    "prime_extra_delay",
    "bead_width_factor",
    "overlap",
    "boundary_margin",
    "micro_line_z_start",
    "micro_line_z_end",
)
# [paste_dispenser] の直下から [paste_dispenser.motion] へ移す吐出ダイナミクス
_MOTION_KEYS = (
    "max_fill_speed",
    "max_dispense_rate",
    "dispense_accel",
    "retract_amount",
    "retract_rate",
    "retract_accel_factor",
    "lift_height",
    "micro_line_settle_seconds",
    "post_dispense_settle_seconds",
)
# [paste_dispenser.pad_align] のうち、コードが読んでいないキー
_DEAD_ALIGNMENT_KEYS = (
    "tolerance",
    "roi_margin",
    "min_roi",
    "region_count",
    "min_regions",
)
# [paste_dispenser] の中で移行の対象になるキー
_DISPENSER_SOURCES = (
    "air_pump_enabled",
    "pad_align",
    "flow_calibration",
    *_DEFAULTS_KEYS,
    *_MOTION_KEYS,
)
# [klipper] の接続先はコードの既定値を撤去したので、移行するファイルに書かれていなければこれまでの既定値を明示する
_KLIPPER_DEFAULTS = {"host": "localhost", "port": 7125}


class MigrationError(Exception):
    """移行できない（TOML として読めない・途中まで移行済み）。元のファイルは変えない."""


def migrate_machine_toml(path: Path) -> list[str]:
    """machine.toml に移行 M を当てて書き戻し、行った変更を返す（移行済みなら空で、何も書かない）.

    書き換える前の内容を ``history/machine-pre-M.toml`` に残す（既にあれば上書きしない）。

    Raises:
        MigrationError: 移行できない場合（ファイルには触れない）
    """
    original = path.read_text()
    migrated, changes = migrate_text(original)
    if changes:
        snapshot = path.parent / "history" / f"machine-pre-{NAME}.toml"
        # 2 回目に当たっても（古い世代を復元した場合など）、最初の移行の戻り先を残す
        if not snapshot.exists():
            snapshot.parent.mkdir(exist_ok=True)
            write_text_atomic(snapshot, original)
        write_text_atomic(path, migrated)
    return changes


def migrate_text(text: str) -> tuple[str, list[str]]:
    """machine.toml の内容に移行 M を当てた内容と、行った変更を返す.

    Raises:
        MigrationError: 移行できない場合
    """
    try:
        doc = tomlkit.parse(text)
    except ParseError as exc:
        raise MigrationError(f"TOML として読めません: {exc}") from exc
    changes = _migrate(doc)
    if not changes:
        return text, changes
    # 先頭の machine_type を除いた後ろの空行と、末尾へ移した表の後ろの空行を落とす
    return tomlkit.dumps(doc).strip("\n") + "\n", changes


def _migrate(doc: Container) -> list[str]:
    changes: list[str] = []
    for key in ("machine_type", "xy_calibration"):
        if key in doc:
            del doc[key]
            changes.append(f"{key} を削除")

    _migrate_dispenser(doc, changes)
    # 新しい形のファイルで書き忘れた接続先は補わない（設定エラーにする）
    if changes:
        _make_klipper_explicit(doc, changes)
    return changes


def _make_klipper_explicit(doc: Container, changes: list[str]) -> None:
    klipper = _table(doc, "klipper")
    if klipper is None:
        klipper = tomlkit.table()
        doc.add("klipper", klipper)
    for key, value in _KLIPPER_DEFAULTS.items():
        if key not in klipper:
            klipper.add(key, value)
            changes.append(f"klipper.{key} = {value!r} を明示")


def _migrate_dispenser(doc: Container, changes: list[str]) -> None:
    if "paste_dispenser" not in doc:
        return
    dispenser = doc.item("paste_dispenser")
    if not any(key in dispenser for key in _DISPENSER_SOURCES):
        return
    if not isinstance(dispenser, Table):
        # 下位表が別の節をはさんで書かれていると、tomlkit は 1 つの表として書き換えられない
        raise MigrationError(
            "[paste_dispenser] の下位表がほかの節をはさんで書かれているので、"
            "自動では移行できません"
        )
    if "air_pump_enabled" in dispenser:
        dispenser.remove("air_pump_enabled")
        changes.append("paste_dispenser.air_pump_enabled を削除")

    for name, keys in (("defaults", _DEFAULTS_KEYS), ("motion", _MOTION_KEYS)):
        present = [key for key in keys if key in dispenser]
        if not present:
            continue
        if name in dispenser:
            raise MigrationError(
                f"[paste_dispenser] の {', '.join(present)} と "
                f"[paste_dispenser.{name}] が両方あります（途中まで移行済み）"
            )
        table = tomlkit.table()
        for key in present:
            table.add(key, dispenser.item(key))
            dispenser.remove(key)
        table.add(tomlkit.nl())
        dispenser.add(name, table)
        changes.append(
            f"paste_dispenser の {', '.join(present)} を [paste_dispenser.{name}] へ移動"
        )

    pad_align = _table(dispenser, "pad_align")
    if pad_align is not None:
        if "alignment" in doc:
            raise MigrationError(
                "[paste_dispenser.pad_align] と [alignment] が両方あります（途中まで移行済み）"
            )
        for key in _DEAD_ALIGNMENT_KEYS:
            if key in pad_align:
                pad_align.remove(key)
                changes.append(f"paste_dispenser.pad_align.{key} を削除")
        doc.add("alignment", _detach(dispenser, "pad_align"))
        changes.append("[paste_dispenser.pad_align] を [alignment] へ移動")

    if _table(dispenser, "flow_calibration") is not None:
        if "volume_correction" in dispenser:
            raise MigrationError(
                "[paste_dispenser.flow_calibration] と "
                "[paste_dispenser.volume_correction] が両方あります（途中まで移行済み）"
            )
        dispenser.add("volume_correction", _detach(dispenser, "flow_calibration"))
        changes.append(
            "[paste_dispenser.flow_calibration] を [paste_dispenser.volume_correction] へ移動"
        )


def _table(container: Container | Table, key: str) -> Table | None:
    if key not in container:
        return None
    item = container.item(key)
    if not isinstance(item, Table):
        raise MigrationError(f"{key} が表ではありません")
    return item


def _detach(parent: Table, key: str) -> Table:
    """``parent`` から表を外して返す.

    表の末尾の空行とコメントは次の表の見出しの前に書かれたものなので、表から外す。
    コメントは直前の表の末尾へ移して元の位置に残す（空行は直前の表が自分の末尾に持っている）。
    """
    table = parent.item(key)
    assert isinstance(table, Table)
    body = parent.value.body
    index = next(i for i, (k, _) in enumerate(body) if k is not None and k.key == key)
    previous = next(
        (item for _, item in reversed(body[:index]) if isinstance(item, Table)), None
    )
    own = table.value.body
    last_key = max((i for i, (k, _) in enumerate(own) if k is not None), default=-1)
    first_comment = next(
        (i for i in range(last_key + 1, len(own)) if isinstance(own[i][1], Comment)),
        len(own),
    )
    if previous is not None:
        for _, item in own[first_comment:]:
            previous.append(None, item)
        del own[last_key + 1 :]
    parent.remove(key)
    return table
