import json
import shutil
import socket
import struct
from collections.abc import Callable, Sequence
from functools import wraps
from pathlib import Path
from secrets import token_hex
from typing import Any, ParamSpec, TypeVar

import pcbnew
import pytest

from tests.support.paths import TESTING_CONFIG_DIR, TESTING_DATA_DIR

PASTE_VOLUME_MATERIAL_DIR = TESTING_DATA_DIR / "paste-volume"

# 実素材 `data/testing/paste-volume/` の収集条件（出典は同 directory の README）
PASTE_VOLUME_PIXEL_PER_MM = 28.677782176153425

# 素材名と、そこへ割り当てる教師体積 [uL]。同じ素材を 2 セルに使い体積をわずかに
# ずらすことで、3 次フィットを厳密内挿ではなく過決定にする
PASTE_VOLUME_DISPENSED: tuple[tuple[str, float], ...] = (
    ("small", 0.050),
    ("small", 0.052),
    ("medium", 0.125),
    ("medium", 0.121),
    ("large", 0.200),
    ("large", 0.206),
)


def build_paste_volume_session(root: Path, *, blank_material: str = "blank") -> Path:
    """実素材を参照する schema v3 の完成 session を組み立てる.

    合成画像では銅板テクスチャ・照明ムラ・ペーストの質感が再現できず blank ガードの
    効きを確かめられないので、コミット済みの実画像を session の形へ並べ直す。

    Args:
        root: 作成する session directory
        blank_material: blank セルに使う素材名（誤検出の経路を試すときに差し替える）

    Returns:
        作成した session directory
    """
    document = json.loads(
        (TESTING_DATA_DIR / "schemas" / "paste_dataset_metadata_v3.json").read_text(
            encoding="utf-8"
        )
    )
    sample_template = document["samples"][0]
    blank_template = document["blanks"][0]

    samples = []
    for order, (material, volume) in enumerate(PASTE_VOLUME_DISPENSED, start=1):
        samples.append(
            {
                **sample_template,
                "index": order,
                "order": order,
                "measured_volume_ul": volume,
                "views": [_paste_volume_view(order, number) for number in (0, 1)],
            }
        )
        _copy_paste_volume_views(root, order, material)

    blank_index = len(PASTE_VOLUME_DISPENSED) + 1
    _copy_paste_volume_views(root, blank_index, blank_material)
    document["samples"] = samples
    document["blanks"] = [
        {
            **blank_template,
            "index": blank_index,
            "measured_volume_ul": 0.0,
            "views": [_paste_volume_view(blank_index, number) for number in (0, 1)],
        }
    ]
    document["camera"] = {
        **document["camera"],
        "pixel_per_mm": PASTE_VOLUME_PIXEL_PER_MM,
    }
    document["total"] = {
        **document["total"],
        "measured_volume_ul": sum(volume for _, volume in PASTE_VOLUME_DISPENSED),
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "metadata.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    return root


def _paste_volume_view(index: int, number: int) -> dict[str, Any]:
    """1 view ぶんの metadata（crop 矩形は素材の 53x53 に合わせる）."""
    return {
        "number": number,
        "offset_x_mm": float(number),
        "offset_y_mm": 0.0,
        "pixel_rect": [0, 0, 53, 53],
        "pre": f"pre/{index:06d}.{number:02d}.png",
        "post": f"post/{index:06d}.{number:02d}.png",
    }


def _copy_paste_volume_views(session: Path, index: int, material: str) -> None:
    """素材の pre/post を session の view path へ複製する."""
    for number in (0, 1):
        for phase in ("pre", "post"):
            destination = session / phase / f"{index:06d}.{number:02d}.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                PASTE_VOLUME_MATERIAL_DIR / material / phase / f"{number:02d}.png",
                destination,
            )


def copy_testing_config(tmp_path: Path) -> Path:
    """`data/testing/config` を `tmp_path/config` へ複製して返す（webui / e2e 共有）.

    テストが machine.toml を書き換えるため、追跡下の fixture を汚さないよう毎回コピーする。
    """
    config_dir = tmp_path / "config"
    shutil.copytree(TESTING_CONFIG_DIR, config_dir)
    return config_dir


