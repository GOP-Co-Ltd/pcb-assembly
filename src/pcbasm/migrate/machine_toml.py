"""machine.toml の移行（ステップ M → N の順に当てる）.

- M: 死にキーを除き、節を「TOML のパス = 属性のパス」にそろえる。
  ``[tact]`` の速度と加速度も除く（所要時間の予測は printer.cfg の値を読む。``setup_sec`` は残す）。
- N: ``[paste_dispenser]`` と ``[tact]`` を ``[solder]`` の階層へ作り直す（:data:`SOLDER_KEYS` と :data:`SOLDER_TABLES`）。

各ステップは冪等で、移行済みのファイルには何もしない。
tomlkit で書き換えるので、値の行に付いたコメントは移動先へ一緒に動く。
除くキーの行のコメントは消えるが、ステップごとに書き換える前の内容を ``history/machine-pre-<ステップ>.toml``（ローテーションで消えない）に残す。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import tomlkit
from tomlkit.container import Container
from tomlkit.exceptions import ParseError
from tomlkit.items import Comment, Item, Key, Table, Whitespace

from pcbasm.atomic import write_text_atomic

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
# [tact] のうち、printer.cfg から読むようにした速度と加速度
_TACT_SPEED_KEYS = ("travel_speed", "travel_accel", "z_speed", "z_accel")
# [klipper] の接続先はコードの既定値を撤去したので、移行するファイルに書かれていなければこれまでの既定値を明示する
_KLIPPER_DEFAULTS = {"host": "localhost", "port": 7125}

# 移行 N: [paste_dispenser] と [paste_dispenser.motion] のキー → [solder] の下の表とキー（表の "" は [solder] の直下）
SOLDER_KEYS: dict[str, dict[str, tuple[str, str]]] = {
    "": {
        "solder_paste_density": ("", "paste_density"),
        "initial_purge_ul": ("", "initial_purge_ul"),
        "rotations_per_ul": ("dispenser", "rotations_per_ul"),
        "nozzle_diameter": ("nozzle", "diameter"),
        "nozzle_outer_diameter": ("nozzle", "outer_diameter"),
        "auto_line_aspect_ratio": ("auto", "line_aspect_ratio"),
        "auto_area_short_side_factor": ("auto", "area_short_side_factor"),
    },
    "motion": {
        "max_dispense_rate": ("dispenser", "max_dispense_rate"),
        "dispense_accel": ("dispenser", "dispense_accel"),
        "retract_amount": ("dispenser", "retract_amount"),
        "retract_rate": ("dispenser", "retract_rate"),
        "retract_accel_factor": ("dispenser", "retract_accel_factor"),
        "max_fill_speed": ("motion", "max_fill_speed"),
        "lift_height": ("motion", "lift_height"),
        "micro_line_settle_seconds": ("motion", "micro_line_settle_seconds"),
        "post_dispense_settle_seconds": ("motion", "post_dispense_settle_seconds"),
    },
}
# 移行 N: 中身ごと移す [paste_dispenser] の下位表 → [solder] の下の表
SOLDER_TABLES = {
    "toolhead": "nozzle.offset",
    "nozzle_cap": "nozzle.cap",
    "nozzle_clean": "nozzle.clean",
    "defaults": "defaults",
    "volume_correction": "volume_correction",
}
# [solder] の下の表の並び（[tact] は最後の tact）
_SOLDER_LAYOUT = (
    "dispenser",
    "nozzle",
    "nozzle.offset",
    "nozzle.cap",
    "nozzle.clean",
    "motion",
    "defaults",
    "auto",
    "volume_correction",
)


class MigrationError(Exception):
    """移行できない（TOML として読めない・途中まで移行済み）。元のファイルは変えない."""


def migrate_machine_toml(path: Path) -> dict[str, list[str]]:
    """machine.toml に移行を当てて書き戻し、当てたステップと行った変更を返す（移行済みなら空で、何も書かない）.

    当てたステップごとに、その手前の内容を ``history/machine-pre-<ステップ>.toml`` に残す（既にあれば上書きしない）。

    Raises:
        MigrationError: 移行できない場合（ファイルには触れない）
    """
    migrated, applied = _apply(path.read_text())
    for name, before, _ in applied:
        snapshot = path.parent / "history" / f"machine-pre-{name}.toml"
        # 2 回目に当たっても（古い世代を復元した場合など）、最初の移行の戻り先を残す
        if not snapshot.exists():
            snapshot.parent.mkdir(exist_ok=True)
            write_text_atomic(snapshot, before)
    if applied:
        write_text_atomic(path, migrated)
    return {name: changes for name, _, changes in applied}


def migrate_text(text: str) -> tuple[str, dict[str, list[str]]]:
    """machine.toml の内容に移行を当てた内容と、当てたステップと行った変更を返す.

    Raises:
        MigrationError: 移行できない場合
    """
    migrated, applied = _apply(text)
    return migrated, {name: changes for name, _, changes in applied}


def _apply(text: str) -> tuple[str, list[tuple[str, str, list[str]]]]:
    """ステップを順に当て、移行後の内容と、当てたステップの（名前, 手前の内容, 変更）を返す."""
    applied = []
    for name, step in _STEPS:
        try:
            doc = tomlkit.parse(text)
        except ParseError as exc:
            raise MigrationError(f"TOML として読めません: {exc}") from exc
        changes = step(doc)
        if changes:
            applied.append((name, text, changes))
            # 除いたキーの後ろの空行と、末尾へ移した表の後ろの空行を落とす
            text = tomlkit.dumps(doc).strip("\n") + "\n"
    return text, applied


def _migrate_m(doc: Container) -> list[str]:
    changes: list[str] = []
    for key in ("machine_type", "xy_calibration"):
        if key in doc:
            del doc[key]
            changes.append(f"{key} を削除")

    _migrate_dispenser(doc, changes)
    # 新しい形のファイルで書き忘れた接続先は補わない（設定エラーにする）
    if changes:
        _make_klipper_explicit(doc, changes)

    # [tact] の速度は新しい形のファイルにも残りうるので、古い形かどうかの判定に含めない
    tact = _table(doc, "tact")
    if tact is not None:
        for key in _TACT_SPEED_KEYS:
            if key in tact:
                tact.remove(key)
                changes.append(f"tact.{key} を削除")
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


def _detach(parent: Container | Table, key: str) -> Table:
    """``parent`` から表を外して返す.

    表の末尾の空行とコメントは次の表の見出しの前に書かれたものなので、表から外す。
    コメントは直前の表の末尾へ移して元の位置に残す（空行は直前の表が自分の末尾に持っている）。
    """
    table = parent.item(key)
    assert isinstance(table, Table)
    body = (parent.value if isinstance(parent, Table) else parent).body
    index = next(i for i, (k, _) in enumerate(body) if k is not None and k.key == key)
    previous = next(
        (item for _, item in reversed(body[:index]) if isinstance(item, Table)), None
    )
    if previous is not None:
        tail = _pop_tail(table)
        first_comment = next(
            (i for i, item in enumerate(tail) if isinstance(item, Comment)), len(tail)
        )
        for item in tail[first_comment:]:
            _last_table(previous).append(None, item)
    parent.remove(key)
    return table


def _pop_tail(table: Table) -> list[Item]:
    """表の最後のキーより後ろ（次の表の見出しの前に書かれた空行とコメント）を外して返す.

    最後のキーが下位表なら、その下位表の末尾を外す。
    """
    own = table.value.body
    last_key = max((i for i, (k, _) in enumerate(own) if k is not None), default=-1)
    last = own[last_key][1] if last_key >= 0 else None
    if isinstance(last, Table):
        return _pop_tail(last)
    tail = [item for _, item in own[last_key + 1 :]]
    del own[last_key + 1 :]
    return tail


def _last_table(table: Table) -> Table:
    """ファイルの上で ``table`` の最後に書かれている表（下位表があればその最後）."""
    tables = [item for _, item in table.value.body if isinstance(item, Table)]
    return _last_table(tables[-1]) if tables else table


def _migrate_n(doc: Container) -> list[str]:
    """[paste_dispenser] と [tact] を [solder] の階層へ作り直し、ファイルの末尾に置く.

    中身ごと移す下位表は本文ごと動く。分けて移すキーは値の行のコメントと一緒に動く（:func:`_lines`）。
    見出しの直前に空行をはさまずに書いたコメントは、その見出しと一緒に動く。
    """
    if "paste_dispenser" not in doc:
        return []
    if "solder" in doc:
        raise MigrationError(
            "[paste_dispenser] と [solder] が両方あります（途中まで移行済み）"
        )
    if not isinstance(doc.item("paste_dispenser"), Table):
        raise MigrationError(
            "[paste_dispenser] の下位表がほかの節をはさんで書かれているので、"
            "自動では移行できません"
        )
    changes = ["[paste_dispenser] を [solder] の階層へ作り直す"]
    tact = None
    if _table(doc, "tact") is not None:
        tact = _detach(doc, "tact")
        changes.append("[tact] を [solder.tact] へ移動")
    old = _detach(doc, "paste_dispenser")

    # 見出しの前のコメントは、ファイルの上で 1 つ前の表の末尾にある
    lines, header = _lines(
        [entry for entry in old.value.body if not isinstance(entry[1], Table)]
    )
    sources = {"": lines}
    tables: dict[str, Table] = {}
    headers: dict[str, list[Item]] = {}
    for key, item in old.value.body:
        if key is not None and isinstance(item, Table):
            headers[key.key] = header
            own, header = _split_tail(_pop_tail(item))
            for entry in own:
                item.append(None, entry)
            _end_with_blank_line(item)
            tables[key.key] = item
    if "motion" in tables:
        sources["motion"], _ = _lines(tables.pop("motion").value.body)

    groups: dict[str, list[tuple[str | None, Item]]] = {}
    for source, owned in sources.items():
        for name, entries in owned.items():
            target, new_name = SOLDER_KEYS[source].get(name, (source, name))
            groups.setdefault(target, []).extend(
                (None if key is None else new_name, item) for key, item in entries
            )

    solder = _new_table(groups.pop("", []))
    built = {"": solder}
    moved = {path: name for name, path in SOLDER_TABLES.items()}
    extra = [name for name in tables if name not in SOLDER_TABLES]
    for path in (*_SOLDER_LAYOUT, *extra):
        source = moved.get(path, path)
        if source in tables:
            table = tables[source]
        elif path in groups or any(
            other.startswith(f"{path}.") and moved[other] in tables for other in moved
        ):
            table = _new_table(groups.pop(path, []))
        else:
            continue
        parent, _, name = path.rpartition(".")
        _add_table(solder, built[parent], name, table, headers.get(source, []))
        built[path] = table
    if tact is not None:
        _add_table(solder, solder, "tact", tact, [])
    doc.add("solder", solder)
    return changes


def _lines(
    entries: list[tuple[Key | None, Item]],
) -> tuple[dict[str, list[tuple[Key | None, Item]]], list[Item]]:
    """表の本文を、キーごとの行と、次の表の見出しに付くコメントに分ける.

    キーごとの行は、キーと値の行に、そのキーに付く単独の行のコメントを並べたもの。
    単独の行のコメントは直前のキー（最初のキーより前なら最初のキー）に付け、空行は落とす。
    """
    last = max((i for i, (key, _) in enumerate(entries) if key is not None), default=-1)
    own, header = _split_tail([item for _, item in entries[last + 1 :]])
    lines: dict[str, list[tuple[Key | None, Item]]] = {}
    leading: list[tuple[Key | None, Item]] = []
    name: str | None = None
    for key, item in [*entries[: last + 1], *((None, item) for item in own)]:
        if key is not None:
            lines[key.key] = [*leading, (key, item)]
            leading = []
            name = key.key
        elif isinstance(item, Comment):
            if name is None:
                leading.append((None, item))
            else:
                lines[name].append((None, item))
    return lines, header


def _split_tail(tail: list[Item]) -> tuple[list[Item], list[Item]]:
    """表の最後のキーより後ろを、表に残す分と、次の表の見出しに付くコメントに分ける.

    空行をはさまずに次の見出しへ続くコメントは、次の表の見出しに付く。表に残す分の後ろの空行は落とす。
    """
    start = len(tail)
    while start > 0 and isinstance(tail[start - 1], Comment):
        start -= 1
    own = tail[:start]
    while own and not isinstance(own[-1], Comment):
        own.pop()
    return own, tail[start:]


def _new_table(entries: list[tuple[str | None, Item]]) -> Table:
    """キーとコメント行から表を作る（キーが無ければ見出しを書かない表にする）."""
    table = tomlkit.table(is_super_table=all(key is None for key, _ in entries))
    for key, item in entries:
        table.append(key, item)
    return table


def _end_with_blank_line(table: Table) -> None:
    body = table.value.body
    if not body or not isinstance(body[-1][1], Whitespace):
        table.add(tomlkit.nl())


def _add_table(
    root: Table, parent: Table, name: str, table: Table, header: list[Item]
) -> None:
    """``parent`` の下位表として ``table`` を足し、見出しの前に空行 1 行と ``header`` のコメントを置く.

    見出しの前の行は、``root`` の中でファイルの上の 1 つ前の表の末尾に置く。
    """
    previous = _last_table(root)
    if not previous.is_super_table():
        _end_with_blank_line(previous)
    for comment in header:
        previous.append(None, comment)
    parent.add(name, table)
    # 空行は自分で置くので、tomlkit が見出しの前に足す空行を消す
    table.trivia.indent = ""


_STEPS: tuple[tuple[str, Callable[[Container], list[str]]], ...] = (
    ("M", _migrate_m),
    ("N", _migrate_n),
)
