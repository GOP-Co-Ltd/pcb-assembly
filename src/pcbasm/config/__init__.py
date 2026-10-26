"""machine.toml のスキーマと読み込み.

TOML の 1 節を 1 モジュールに置く（``[solder]`` は :mod:`.solder`、``[alignment]`` は :mod:`.alignment`）。
TOML のパスと属性のパスは一致する（``solder.defaults.ul_per_mm2`` は ``MachineConfig.solder.defaults.ul_per_mm2``）。
ここは全体の :class:`MachineConfig` と、読み込み・一括検証だけを持つ。
設定ページの項目は各節の metadata から :func:`pcbasm.config.fields.setting_fields` が作る。

機体の機能（:data:`CAPABILITIES`）は節の有無で表す。
``[solder]`` があればはんだ塗布機、``[pnp]`` があれば部品実装機、両方あれば両方使える機体になる。
どちらも無い machine.toml は設定エラーにする。種別を表すキーは持たない。
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

import attrs
import cattrs
from cattrs.gen import make_dict_structure_fn

from pcbasm.config.alignment import AlignmentConfig
from pcbasm.config.audio import AudioConfig
from pcbasm.config.camera import CameraConfig
from pcbasm.config.detection import DetectionConfig
from pcbasm.config.fields import section
from pcbasm.config.klipper import KlipperConfig
from pcbasm.config.pnp import PnpConfig
from pcbasm.config.probe import ProbeConfig
from pcbasm.config.reference_point import ReferencePointConfig
from pcbasm.config.settle import SettleConfig
from pcbasm.config.solder import (
    DispenserConfig,
    NozzleCapConfig,
    NozzleCleanConfig,
    SolderConfig,
)
from pcbasm.geometry import Point3d
from pcbasm.pasting.params import (
    DISPENSE_MODES,
    LINE_DIRECTIONS,
    DispenseMode,
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


def _dispenser_hook(
    converter: cattrs.Converter,
) -> Callable[[Mapping[str, Any], object], DispenserConfig]:
    """``[solder.dispenser]`` の structure hook を作る.

    ``retract_rate`` を省略したら ``max_dispense_rate`` と同じ値にする。
    """
    structure = make_dict_structure_fn(DispenserConfig, converter)

    def hook(value: Mapping[str, Any], _: object) -> DispenserConfig:
        data = dict(value)
        if "retract_rate" not in data and "max_dispense_rate" in data:
            data["retract_rate"] = data["max_dispense_rate"]
        return structure(data, DispenserConfig)

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
    converter.register_structure_hook(DispenserConfig, _dispenser_hook(converter))
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


type Capability = Literal["solder", "pnp"]

# 機体の機能。名前はその機能を表す machine.toml の節の名前
CAPABILITIES: tuple[Capability, ...] = ("solder", "pnp")
CAPABILITY_LABELS: dict[Capability, str] = {"solder": "はんだ塗布", "pnp": "部品実装"}


@attrs.frozen
class MachineConfig:
    """machine.toml 全体の設定（:func:`load_machine` が一括で検証して作る）.

    未知のキーは設定エラーにする（書き間違えたキーが黙って既定値で動くのを防ぐ）。
    ``section`` の付いた節が設定ページの 1 画面になる（``order`` は画面の並び順）。
    ``solder`` と ``pnp`` は機能の節で、無い機能は None になる。
    """

    klipper: KlipperConfig
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
    solder: SolderConfig | None = attrs.field(
        default=None, metadata=section(label=CAPABILITY_LABELS["solder"], order=1)
    )
    # 設定の項目を足すときに section の metadata を付ける
    pnp: PnpConfig | None = None

    @property
    def cap_position(self) -> Point3d | None:
        """ジョブの終了時の駐機先（[solder.nozzle.cap]）.

        塗布機でないか未教示なら None で、PRESENT へ駐機する。
        """
        return None if self.solder is None else self.solder.nozzle.cap.position

    def require_solder(self) -> SolderConfig:
        """はんだ塗布の設定（塗布のジョブと API は solder 機能の機体にだけ登録する）.

        Raises:
            ConfigError: 起動したあとに machine.toml から [solder] を消した場合
        """
        if self.solder is None:
            raise ConfigError(
                "machine.toml に [solder] がありません（はんだ塗布機ではありません）",
                frozenset({"solder"}),
            )
        return self.solder


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
    if not capabilities(data):
        raise ConfigError(
            "machine.toml に [solder]（はんだ塗布）か [pnp]（部品実装）のどちらかが要ります",
            frozenset(CAPABILITIES),
        )
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


def capabilities(data: Mapping[str, Any]) -> tuple[Capability, ...]:
    """machine.toml の内容が表す機体の機能（節があれば、その機能がある）.

    読み込めば ``MachineConfig.solder`` / ``pnp`` が None でないことと同じ。
    検証の前の内容から決めるので、設定エラーの間も分かる。
    """
    return tuple(name for name in CAPABILITIES if name in data)


def load_capabilities(path: str | Path) -> tuple[Capability, ...]:
    """machine.toml の節から機体の機能を読む（設定エラーの間も機能ごとのジョブと画面を出せるように）.

    Raises:
        ConfigError: TOML として読めない場合
    """
    return capabilities(_read_toml(Path(path)))


def load_klipper(path: str | Path) -> KlipperConfig:
    """machine.toml の [klipper] 節だけを読む（設定エラーの間も緊急停止を送れるように）.

    Raises:
        ConfigError: TOML として読めない・[klipper] が無いか不正な場合
    """
    data = _read_toml(Path(path))
    if "klipper" not in data:
        raise ConfigError("machine.toml に [klipper] がありません")
    return _structure(data["klipper"], KlipperConfig, "machine.toml.klipper")
