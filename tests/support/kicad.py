"""KiCad の実 footprint library をテスト用に作る（system の KiCad library に依存しない）."""

from collections.abc import Sequence
from pathlib import Path

import pcbnew


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
