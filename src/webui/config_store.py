"""machine.toml / printer.cfg のホワイトリスト読み書き.

machine.toml は tomlkit でコメント・構造を保持して書き戻す。printer.cfg は
既存行の値のみを行ベースで書き換える（行追加はしない）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import attrs
import tomlkit
from tomlkit.items import Item, Table


@attrs.frozen
class FieldSpec:
    """設定項目 1 件の定義.

    Attributes:
        key: ドット区切りの設定パス（例 "paste_dispenser.rotations_per_ul"）
        label: UI 表示名
        value_type: 値の型
        unit: 表示用の単位（任意）
    """

    key: str
    label: str
    value_type: Literal["float", "int", "str"]
    unit: str | None = None


MACHINE_FIELDS: tuple[FieldSpec, ...] = (
    # [paste_dispenser]
    FieldSpec(
        "paste_dispenser.rotations_per_ul", "1uLあたりの回転数", "float", "rev/uL"
    ),
    FieldSpec("paste_dispenser.nozzle_diameter", "ノズル内径", "float", "mm"),
    FieldSpec("paste_dispenser.fill_speed", "塗布移動速度", "float", "mm/s"),
    FieldSpec("paste_dispenser.max_dispense_rate", "吐出レート上限", "float", "uL/s"),
    FieldSpec("paste_dispenser.dispense_accel", "吐出加速度", "float", "uL/s^2"),
    FieldSpec("paste_dispenser.retract_amount", "リトラクション量", "float", "uL"),
    FieldSpec("paste_dispenser.retract_rate", "リトラクションレート", "float", "uL/s"),
    FieldSpec(
        "paste_dispenser.retract_accel_factor", "リトラクション加速度係数", "float"
    ),
    FieldSpec("paste_dispenser.paste_height", "塗布面のZ高さ", "float", "mm"),
    FieldSpec(
        "paste_dispenser.ul_per_mm2", "面積あたりのペースト量", "float", "uL/mm^2"
    ),
    FieldSpec(
        "paste_dispenser.prime_extra_delay", "プライム後の追加遅延", "float", "s"
    ),
    FieldSpec("paste_dispenser.bead_width_factor", "ビード幅係数", "float"),
    FieldSpec("paste_dispenser.overlap", "ジグザグ行間オーバーラップ", "float"),
    FieldSpec("paste_dispenser.boundary_margin", "外周マージン", "float", "mm"),
    # [paste_dispenser.toolhead]
    FieldSpec("paste_dispenser.toolhead.x", "ツールヘッド相対位置 X", "float", "mm"),
    FieldSpec("paste_dispenser.toolhead.y", "ツールヘッド相対位置 Y", "float", "mm"),
    # [paste_dispenser.pad_align]
    FieldSpec("paste_dispenser.pad_align.tolerance", "収束許容誤差", "float", "mm"),
    FieldSpec("paste_dispenser.pad_align.max_correction", "最大補正量", "float", "mm"),
    FieldSpec(
        "paste_dispenser.pad_align.search_window", "探索窓 片側幅", "float", "mm"
    ),
    FieldSpec("paste_dispenser.pad_align.roi_margin", "ROIマージン", "float", "mm"),
    FieldSpec("paste_dispenser.pad_align.min_roi", "ROI最小辺長", "float", "mm"),
    FieldSpec(
        "paste_dispenser.pad_align.theta_range", "回転探索 片側範囲", "float", "deg"
    ),
    FieldSpec("paste_dispenser.pad_align.canny_low", "Canny下側閾値", "float"),
    FieldSpec("paste_dispenser.pad_align.canny_high", "Canny上側閾値", "float"),
    FieldSpec("paste_dispenser.pad_align.blur_ksize", "ブラーカーネルサイズ", "int"),
    # [probe]
    FieldSpec("probe.servo_name", "サーボ名", "str"),
    FieldSpec("probe.revolution_distance", "一回転あたりの移動量", "float", "mm"),
    FieldSpec("probe.down_distance", "グラウンド下降距離", "float", "mm"),
    FieldSpec("probe.min_radius", "銅箔境界からの最小距離", "float", "mm"),
    FieldSpec("probe.min_samples", "最小サンプル数", "int"),
    FieldSpec("probe.max_samples", "最大サンプル数", "int"),
    # [reference_point]
    FieldSpec("reference_point.x", "基準点 X", "float", "mm"),
    FieldSpec("reference_point.y", "基準点 Y", "float", "mm"),
    FieldSpec("reference_point.target_diameter", "基準点マーカー直径", "float", "mm"),
    # [camera]
    FieldSpec("camera.calibration_file", "キャリブレーションファイル", "str"),
    FieldSpec("camera.device_id", "デバイスID", "int"),
    FieldSpec("camera.width", "幅", "int", "px"),
    FieldSpec("camera.height", "高さ", "int", "px"),
    FieldSpec("camera.fps", "フレームレート", "float", "fps"),
    FieldSpec("camera.format", "ピクセルフォーマット", "str"),
    # [camera.crop]
    FieldSpec("camera.crop.width", "クロップ幅", "int", "px"),
    FieldSpec("camera.crop.height", "クロップ高さ", "int", "px"),
)

MOTION_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("printer.max_velocity", "最大速度", "float", "mm/s"),
    FieldSpec("printer.max_accel", "最大加速度", "float", "mm/s^2"),
    FieldSpec(
        "manual_stepper paste_dispenser.velocity", "ディスペンサー速度", "float", "mm/s"
    ),
    FieldSpec(
        "manual_stepper paste_dispenser.accel",
        "ディスペンサー加速度",
        "float",
        "mm/s^2",
    ),
)

_MACHINE_FIELDS_BY_KEY = {spec.key: spec for spec in MACHINE_FIELDS}
_MOTION_FIELDS_BY_KEY = {spec.key: spec for spec in MOTION_FIELDS}


class UnknownFieldError(ValueError):
    """ホワイトリスト外のキー・型不一致・編集対象行の欠落を表す（→ HTTP 400）."""


def _coerce(spec: FieldSpec, value: object) -> float | int | str:
    """値を FieldSpec の型に合わせて検証・変換する.

    Raises:
        UnknownFieldError: 型が一致しない場合
    """
    if isinstance(value, bool):
        raise UnknownFieldError(f"{spec.key}: bool は受け付けません")
    match spec.value_type:
        case "float":
            if isinstance(value, (int, float)):
                return float(value)
        case "int":
            if isinstance(value, int):
                return value
            if isinstance(value, float) and value.is_integer():
                return int(value)
        case "str":
            if isinstance(value, str):
                return value
    raise UnknownFieldError(
        f"{spec.key}: {spec.value_type} 型の値が必要です（与えられた値: {value!r}）"
    )


class ConfigStore:
    """Configs/ 配下のマシン設定ファイルへの読み書きを集約するクラス."""

    def __init__(self, configs_root: Path) -> None:
        """ConfigStore を初期化する.

        Args:
            configs_root: configs ディレクトリのルート
        """
        self._configs_root = configs_root

    def list_machines(self) -> list[str]:
        """machine.toml を持つマシン名をソート順で返す."""
        return sorted(
            path.parent.name for path in self._configs_root.glob("*/machine.toml")
        )

    def machine_toml_path(self, machine: str) -> Path:
        """machine.toml のパスを返す."""
        return self._configs_root / machine / "machine.toml"

    def printer_cfg_path(self, machine: str) -> Path:
        """printer.cfg のパスを返す."""
        return self._configs_root / machine / "printer.cfg"

    def read_machine_settings(
        self, machine: str
    ) -> dict[str, float | int | str | None]:
        """machine.toml のホワイトリスト項目の現在値を返す.

        toml に存在しないキーは None。

        Raises:
            FileNotFoundError: machine.toml が存在しない場合
        """
        doc = tomlkit.parse(self.machine_toml_path(machine).read_text())
        values: dict[str, float | int | str | None] = {}
        for spec in MACHINE_FIELDS:
            raw = _lookup_toml(doc, spec.key)
            values[spec.key] = None if raw is None else _coerce(spec, raw)
        return values

    def write_machine_settings(
        self, machine: str, values: Mapping[str, float | int | str]
    ) -> None:
        """machine.toml へホワイトリスト項目を書き込む.

        tomlkit によりコメント・構造を保持する。toml に無いキーは追加する。

        Raises:
            UnknownFieldError: 未知キーまたは型不一致の場合
        """
        coerced = {
            key: _coerce(self._machine_spec(key), value)
            for key, value in values.items()
        }
        path = self.machine_toml_path(machine)
        doc = tomlkit.parse(path.read_text())
        for key, value in coerced.items():
            *table_keys, option = key.split(".")
            table = doc
            for table_key in table_keys:
                if table_key not in table:
                    table[table_key] = tomlkit.table()
                child = table[table_key]
                assert isinstance(child, Table)
                table = child
            table[option] = value
        path.write_text(tomlkit.dumps(doc))

    def read_motion_settings(self, machine: str) -> dict[str, float | None]:
        """printer.cfg のホワイトリスト項目の現在値を返す.

        対象行が存在しないキーは None。

        Raises:
            FileNotFoundError: printer.cfg が存在しない場合
        """
        lines = self.printer_cfg_path(machine).read_text().splitlines()
        values: dict[str, float | None] = {}
        for spec in MOTION_FIELDS:
            found = _find_cfg_line(lines, spec.key)
            values[spec.key] = float(found[1].group("value").strip()) if found else None
        return values

    def write_motion_settings(self, machine: str, values: Mapping[str, float]) -> None:
        """printer.cfg のホワイトリスト項目を行ベースで書き換える.

        対象オプションの既存行のみ値を置換する（行追加はしない）。

        Raises:
            UnknownFieldError: 未知キー・型不一致・対象行が無い場合
        """
        coerced = {
            key: float(_coerce(self._motion_spec(key), value))
            for key, value in values.items()
        }
        path = self.printer_cfg_path(machine)
        lines = path.read_text().splitlines(keepends=True)
        for key, value in coerced.items():
            found = _find_cfg_line(lines, key)
            if found is None:
                raise UnknownFieldError(
                    f"printer.cfg に編集対象の行がありません: {key}"
                )
            i, match = found
            lines[i] = match.group("head") + f"{value:g}" + match.group("tail")
        path.write_text("".join(lines))

    def symlink_points_to(self, machine: str, link: Path) -> bool:
        """Link が configs/<machine>/printer.cfg を指す symlink か判定する."""
        if not link.is_symlink():
            return False
        try:
            return link.resolve() == self.printer_cfg_path(machine).resolve()
        except OSError:
            return False

    def _machine_spec(self, key: str) -> FieldSpec:
        if key not in _MACHINE_FIELDS_BY_KEY:
            raise UnknownFieldError(f"未知のマシン設定キーです: {key}")
        return _MACHINE_FIELDS_BY_KEY[key]

    def _motion_spec(self, key: str) -> FieldSpec:
        if key not in _MOTION_FIELDS_BY_KEY:
            raise UnknownFieldError(f"未知のモーション設定キーです: {key}")
        return _MOTION_FIELDS_BY_KEY[key]


def _lookup_toml(doc: tomlkit.TOMLDocument, key: str) -> object | None:
    """ドット区切りキーで toml ドキュメントを辿り、値を返す（無ければ None）."""
    node: object = doc
    for part in key.split("."):
        if not isinstance(node, (tomlkit.TOMLDocument, Table)) or part not in node:
            return None
        node = node[part]
    if isinstance(node, (tomlkit.TOMLDocument, Table)):
        return None
    return node.unwrap() if isinstance(node, Item) else node


_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*(?:[#;].*)?$")


def _option_re(option: str) -> re.Pattern[str]:
    return re.compile(
        rf"^(?P<head>{re.escape(option)}\s*[:=]\s*)"
        r"(?P<value>[^#;\r\n]*?)"
        r"(?P<tail>\s*(?:[#;].*)?(?:\r?\n)?)$"
    )


def _find_cfg_line(lines: list[str], key: str) -> tuple[int, re.Match[str]] | None:
    """printer.cfg の行リストから key（"section.option"）の行を探す.

    Returns:
        (行番号, オプション行のマッチ)。見つからなければ None
    """
    section, option = key.rsplit(".", 1)
    pattern = _option_re(option)
    current_section = None
    for i, line in enumerate(lines):
        if section_match := _SECTION_RE.match(line):
            current_section = section_match.group("name").strip()
        elif current_section == section and (option_match := pattern.match(line)):
            return i, option_match
    return None