def make_paste_test_board_footprint_root(root: Path) -> Path:
    """テスト塗布基板テスト用の実KiCad footprint rootを作る.

    system KiCad libraryの有無や収録数に依存させず、productionと同じ
    ``pcbnew.FootprintLoad`` 経路を通すため、実 ``FOOTPRINT`` / ``PAD`` を
    ``*.pretty/*.kicad_mod`` として保存する。
    """
    libraries = {
        "Resistor_SMD.pretty": (
            _two_pad_footprint("R_0402_1005Metric", 0.54, 0.64),
            _two_pad_footprint("R_0603_1608Metric", 0.90, 0.95),
            _two_pad_footprint("R_0805_2012Metric", 1.00, 1.40),
            _two_pad_footprint("R_1206_3216Metric", 1.15, 1.80),
        ),
        "Package_TO_SOT_SMD.pretty": (
            _uniform_pad_footprint("SOT-23", ("1", "2", "3"), 0.80, 0.90),
            _uniform_pad_footprint("SOT-23-5", ("1", "2", "3", "4", "5"), 0.60, 0.90),
            _sot223_footprint(),
        ),
        "Package_DFN_QFN.pretty": (_qfn_footprint(),),
    }
    root.mkdir(parents=True, exist_ok=True)
    for library_name, footprints in libraries.items():
        _save_footprint_library(root / library_name, footprints)
    return root


def make_paste_test_board_offset_pad_root(
    root: Path, *, shape_offset_x_mm: float, pad_size_mm: float = 1.0
) -> Path:
    """形状offset付き正方形SMD padを持つ独立KiCad footprint rootを作る."""
    footprint = _footprint("OffsetPad")
    _add_smd_pad(
        footprint,
        number="1",
        width_mm=pad_size_mm,
        height_mm=pad_size_mm,
        x_mm=0.0,
        shape_offset_x_mm=shape_offset_x_mm,
    )
    root.mkdir(parents=True, exist_ok=True)
    _save_footprint_library(root / "Test.pretty", (footprint,))
    return root


def _two_pad_footprint(
    name: str, width_mm: float, height_mm: float
) -> pcbnew.FOOTPRINT:
    return _uniform_pad_footprint(name, ("1", "2"), width_mm, height_mm)


def _uniform_pad_footprint(
    name: str,
    pad_numbers: Sequence[str],
    width_mm: float,
    height_mm: float,
) -> pcbnew.FOOTPRINT:
    footprint = _footprint(name)
    for index, number in enumerate(pad_numbers):
        _add_smd_pad(
            footprint,
            number=number,
            width_mm=width_mm,
            height_mm=height_mm,
            x_mm=float(index) * (width_mm + 0.5),
        )
    return footprint


def _qfn_footprint() -> pcbnew.FOOTPRINT:
    """Paste aperture、lead、exposed padの3群を持つQFN相当fixture."""
    footprint = _footprint("QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm")
    paste_only_layers = pcbnew.LSET()
    paste_only_layers.AddLayer(pcbnew.F_Paste)
    copper_mask_layers = pcbnew.LSET()
    copper_mask_layers.AddLayer(pcbnew.F_Cu)
    copper_mask_layers.AddLayer(pcbnew.F_Mask)
    for index in range(4):
        _add_smd_pad(
            footprint,
            number="",
            width_mm=0.50,
            height_mm=0.50,
            x_mm=float(index),
            layer_set=paste_only_layers,
        )
    for index in range(16):
        _add_smd_pad(
            footprint,
            number=str(index + 1),
            width_mm=0.25,
            height_mm=0.80,
            x_mm=float(index),
        )
    _add_smd_pad(
        footprint,
        number="17",
        width_mm=1.75,
        height_mm=1.75,
        x_mm=20.0,
        layer_set=copper_mask_layers,
    )
    return footprint


