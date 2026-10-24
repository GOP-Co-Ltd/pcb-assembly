"""machine.toml のスキーマと読み込み.

TOML の 1 節を 1 モジュールに置く（``[paste_dispenser]`` は :mod:`.dispenser`、その下の ``pad_align`` は :mod:`.alignment`）。
ここは全体の :class:`MachineConfig` と、読み込み・一括検証だけを持つ。
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import attrs
import cattrs
from cattrs.gen import make_dict_structure_fn

from pcbasm.config.audio import AudioConfig
from pcbasm.config.camera import CameraConfig
from pcbasm.config.detection import DetectionConfig
from pcbasm.config.dispenser import (
    DISPENSE_MODES,
    LINE_DIRECTIONS,
    DispenseMode,
    LineDirection,
    NozzleCapConfig,
    NozzleCleanConfig,
    PasteDispenserConfig,
    PasteHeight,
)
from pcbasm.config.klipper import KlipperConfig
from pcbasm.config.probe import ProbeConfig
from pcbasm.config.reference_point import ReferencePointConfig
from pcbasm.config.settle import SettleConfig
from pcbasm.paths import PROJECT_ROOT


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


def _make_converter() -> cattrs.Converter:
    converter = cattrs.Converter()
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

    未知のキーは読み飛ばす（稼働機の machine.toml には、もう読まない ``machine_type`` が残っている）。
    """

    klipper: KlipperConfig
    paste_dispenser: PasteDispenserConfig
    camera: CameraConfig
    reference_point: ReferencePointConfig
    probe: ProbeConfig
    audio: AudioConfig = attrs.field(factory=AudioConfig)
    settle: SettleConfig = attrs.field(factory=SettleConfig)
    detection: DetectionConfig = attrs.field(factory=DetectionConfig)


def _describe(exc: BaseException, _: object) -> str:
    if isinstance(exc, KeyError):
        return "必須の項目がありません"
    return str(exc) or type(exc).__name__


def _structure[T](value: object, cls: type[T]) -> T:
    """Machine.toml の値を ``cls`` へ structure する（不正なら全件を挙げた ConfigError）."""
    try:
        return _CONVERTER.structure(value, cls)
    except cattrs.BaseValidationError as exc:
        errors = cattrs.transform_error(exc, "machine.toml", _describe)
        # transform_error は 1 件を「理由 @ machine.toml.<節>.<キー>」の書式で返す
        sections = frozenset(
            error.rpartition(" @ machine.toml.")[2].split(".")[0] for error in errors
        )
        raise ConfigError(" / ".join(errors), sections) from exc


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
    return _structure(data["klipper"], KlipperConfig)


def get_config_dir() -> Path:
    """マシン設定ディレクトリを返す.

    ``PCBASM_CONFIG_DIR`` が設定されていればそれを、無ければ
    ``PROJECT_ROOT/config`` を返す。env は呼び出しごとに読む。

    Returns:
        マシン設定ディレクトリのパス
    """
    value = os.environ.get("PCBASM_CONFIG_DIR")
    return Path(value) if value else PROJECT_ROOT / "config"
