"""machine.toml のホワイトリスト読み書き.

machine.toml は tomlkit でコメント・構造を保持して書き戻す。
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import attrs
import tomlkit
from tomlkit.items import Item, Table

from pcbasm.config import (
    DISPENSE_MODES,
    validate_audio_device,
    validate_audio_volume,
    validate_paste_lift_height,
    validate_probe_board_edge_margin,
)

type SettingValueType = Literal[
    "float", "int", "str", "float_pair", "float_or_auto", "dispense_mode", "bool"
]


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
    value_type: SettingValueType
    unit: str | None = None


type MachineSettingValue = float | int | str | bool | list[float]


MACHINE_FIELDS: tuple[FieldSpec, ...] = (
    # [paste_dispenser]
    FieldSpec(
        "paste_dispenser.rotations_per_ul", "1uLあたりの回転数", "float", "rev/uL"
    ),
    FieldSpec("paste_dispenser.nozzle_diameter", "ノズル内径", "float", "mm"),
    FieldSpec("paste_dispenser.dispense_mode", "塗布方式", "dispense_mode"),
    FieldSpec("paste_dispenser.air_pump_enabled", "エアポンプ", "bool"),
    FieldSpec(
        "paste_dispenser.auto_line_aspect_ratio",
        "Auto線塗布しきい縦横比",
        "float",
    ),
    FieldSpec(
        "paste_dispenser.auto_area_short_side_factor",
        "Auto面塗布しきい短辺倍率",
        "float",
    ),
    FieldSpec("paste_dispenser.max_fill_speed", "最大塗布速度", "float", "mm/s"),
    FieldSpec("paste_dispenser.max_dispense_rate", "吐出レート上限", "float", "uL/s"),
    FieldSpec("paste_dispenser.dispense_accel", "吐出加速度", "float", "uL/s^2"),
    FieldSpec(
        "paste_dispenser.solder_paste_density",
        "はんだペースト密度",
        "float",
        "mg/uL",
    ),
    FieldSpec("paste_dispenser.retract_amount", "リトラクション量", "float", "uL"),
    FieldSpec("paste_dispenser.retract_rate", "リトラクションレート", "float", "uL/s"),
    FieldSpec(
        "paste_dispenser.retract_accel_factor", "リトラクション加速度係数", "float"
    ),
    FieldSpec("paste_dispenser.paste_height", "塗布面のZ高さ", "float_or_auto", "mm"),
    FieldSpec("paste_dispenser.lift_height", "吐出後の上昇高さ", "float", "mm"),
    FieldSpec(
        "paste_dispenser.ul_per_mm2", "面積あたりのペースト量", "float", "uL/mm^2"
    ),
    FieldSpec(
        "paste_dispenser.prime_extra_delay", "プライム後の追加遅延", "float", "s"
    ),
    FieldSpec("paste_dispenser.initial_purge_ul", "初回パージ量", "float", "uL"),
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
    FieldSpec("paste_dispenser.pad_align.canny_low", "Canny下側閾値", "float"),
    FieldSpec("paste_dispenser.pad_align.canny_high", "Canny上側閾値", "float"),
    FieldSpec("paste_dispenser.pad_align.blur_ksize", "ブラーカーネルサイズ", "int"),
    FieldSpec("paste_dispenser.pad_align.max_failures", "照合失敗の許容部品数", "int"),
    # [probe]
    FieldSpec("probe.lift_height", "プローブ後の上昇高さ", "float", "mm"),
    FieldSpec("probe.min_radius", "銅箔境界からの最小距離", "float", "mm"),
    FieldSpec("probe.board_edge_margin", "基板外形からの最小距離", "float", "mm"),
    FieldSpec("probe.min_samples", "最小サンプル数", "int"),
    FieldSpec("probe.max_samples", "最大サンプル数", "int"),
    # [reference_point]
    FieldSpec("reference_point.x", "基準点 X", "float", "mm"),
    FieldSpec("reference_point.y", "基準点 Y", "float", "mm"),
    FieldSpec("reference_point.target_diameter", "基準点マーカー直径", "float", "mm"),
    # [reference_point.offsets] — 基盤コーナーから基準点マーカーへの相対位置 [x, y]
    FieldSpec("reference_point.offsets.top_left", "左上 [x, y]", "float_pair", "mm"),
    FieldSpec("reference_point.offsets.top_right", "右上 [x, y]", "float_pair", "mm"),
    FieldSpec("reference_point.offsets.bottom_left", "左下 [x, y]", "float_pair", "mm"),
    FieldSpec(
        "reference_point.offsets.bottom_right", "右下 [x, y]", "float_pair", "mm"
    ),
    # [nozzle_cap] — タスク終了時の駐機先（マシン座標）
    FieldSpec("nozzle_cap.x", "キャップ位置 X", "float", "mm"),
    FieldSpec("nozzle_cap.y", "キャップ位置 Y", "float", "mm"),
    FieldSpec("nozzle_cap.z", "キャップ位置 Z", "float", "mm"),
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
    # [audio] — ジョブ完了通知音（Raspberry Pi 本体スピーカー）
    FieldSpec("audio.device", "出力デバイス", "str"),
    FieldSpec("audio.volume", "音量", "float"),
)

_MACHINE_FIELDS_BY_KEY = {spec.key: spec for spec in MACHINE_FIELDS}


class UnknownFieldError(ValueError):
    """ホワイトリスト外のキー・型不一致・編集対象行の欠落を表す（→ HTTP 400）."""


def _coerce(spec: FieldSpec, value: object) -> MachineSettingValue:
    """値を FieldSpec の型に合わせて検証・変換する.

    Raises:
        UnknownFieldError: 型が一致しない場合
    """
    if spec.value_type != "bool" and isinstance(value, bool):
        raise UnknownFieldError(f"{spec.key}: bool は受け付けません")
    match spec.value_type:
        case "bool":
            if isinstance(value, bool):
                return value
        case "float":
            if isinstance(value, (int, float)):
                coerced_float = float(value)
                if (
                    spec.key == "paste_dispenser.auto_line_aspect_ratio"
                    and coerced_float <= 1.0
                ):
                    raise UnknownFieldError(f"{spec.key}: 1.0より大きい値が必要です")
                if (
                    spec.key == "paste_dispenser.auto_area_short_side_factor"
                    and coerced_float <= 0.0
                ):
                    raise UnknownFieldError(f"{spec.key}: 正の値が必要です")
                if (
                    spec.key == "paste_dispenser.solder_paste_density"
                    and coerced_float <= 0.0
                ):
                    raise UnknownFieldError(f"{spec.key}: 正の値が必要です")
                if (
                    spec.key == "paste_dispenser.initial_purge_ul"
                    and coerced_float < 0.0
                ):
                    raise UnknownFieldError(f"{spec.key}: 0以上の値が必要です")
                if spec.key == "paste_dispenser.lift_height":
                    if error := validate_paste_lift_height(coerced_float):
                        raise UnknownFieldError(error)
                if spec.key == "probe.board_edge_margin":
                    if error := validate_probe_board_edge_margin(coerced_float):
                        raise UnknownFieldError(error)
                if spec.key == "audio.volume":
                    if error := validate_audio_volume(coerced_float):
                        raise UnknownFieldError(error)
                return coerced_float
        case "float_or_auto":
            if value == "auto":
                return "auto"
            if isinstance(value, (int, float)):
                coerced_float = float(value)
                if spec.key == "paste_dispenser.paste_height" and coerced_float <= 0.0:
                    raise UnknownFieldError(f"{spec.key}: 正の値が必要です")
                return coerced_float
        case "int":
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if isinstance(value, int):
                if spec.key == "paste_dispenser.pad_align.max_failures" and value < 0:
                    raise UnknownFieldError(f"{spec.key}: 0以上の値が必要です")
                if (
                    spec.key in ("camera.crop.width", "camera.crop.height")
                    and value < 1
                ):
                    raise UnknownFieldError(f"{spec.key}: 1以上の値が必要です")
                return value
        case "str":
            if isinstance(value, str):
                if spec.key == "audio.device":
                    if error := validate_audio_device(value):
                        raise UnknownFieldError(error)
                    return value.strip()
                return value
        case "dispense_mode":
            if isinstance(value, str) and value in DISPENSE_MODES:
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
    """`config/` 配下のマシン設定ファイルへの読み書きを集約するクラス."""

    def __init__(self, config_dir: Path) -> None:
        """ConfigStore を初期化する.

        Args:
            config_dir: マシン設定ディレクトリ
        """
        self._config_dir = config_dir

    def machine_toml_path(self) -> Path:
        """machine.toml のパスを返す."""
        return self._config_dir / "machine.toml"

    def read_machine_settings(self) -> dict[str, MachineSettingValue | None]:
        """machine.toml のホワイトリスト項目の現在値を返す.

        toml に存在しないキーは None。

        Raises:
            FileNotFoundError: machine.toml が存在しない場合
        """
        doc = tomlkit.parse(self.machine_toml_path().read_text())
        values: dict[str, MachineSettingValue | None] = {}
        for spec in MACHINE_FIELDS:
            raw = _lookup_toml(doc, spec.key)
            values[spec.key] = None if raw is None else _coerce(spec, raw)
        return values

    def write_machine_settings(self, values: Mapping[str, MachineSettingValue]) -> None:
        """machine.toml へホワイトリスト項目を書き込む.

        tomlkit によりコメント・構造を保持する。toml に無いキーは追加する。
        同一ディレクトリ内の一時ファイル経由の atomic replace で書き込むため、
        書き込み中に他プロセスが読んでも torn read（部分/空 TOML）は発生しない。

        Raises:
            UnknownFieldError: 未知キーまたは型不一致の場合
        """
        coerced = {
            key: _coerce(self._machine_spec(key), value)
            for key, value in values.items()
        }
        path = self.machine_toml_path()
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
        payload = tomlkit.dumps(doc)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                encoding="utf-8",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                tmp.write(payload)
            tmp_path.replace(path)
        except Exception:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
            raise

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
