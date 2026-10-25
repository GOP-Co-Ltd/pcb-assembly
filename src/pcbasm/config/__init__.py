"""machine.toml のスキーマと読み込み.

TOML の 1 節を 1 モジュールに置く（``[paste_dispenser]`` は :mod:`.dispenser`、``[alignment]`` は :mod:`.alignment`）。
TOML のパスと属性のパスは一致する（``paste_dispenser.defaults.ul_per_mm2`` は ``MachineConfig.paste_dispenser.defaults.ul_per_mm2``）。
ここは全体の :class:`MachineConfig` と、読み込み・一括検証だけを持つ。
設定ページの項目は各節の metadata から :func:`pcbasm.config.fields.setting_fields` が作る。
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import attrs
import cattrs
from cattrs.gen import make_dict_structure_fn

from pcbasm.config.alignment import AlignmentConfig
from pcbasm.config.audio import AudioConfig
from pcbasm.config.camera import CameraConfig
from pcbasm.config.detection import DetectionConfig
from pcbasm.config.dispenser import (
    NozzleCapConfig,
    NozzleCleanConfig,
    PasteDispenserConfig,
)
from pcbasm.config.fields import section
from pcbasm.config.klipper import KlipperConfig
from pcbasm.config.probe import ProbeConfig
from pcbasm.config.reference_point import ReferencePointConfig
from pcbasm.config.settle import SettleConfig
from pcbasm.pasting.params import (
    DISPENSE_MODES,
    LINE_DIRECTIONS,
    DispenseMode,
    DispenseMotion,
    LineDirection,
    PasteHeight,
)


def _structure_dispense_mode(value: object, _: object) -> DispenseMode:
    if isinstance(value, str) and value in DISPENSE_MODES:
        return value
    raise ValueError(f"未知の塗布方式です: {value!r}")


def _structure_line_direction(value: object, _: object) -> LineDirection:
    if isinstance(value, str) and value in LINE_DIRECTIONS:
        return value
    raise ValueError(f"未知の線走行方向です: {value!r}")


def _structure_paste_height(value: object, _: object) -> PasteHeight:
    if value == "auto":
        return "auto"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"高さは'auto'または数値である必要があります: {value!r}")
    return float(value)


def _position_hook(
    cls: type[Any], converter: cattrs.Converter
) -> Callable[[Mapping[str, Any], object], Any]:
    """TOML の x / y / z を ``position`` の 1 組へ束ねる structure hook を作る.

    3 つそろえば教示済み、すべて無ければ未教示（None）。一部だけなら設定エラーにする。
    """
    structure = make_dict_structure_fn(cls, converter)

    def hook(value: Mapping[str, Any], _: object) -> Any:
        data = dict(value)
        axes = {axis: data.pop(axis) for axis in ("x", "y", "z") if axis in data}
        if axes:
            if len(axes) < 3:
                raise ValueError(
                    "座標は x / y / z の 3 つをそろえて書く必要があります"
                    f"（書かれているのは {', '.join(axes)} だけです）"
                )
            data["position"] = axes
        return structure(data, cls)

    return hook


def _motion_hook(
    converter: cattrs.Converter,
) -> Callable[[Mapping[str, Any], object], DispenseMotion]:
    """``[paste_dispenser.motion]`` の structure hook を作る.

    ``retract_rate`` を省略したら ``max_dispense_rate`` と同じ値にする。
    """
    structure = make_dict_structure_fn(DispenseMotion, converter)

    def hook(value: Mapping[str, Any], _: object) -> DispenseMotion:
        data = dict(value)
        if "retract_rate" not in data and "max_dispense_rate" in data:
            data["retract_rate"] = data["max_dispense_rate"]
        return structure(data, DispenseMotion)

    return hook


def _make_converter() -> cattrs.Converter:
    converter = cattrs.Converter(forbid_extra_keys=True)
    converter.register_structure_hook_func(
        lambda t: t == DispenseMode, _structure_dispense_mode
    )
    converter.register_structure_hook_func(
        lambda t: t == LineDirection, _structure_line_direction
    )
    converter.register_structure_hook_func(
        lambda t: t == PasteHeight, _structure_paste_height
    )
    for cls in (NozzleCapConfig, NozzleCleanConfig):
        converter.register_structure_hook(cls, _position_hook(cls, converter))
    converter.register_structure_hook(DispenseMotion, _motion_hook(converter))
    return converter


_CONVERTER = _make_converter()


class ConfigError(Exception):
    """machine.toml を読めない・検証に通らない（WebAPI はこの間 degraded で動く）.

    Attributes:
        sections: 誤りのある節（``probe`` など、machine.toml の最上位の表やキーの名前）
    """

    def __init__(self, message: str, sections: frozenset[str] = frozenset()) -> None:
        super().__init__(message)
        self.sections = sections


@attrs.frozen
class MachineConfig:
    """machine.toml 全体の設定（:func:`load_machine` が一括で検証して作る）.

    未知のキーは設定エラーにする（書き間違えたキーが黙って既定値で動くのを防ぐ）。
    ``section`` の付いた節が設定ページの 1 画面になる（``order`` は画面の並び順）。
    """

    klipper: KlipperConfig
    paste_dispenser: PasteDispenserConfig = attrs.field(
        metadata=section(label="ペーストディスペンサー", order=1)
    )
    camera: CameraConfig = attrs.field(metadata=section(label="カメラ", order=5))
    reference_point: ReferencePointConfig = attrs.field(
        metadata=section(label="基準点", order=4)
    )
    probe: ProbeConfig = attrs.field(metadata=section(label="プローブ", order=3))
    alignment: AlignmentConfig = attrs.field(
        factory=AlignmentConfig, metadata=section(label="位置合わせ", order=2)
    )
    audio: AudioConfig = attrs.field(
        factory=AudioConfig, metadata=section(label="通知音", order=8)
    )
    settle: SettleConfig = attrs.field(
        factory=SettleConfig, metadata=section(label="静定待ち", order=6)
    )
    detection: DetectionConfig = attrs.field(
        factory=DetectionConfig, metadata=section(label="統計検出", order=7)
    )
    # タクトタイム見積りのための予約節。中身は読まずに受け入れる
    tact: dict[str, Any] | None = None


def _describe(exc: BaseException, _: object) -> str:
    if isinstance(exc, KeyError):
        return "必須の項目がありません"
    if isinstance(exc, cattrs.ForbiddenExtraKeysError):
        return f"未知のキーです: {', '.join(sorted(exc.extra_fields))}"
    return str(exc) or type(exc).__name__


def _structure[T](
    value: Mapping[str, Any], cls: type[T], path: str = "machine.toml"
) -> T:
    """machine.toml の値を ``cls`` へ structure する.

    ``path`` は誤りの位置を示す接頭辞。不正なら全件を挙げた ConfigError を出す。
    """
    try:
        return _CONVERTER.structure(value, cls)
    except cattrs.BaseValidationError as exc:
        errors = cattrs.transform_error(exc, path, _describe)
        # transform_error は 1 件を「理由 @ machine.toml.<節>.<キー>」の書式で返す。
        # 最上位の未知のキーは「理由 @ machine.toml」なので、そのキー自身を節とする
        sections = {
            error.rpartition(" @ machine.toml")[2].removeprefix(".").split(".")[0]
            for error in errors
        }
        if "" in sections:
            sections.remove("")
            sections.update(set(value) - attrs.fields_dict(cls).keys())  # type: ignore[arg-type]
        raise ConfigError(" / ".join(errors), frozenset(sections)) from exc


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name} を TOML として読めません: {exc}") from exc


def parse_machine(data: Mapping[str, Any], config_dir: Path) -> MachineConfig:
    """machine.toml の内容を全体で 1 回だけ検証して MachineConfig にする.

    書き込む前の内容の検証にも使う。``camera.calibration_file`` は ``config_dir`` からの相対パスとして解決する。

    Raises:
        ConfigError: 検証に通らない場合（不正な項目をすべて挙げる）
    """
    machine = _structure(data, MachineConfig)
    calibration_file = config_dir.resolve() / machine.camera.calibration_file
    return attrs.evolve(
        machine,
        camera=attrs.evolve(machine.camera, calibration_file=calibration_file),
    )


def load_machine(path: str | Path) -> MachineConfig:
    """machine.toml をディスクから読み、全体を 1 回で検証する.

    Raises:
        ConfigError: TOML として読めない・検証に通らない場合
    """
    path = Path(path)
    return parse_machine(_read_toml(path), path.parent)


def load_klipper(path: str | Path) -> KlipperConfig:
    """machine.toml の [klipper] 節だけを読む（設定エラーの間も緊急停止を送れるように）.

    Raises:
        ConfigError: TOML として読めない・[klipper] が無いか不正な場合
    """
    data = _read_toml(Path(path))
    if "klipper" not in data:
        raise ConfigError("machine.toml に [klipper] がありません")
    return _structure(data["klipper"], KlipperConfig, "machine.toml.klipper")