def _sot223_footprint() -> pcbnew.FOOTPRINT:
    """3本のleadと異寸法tabの2群を持つSOT-223相当fixture."""
    footprint = _footprint("SOT-223-3_TabPin2")
    for index, number in enumerate(("1", "2", "3")):
        _add_smd_pad(
            footprint,
            number=number,
            width_mm=0.70,
            height_mm=1.50,
            x_mm=float(index),
        )
    _add_smd_pad(
        footprint,
        number="2",
        width_mm=3.00,
        height_mm=2.00,
        x_mm=4.0,
    )
    return footprint


def _footprint(name: str) -> pcbnew.FOOTPRINT:
    footprint = pcbnew.FOOTPRINT(None)
    footprint.SetFPID(pcbnew.LIB_ID("", name))
    footprint.SetReference("REF**")
    footprint.SetValue(name)
    return footprint


def _add_smd_pad(
    footprint: pcbnew.FOOTPRINT,
    *,
    number: str,
    width_mm: float,
    height_mm: float,
    x_mm: float,
    layer_set: pcbnew.LSET | None = None,
    shape_offset_x_mm: float = 0.0,
) -> None:
    pad = pcbnew.PAD(footprint)
    pad.SetNumber(number)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetShape(pcbnew.PAD_SHAPE_RECTANGLE)
    pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(width_mm), pcbnew.FromMM(height_mm)))
    pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), 0))
    pad.SetOffset(pcbnew.VECTOR2I(pcbnew.FromMM(shape_offset_x_mm), 0))
    pad.SetLayerSet(pad.SMDMask() if layer_set is None else layer_set)
    footprint.Add(pad)


def _save_footprint_library(path: Path, footprints: Sequence[pcbnew.FOOTPRINT]) -> None:
    plugin = pcbnew.PCB_IO_KICAD_SEXPR()
    plugin.FootprintLibCreate(str(path))
    first, *remaining = footprints
    # pcbnew.FootprintSaveは空libraryのformatを判別できないため、最初の1個だけ
    # concrete pluginでbootstrapする。以後は公開helperを通して実ファイルへ保存する。
    plugin.FootprintSave(str(path), first)
    for footprint in remaining:
        pcbnew.FootprintSave(str(path), footprint)


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _skip_unless_available(
    is_available: Callable[[], bool],
    reason: str,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """実行時に能力（mDNS）を確認してテストをskipするdecoratorを返す."""

    def decorator(test: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(test)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            if not is_available():
                pytest.skip(reason)
            return test(*args, **kwargs)

        return wrapper

    return decorator


# mDNS の能力プローブ結果（bind し直さないようモジュールレベルでキャッシュする）
_MDNS_AVAILABLE: bool | None = None

_MDNS_GROUP = "224.0.0.251"
_MDNS_PORT = 5353


def _mdns_available() -> bool:
    """5353 を共有 bind してループバックでマルチキャストに join できるか確認する.

    実機では avahi が 5353 を持っているので `SO_REUSEADDR` での共存を確かめる
    （共存できない環境ではテストが zeroconf を起動できない）。結果はキャッシュする。
    """
    global _MDNS_AVAILABLE
    if _MDNS_AVAILABLE is None:
        _MDNS_AVAILABLE = _probe_mdns()
    return _MDNS_AVAILABLE


def _probe_mdns() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("", _MDNS_PORT))
            probe.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                struct.pack(
                    "=4s4s",
                    socket.inet_aton(_MDNS_GROUP),
                    socket.inet_aton("127.0.0.1"),
                ),
            )
        return True
    except OSError:
        return False


skip_if_no_mdns = _skip_unless_available(
    _mdns_available,
    "mDNS（5353 の共有 bind / マルチキャスト join）が使えません",
)


def random_service_type() -> str:
    """テスト専用の DNS-SD サービス型（実 LAN / CI の並列ジョブと混ざらない）.

    実 zeroconf を触るテストは運用のサービス型（``_pcbasm._tcp``）を使わない。
    bind が失敗する前提のテスト（存在しない IF を指定するもの）でも、万一 bind が
    成功したときに実 LAN へ広告・探索を漏らさないための構造的な予防。
    """
    return f"_pcbasmt{token_hex(4)}._tcp.local."
