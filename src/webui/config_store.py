"""machine.toml / printer.cfg のホワイトリスト読み書き.

machine.toml は tomlkit でコメント・構造を保持して書き戻す。printer.cfg は
既存行の値のみを行ベースで書き換える（行追加はしない）。
"""

from __future__ import annotations

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
    value_type: Literal["float", "int", "str", "float_pair"]
    unit: str | None = None


type MachineSettingValue = float | int | str | list[float]


# 設定セクション（key のドット区切り親パス）→ UI 表示名。
# settings ページの階層表示に使う
SECTION_LABELS: dict[str, str] = {
    "paste_dispenser": "ペーストディスペンサー",
    "paste_dispenser.toolhead": "ペーストディスペンサー / ツールヘッド",
    "paste_dispenser.pad_align": "ペーストディスペンサー / パッド位置合わせ",
    "probe": "プローブ",
    "reference_point": "基準点",
    "camera": "カメラ",
    "camera.crop": "カメラ / クロップ",
}


def section_of(key: str) -> str:
    """設定 key の属するセクション（最後のドットより前）を返す."""
    return key.rsplit(".", 1)[0]


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
    FieldSpec("probe.lift_height", "プローブ後の上昇高さ", "float", "mm"),
    FieldSpec("probe.min_radius", "銅箔境界からの最小距離", "float", "mm"),
    FieldSpec("probe.min_samples", "最小サンプル数", "int"),
    FieldSpec("probe.max_samples", "最大サンプル数", "int"),
    FieldSpec("probe.shift", "プローブ点シフト", "float_pair", "mm"),
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

_MACHINE_FIELDS_BY_KEY = {spec.key: spec for spec in MACHINE_FIELDS}


class UnknownFieldError(ValueError):
    """ホワイトリスト外のキー・型不一致・編集対象行の欠落を表す（→ HTTP 400）."""


def _coerce(spec: FieldSpec, value: object) -> MachineSettingValue:
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
        case "float_pair":
            pair = _coerce_float_pair(value)
            if pair is not None:
                return pair
    raise UnknownFieldError(
        f"{spec.key}: {spec.value_type} 型の値が必要です（与えられた値: {value!r}）"
    )


def _coerce_float_pair(value: object) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    pair: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        pair.append(float(item))
    return pair


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

    def read_machine_settings(
        self, machine: str
    ) -> dict[str, MachineSettingValue | None]:
        """machine.toml のホワイトリスト項目の現在値を返す.

        toml に存在しないキーは None。

        Raises:
            FileNotFoundError: machine.toml が存在しない場合
        """
        doc = tomlkit.parse(self.machine_toml_path(machine).read_text())
        values: dict[str, MachineSettingValue | None] = {}
        for spec in MACHINE_FIELDS:
            raw = _lookup_toml(doc, spec.key)
            values[spec.key] = None if raw is None else _coerce(spec, raw)
        return values

    def write_machine_settings(
        self, machine: str, values: Mapping[str, MachineSettingValue]
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

    def _machine_spec(self, key: str) -> FieldSpec:
        if key not in _MACHINE_FIELDS_BY_KEY:
            raise UnknownFieldError(f"未知のマシン設定キーです: {key}")
        return _MACHINE_FIELDS_BY_KEY[key]


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
