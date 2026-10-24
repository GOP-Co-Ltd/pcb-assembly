"""machine.toml の読み込み・起動時の移行と、設定項目の書き込み.

読み込みは呼ぶたびにディスクから読み直す（:meth:`MachineConfigFile.machine`）。
項目は :func:`pcbasm.config.fields.setting_fields` が設定のスキーマから作る。
書き込みは設定ページ・ジョブ・router のどれからでも :meth:`MachineConfigFile.write` の 1 経路を通る。
項目ごとに値を検証し、ディスクの最新の内容に tomlkit で差分を当て（コメントと構造を保つ）、全体を検証し、書き換える前の内容を設定履歴に残してから atomic に書き、``on_change`` で変更を知らせる。
設定履歴の復元も :meth:`MachineConfigFile.restore` を通り、``on_change`` で知らせる。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path

import tomlkit
from tomlkit.items import Item, Table

from pcbasm.atomic import write_bytes_atomic, write_text_atomic
from pcbasm.config import ConfigError, MachineConfig, load_machine, parse_machine
from pcbasm.config.fields import SettingField, setting_fields
from pcbasm.geometry import Point3d
from pcbasm.migrate.machine_toml import NAME, MigrationError, migrate_machine_toml
from pcbasm.validators import is_finite_number
from web.api.history import ConfigHistory
from web.contract.models import (
    ConfigHistoryEntry,
    MachineSettingValue,
    SettingChoice,
    SettingsField,
)

logger = logging.getLogger(__name__)


class InvalidSettingError(ValueError):
    """未知のキー・型の合わない値・検証に通らない書き込み（→ HTTP 400）."""


class MachineConfigFile:
    """``config/machine.toml`` の読み込みと移行、設定項目の読み書きと、その設定履歴."""

    def __init__(
        self, config_dir: Path, on_change: Callable[[], None] = lambda: None
    ) -> None:
        """MachineConfigFile を初期化する.

        Args:
            config_dir: マシン設定ディレクトリ
            on_change: machine.toml を書いた後に呼ぶ（状態の変更を画面へ知らせる）
        """
        self._config_dir = config_dir
        self._on_change = on_change
        self._history = ConfigHistory(self.path)
        self._notice: str | None = None
        self._migration_error: str | None = None

    @property
    def path(self) -> Path:
        """machine.toml のパス."""
        return self._config_dir / "machine.toml"

    def machine(self) -> MachineConfig:
        """machine.toml をディスクから読み、全体を検証して返す（毎回ロード）.

        手編集と書き込みが再起動なしで次の読み込みに効く。

        Raises:
            ConfigError: machine.toml が読めない・検証に通らない場合（degraded）
        """
        return load_machine(self.path)

    def machine_or_error(self) -> tuple[MachineConfig, None] | tuple[None, str]:
        """:meth:`machine` の degraded 版。設定エラーなら ``(None, 理由)`` を返す.

        起動時の移行に失敗していれば、読めない理由として移行の失敗を返す。
        """
        try:
            return self.machine(), None
        except ConfigError as exc:
            if self._migration_error is not None:
                return None, (
                    "machine.toml を移行できませんでした（ファイルは変えていません）: "
                    f"{self._migration_error}"
                )
            return None, str(exc)

    @property
    def notice(self) -> str | None:
        """起動時に machine.toml を移行したときの知らせ（移行しなければ None）."""
        return self._notice

    def migrate(self) -> None:
        """machine.toml を移行する（起動時、設定を読む前に 1 回だけ呼ぶ）.

        失敗しても例外は出さない。ファイルに触れず、読めない間はその理由を設定エラーとして出す（degraded）。
        """
        try:
            changes = migrate_machine_toml(self.path)
        except MigrationError as exc:
            logger.error("machine.toml を移行できません: %s", exc)
            self._migration_error = str(exc)
            return
        if changes:
            logger.info(
                "machine.toml を移行 %s で書き換えました: %s", NAME, " / ".join(changes)
            )
            self._notice = (
                "machine.toml を新しい形へ移行しました"
                f"（元の内容は設定履歴の machine-pre-{NAME}.toml）"
            )

    def history_entries(self) -> list[ConfigHistoryEntry]:
        """machine.toml の世代を新しい順に返す."""
        return self._history.entries()

    def values(self) -> dict[str, MachineSettingValue | None]:
        """各項目の machine.toml に書かれている値を返す（未記載は None）.

        検証に通らない値もそのまま返し、設定エラー（degraded）の間も設定ページから直せるようにする。

        Raises:
            FileNotFoundError: machine.toml が存在しない場合
        """
        doc = tomlkit.parse(self.path.read_text())
        return {field.key: _written(doc, field) for field in setting_fields()}

    def fields(self, machine: MachineConfig | None) -> list[SettingsField]:
        """全項目を、表示の定義・書かれている値・実効値付きで表示する順に返す.

        Args:
            machine: 実効値を読む設定（設定エラーの間は None で、実効値も None）
        """
        values = self.values()
        return [
            SettingsField(
                key=field.key,
                section=field.section,
                section_label=field.section_label,
                group=field.group,
                label=field.label,
                value_type=field.kind,
                unit=field.unit,
                choices=[
                    SettingChoice(value=value, label=label)
                    for value, label in field.choices
                ],
                minimum=field.minimum,
                clearable=field.clearable,
                value=values[field.key],
                resolved=None if machine is None else _resolved(field, machine),
            )
            for field in setting_fields()
        ]

    def write(
        self,
        values: Mapping[str, MachineSettingValue],
        *,
        files: Mapping[str, bytes] | None = None,
    ) -> None:
        """machine.toml へ項目を書き込む（1 件でも不正なら何も書かない）.

        値は項目ごとにその validator で検証する。
        次に、ディスクの最新の内容に差分を当ててから全体を検証する（手編集を上書きしない）。
        誤りの無かった節に誤りを生む書き込みは拒否する。すでに設定エラーの間も、誤りが
        もとから誤りのあった節にしか残らないなら受け付け、1 項目ずつ直せるようにする。
        設定エラーの間は全体の検証が壊れた節の中の誤りを隠すので、範囲の外の値は項目ごとの検証で拒否する。

        校正などの追加ファイルは設定値とファイル名を検証した後に保存し、全件保存できてから
        TOML の参照先を更新する。途中で失敗しても既存の TOML は保持する。
        先に保存できた追加ファイルは残る。

        Args:
            values: 書き込む項目（キーは :func:`setting_fields` の ``key``）
            files: config 直下へ保存する追加ファイル（名前 → 内容）

        Raises:
            InvalidSettingError: 未知のキー・型の合わない値・範囲の外の値・不正な追加ファイル名の場合、
                全体の検証で誤りの無かった節に誤りが出る場合
            OSError: ファイルの保存に失敗した場合
        """
        by_key = {field.key: field for field in setting_fields()}
        unknown = [key for key in values if key not in by_key]
        if unknown:
            raise InvalidSettingError(f"未知のマシン設定キーです: {', '.join(unknown)}")
        entries = [
            (by_key[key], _toml_value(by_key[key], v)) for key, v in values.items()
        ]
        for field, value in entries:
            try:
                field.validate(value)
            except ValueError as exc:
                raise InvalidSettingError(f"{field.key}: {exc}") from exc
        for filename in files or ():
            if (
                filename in {"", ".", "..", self.path.name}
                or Path(filename).name != filename
            ):
                raise InvalidSettingError(f"追加ファイル名が不正です: {filename!r}")
        original = self.path.read_text()
        doc = tomlkit.parse(original)
        for field, value in entries:
            _assign(doc, field, value)
        if error := self._config_error(doc):
            before = self._config_error(tomlkit.parse(original))
            if before is None or not error.sections <= before.sections:
                raise InvalidSettingError(str(error))
        for filename, content in (files or {}).items():
            write_bytes_atomic(self._config_dir / filename, content)
        updated = tomlkit.dumps(doc)
        if updated != original:
            self._history.snapshot()
            write_text_atomic(self.path, updated)
        self._on_change()

    def restore(self, name: str) -> None:
        """設定履歴の世代を machine.toml へ書き戻す（書き戻す前の状態も世代に残す）.

        Raises:
            UnknownSnapshotError: 指定の世代が無い場合
        """
        self._history.restore(name)
        self._on_change()

    def _config_error(self, doc: tomlkit.TOMLDocument) -> ConfigError | None:
        """machine.toml の内容を load_machine と同じ検証にかけ、通らなければその誤りを返す."""
        try:
            parse_machine(doc.unwrap(), self._config_dir)
        except ConfigError as exc:
            return exc
        return None


def _toml_value(field: SettingField, value: object) -> MachineSettingValue:
    """受け取った値を項目の型の TOML の値にする（範囲などの検証は全体の検証が行う）.

    Raises:
        InvalidSettingError: 型が合わない場合
    """
    match field.kind:
        case "float" if is_finite_number(value):
            return float(value)
        case "int" if is_finite_number(value) and float(value).is_integer():
            return int(value)
        case "str" | "choice" if isinstance(value, str):
            return value.strip()
        case "float_or_auto" if value == "auto":
            return "auto"
        case "float_or_auto" if is_finite_number(value):
            return float(value)
        case "float_or_auto" if isinstance(value, str):
            # 数値か auto を書く欄は文字列の入力欄なので、数値も文字列で届く
            try:
                number = float(value)
            except ValueError:
                pass
            else:
                if is_finite_number(number):
                    return number
        case "float_pair" | "float_triple" if isinstance(value, list | tuple):
            size = 2 if field.kind == "float_pair" else 3
            if len(value) == size and all(is_finite_number(item) for item in value):
                return [float(item) for item in value]
    raise InvalidSettingError(
        f"{field.key}: {field.kind} 型の値が必要です（与えられた値: {value!r}）"
    )


def _assign(
    doc: tomlkit.TOMLDocument, field: SettingField, value: MachineSettingValue
) -> None:
    """``field`` の TOML の位置へ値を書く（無い表は作る。座標は x / y / z の 3 キー）."""
    *table_keys, option = field.key.split(".")
    table: tomlkit.TOMLDocument | Table = doc
    for table_key in table_keys:
        if table_key not in table:
            table[table_key] = tomlkit.table()
        child = table[table_key]
        assert isinstance(child, Table)
        table = child
    if field.set:
        assert isinstance(value, list)
        for name, item in zip(field.set, value, strict=True):
            table[name] = item
    else:
        table[option] = value


def _written(
    doc: tomlkit.TOMLDocument, field: SettingField
) -> MachineSettingValue | None:
    """``field`` の machine.toml に書かれている値（座標は x / y / z がそろったときだけ）.

    型の合う値は項目の型にそろえ、合わない値も表せるものはそのまま返す。
    """
    if field.set:
        table = field.key.rsplit(".", 1)[0]
        axes = [_lookup(doc, f"{table}.{name}") for name in field.set]
        raw: object = None if None in axes else axes
    else:
        raw = _lookup(doc, field.key)
    if raw is None:
        return None
    try:
        return _toml_value(field, raw)
    except InvalidSettingError:
        return raw if isinstance(raw, int | float | str) else None


def _lookup(doc: tomlkit.TOMLDocument, key: str) -> object | None:
    """ドット区切りキーで toml ドキュメントを辿り、値を返す（無ければ None）."""
    node: object = doc
    for part in key.split("."):
        if not isinstance(node, tomlkit.TOMLDocument | Table) or part not in node:
            return None
        node = node[part]
    if isinstance(node, tomlkit.TOMLDocument | Table):
        return None
    return node.unwrap() if isinstance(node, Item) else node


def _resolved(
    field: SettingField, machine: MachineConfig
) -> MachineSettingValue | None:
    """``field`` の実効値を API の設定値表現にする."""
    value = field.resolve(machine)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, tuple):
        # reference_point.offsets.* の [x, y]
        return [float(item) for item in value]
    if isinstance(value, Point3d):
        # nozzle_cap / nozzle_clean の position
        return [value.x, value.y, value.z]
    if isinstance(value, bool | int | float | str):
        return value
    return None
