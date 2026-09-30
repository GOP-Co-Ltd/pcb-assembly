"""KiCad footprint ライブラリの検索・読込と、footprint / pad の幾何ユーティリティ.

``pcbasm.pcb`` からは re-export しないので、このモジュールを直接 import する。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal, TypeVar

import attrs
import pcbnew

from pcbasm.geometry import Point2d
from pcbasm.pcb.units import (
    KICAD_COORD_MAX_NM,
    KICAD_COORD_MIN_NM,
    KicadCoordinateError,
    KicadError,
    to_mm,
    vector,
)

DEFAULT_KICAD9_FOOTPRINT_DIR = Path("/usr/share/kicad/footprints")
FOOTPRINT_ROOT_ENV = "KICAD9_FOOTPRINT_DIR"

FRONT_PAD_LAYERS: tuple[int, ...] = (pcbnew.F_Cu, pcbnew.F_Paste)

PadShapeName = Literal["circle", "rectangle", "roundrect", "oval"]
_PAD_SHAPES: dict[PadShapeName, int] = {
    "circle": pcbnew.PAD_SHAPE_CIRCLE,
    "rectangle": pcbnew.PAD_SHAPE_RECTANGLE,
    "roundrect": pcbnew.PAD_SHAPE_ROUNDRECT,
    "oval": pcbnew.PAD_SHAPE_OVAL,
}

_SIGNATURE_LAYERS = (
    ("F.Cu", pcbnew.F_Cu),
    ("F.Mask", pcbnew.F_Mask),
    ("F.Paste", pcbnew.F_Paste),
)
_SIGNATURE_QUANTUM_NM = 1_000
_MAX_FILESYSTEM_COMPONENT_BYTES = 255
_FOOTPRINT_SUFFIX = ".kicad_mod"

LayerName = TypeVar("LayerName", bound=str)


class FootprintLibraryError(KicadError):
    """Footprint ライブラリの環境・読込・形状に起因するエラー."""


@attrs.frozen
class FootprintInfo:
    """検索可能な KiCad footprint."""

    footprint_id: str
    label: str
    library: str
    footprint: str


@attrs.frozen
class FootprintEnvelope:
    """Footprint の pad（F.Cu / F.Paste）を包む軸平行矩形 [mm]."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @classmethod
    def measure(cls, footprint: pcbnew.FOOTPRINT, angle: float) -> FootprintEnvelope:
        """原点に置き ``angle`` 度回転させたときの F.Cu / F.Paste pad の外接矩形 [mm].

        Raises:
            FootprintLibraryError: F.Cu / F.Paste の pad が無い
        """
        placed = duplicate_footprint(footprint)
        placed.SetPosition(vector(0.0, 0.0))
        placed.SetOrientationDegrees(angle)
        bounds: list[tuple[float, float, float, float]] = []
        for pad in placed.Pads():
            for layer in FRONT_PAD_LAYERS:
                if not pad.GetLayerSet().Contains(layer):
                    continue
                shape = effective_pad_polygon(pad, layer)
                if shape.OutlineCount() < 1:
                    continue
                box = shape.BBox()
                min_x = to_mm(box.GetX())
                min_y = to_mm(box.GetY())
                bounds.append(
                    (
                        min_x,
                        min_y,
                        min_x + to_mm(box.GetWidth()),
                        min_y + to_mm(box.GetHeight()),
                    )
                )
        if not bounds:
            raise FootprintLibraryError("footprintにF.Cu/F.Pasteパッドがありません")
        return cls(
            min(item[0] for item in bounds),
            min(item[1] for item in bounds),
            max(item[2] for item in bounds),
            max(item[3] for item in bounds),
        )

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y


def format_footprint_id(library: str, footprint: str) -> str:
    """Footprint id ``<library>/<footprint>`` を組む.

    ``library`` は ``.pretty`` 付きのディレクトリ名（例: ``Resistor_SMD.pretty``）。
    """
    return f"{library}/{footprint}"


def parse_footprint_id(footprint_id: object) -> tuple[str, str] | None:
    """Footprint id を ``(library, footprint)`` に分解する。不正なら ``None``.

    ライブラリ名・footprint 名はファイルシステムのパス要素として安全なものだけ通す。
    """
    if not isinstance(footprint_id, str) or footprint_id.count("/") != 1:
        return None
    library, footprint = footprint_id.split("/", 1)
    if not library.endswith(".pretty"):
        return None
    if not _is_safe_path_component(library) or not _is_safe_path_component(
        footprint, suffix=_FOOTPRINT_SUFFIX
    ):
        return None
    return library, footprint


def _is_safe_path_component(value: str, *, suffix: str = "") -> bool:
    if (
        not value
        or value in {".", ".."}
        or any(separator in value for separator in ("/", "\\", "\0"))
    ):
        return False
    try:
        byte_length = len(f"{value}{suffix}".encode())
    except UnicodeEncodeError:
        return False
    return byte_length <= _MAX_FILESYSTEM_COMPONENT_BYTES


class FootprintLibrary:
    """KiCad footprint ルート（``*.pretty`` ディレクトリ群）の索引と読込.

    ルートは引数 → 環境変数 ``KICAD9_FOOTPRINT_DIR`` → KiCad 9 既定の順で決める。
    索引は初回アクセスで構築して保持する。スレッド安全性は持たない。
    """

    def __init__(self, root: Path | None = None) -> None:
        configured = os.environ.get(FOOTPRINT_ROOT_ENV)
        self._root = (
            root
            if root is not None
            else Path(configured)
            if configured
            else DEFAULT_KICAD9_FOOTPRINT_DIR
        )
        self._index: tuple[FootprintInfo, ...] | None = None

    @property
    def root(self) -> Path:
        return self._root

    @property
    def footprints(self) -> tuple[FootprintInfo, ...]:
        """ルート配下の全 footprint（ライブラリ名・footprint 名の昇順）.

        Raises:
            FootprintLibraryError: ルートが無い／読めない／footprint が 1 つも無い
        """
        if self._index is not None:
            return self._index
        try:
            if not self._root.is_dir():
                raise FootprintLibraryError(
                    f"KiCad footprint rootがありません: {self._root}"
                )
            footprints: list[FootprintInfo] = []
            for library_path in sorted(self._root.glob("*.pretty")):
                if not library_path.is_dir():
                    continue
                for path in sorted(library_path.glob(f"*{_FOOTPRINT_SUFFIX}")):
                    footprint = path.stem
                    library = library_path.name
                    family = library.removesuffix(".pretty")
                    footprints.append(
                        FootprintInfo(
                            footprint_id=format_footprint_id(library, footprint),
                            label=f"{family} / {footprint}",
                            library=library,
                            footprint=footprint,
                        )
                    )
        except OSError as error:
            raise FootprintLibraryError(
                f"KiCad footprint rootを確認できません: {self._root}"
            ) from error
        if not footprints:
            raise FootprintLibraryError(f"KiCad footprintがありません: {self._root}")
        self._index = tuple(footprints)
        return self._index

    def search(self, query: str, limit: int) -> tuple[FootprintInfo, ...]:
        """空白・記号区切りのトークン全部を含む footprint を関連度順に返す.

        空 query は空タプル。
        """
        tokens = search_tokens(query)
        if not tokens:
            return ()
        matches = [
            item
            for item in self.footprints
            if all(token in _search_text(item) for token in tokens)
        ]
        normalized_query = " ".join(tokens)
        matches.sort(key=lambda item: _search_rank(item, normalized_query))
        return tuple(matches[:limit])

    def load(self, library: str, footprint: str) -> pcbnew.FOOTPRINT:
        """Footprint を読み込む.

        Raises:
            FootprintLibraryError: ファイルが無い／読めない
        """
        library_path = self._root / library
        path = library_path / f"{footprint}{_FOOTPRINT_SUFFIX}"
        try:
            is_file = path.is_file()
        except OSError as error:
            raise FootprintLibraryError(
                f"KiCad footprintを確認できません: {path}"
            ) from error
        if not is_file:
            raise FootprintLibraryError(f"KiCad footprintがありません: {path}")
        try:
            loaded = pcbnew.FootprintLoad(str(library_path), footprint)
        except OSError as error:
            raise FootprintLibraryError(
                f"KiCad footprintを読み込めません: {path}"
            ) from error
        if loaded is None:
            raise FootprintLibraryError(f"KiCad footprintを読み込めません: {path}")
        return loaded


def search_tokens(query: str) -> tuple[str, ...]:
    """検索語を空白・記号で分割した casefold 済みトークン列（空要素は除く）."""
    return tuple(
        token for token in re.split(r"[\s_:/.-]+", query.casefold().strip()) if token
    )


def _search_text(item: FootprintInfo) -> str:
    return " ".join(
        search_tokens(f"{item.library.removesuffix('.pretty')} {item.footprint}")
    )


def _search_rank(item: FootprintInfo, normalized_query: str) -> tuple[object, ...]:
    footprint = " ".join(search_tokens(item.footprint))
    if footprint == normalized_query:
        rank = 0
    elif footprint.startswith(normalized_query):
        rank = 1
    elif normalized_query in footprint:
        rank = 2
    else:
        rank = 3
    return rank, len(item.footprint), item.library.casefold(), item.footprint.casefold()


def pad_on_any_layer(pad: pcbnew.PAD, layers: Iterable[int]) -> bool:
    """Pad が ``layers`` のいずれかに存在するか."""
    layer_set = pad.GetLayerSet()
    return any(layer_set.Contains(layer) for layer in layers)


def duplicate_footprint(footprint: pcbnew.FOOTPRINT) -> pcbnew.FOOTPRINT:
    """Footprint を複製する（親 board を持たないコピー）."""
    duplicated = footprint.Duplicate()
    if duplicated is None:
        raise FootprintLibraryError("KiCad footprintを複製できません")
    return duplicated


def single_pad_footprint(pad: pcbnew.PAD) -> pcbnew.FOOTPRINT:
    """Pad 1 個だけを原点に持つ footprint を作る（pad 番号は ``"1"``）."""
    duplicated = pad.Duplicate()
    if duplicated is None:
        raise FootprintLibraryError("KiCad padを複製できません")
    footprint = pcbnew.FOOTPRINT(None)
    duplicated.SetPosition(vector(0.0, 0.0))
    duplicated.SetNumber("1")
    footprint.Add(duplicated)
    footprint.Reference().SetVisible(False)
    footprint.Value().SetVisible(False)
    return footprint


def smd_pad_footprint(
    shape: PadShapeName,
    width_mm: float,
    height_mm: float,
    *,
    corner_radius_mm: float = 0.0,
) -> pcbnew.FOOTPRINT:
    """任意寸法の SMD pad 1 個を原点に持つ footprint を作る（pad 番号は ``"1"``）."""
    footprint = pcbnew.FOOTPRINT(None)
    pad = pcbnew.PAD(footprint)
    pad.SetNumber("1")
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetShape(_PAD_SHAPES[shape])
    pad.SetSize(vector(width_mm, height_mm))
    pad.SetPosition(vector(0.0, 0.0))
    pad.SetLayerSet(pad.SMDMask())
    if shape == "roundrect":
        pad.SetRoundRectRadiusRatio(corner_radius_mm / min(width_mm, height_mm))
    footprint.Add(pad)
    footprint.Reference().SetVisible(False)
    footprint.Value().SetVisible(False)
    return footprint


def effective_pad_polygon(pad: pcbnew.PAD, layer: int) -> pcbnew.SHAPE_POLY_SET:
    """KiCad 内部座標で表現可能な pad polygon を返す.

    Raises:
        KicadCoordinateError: 形状が内部座標の範囲を超える場合
    """
    try:
        shape = pad.GetEffectivePolygon(layer)
        box = shape.BBox()
    except OverflowError as error:
        raise KicadCoordinateError(
            "footprintのパッド形状がKiCadの座標範囲を超えています"
        ) from error
    x = box.GetX()
    y = box.GetY()
    width = box.GetWidth()
    height = box.GetHeight()
    if (
        width <= 0
        or height <= 0
        or width > KICAD_COORD_MAX_NM
        or height > KICAD_COORD_MAX_NM
        or x < KICAD_COORD_MIN_NM
        or y < KICAD_COORD_MIN_NM
        or x + width > KICAD_COORD_MAX_NM
        or y + height > KICAD_COORD_MAX_NM
    ):
        raise KicadCoordinateError(
            "footprintのパッド形状がKiCadの座標範囲を超えています"
        )
    return shape


def footprint_polygons(
    footprint: pcbnew.FOOTPRINT, layers: Sequence[tuple[LayerName, int]]
) -> tuple[tuple[LayerName, tuple[Point2d, ...]], ...]:
    """Footprint の各 pad を ``layers`` ごとの ``(レイヤ名, 頂点列)`` に展開する [mm].

    座標は footprint の現在位置・回転を反映した KiCad 座標で、基板座標には正規化しない。

    3 頂点未満の輪郭は除く。
    """
    polygons: list[tuple[LayerName, tuple[Point2d, ...]]] = []
    for pad in footprint.Pads():
        for layer_name, layer in layers:
            if not pad.GetLayerSet().Contains(layer):
                continue
            shape = effective_pad_polygon(pad, layer)
            for index in range(shape.OutlineCount()):
                points = tuple(
                    Point2d(to_mm(point.x), to_mm(point.y))
                    for point in shape.Outline(index).CPoints()
                )
                if len(points) >= 3:
                    polygons.append((layer_name, points))
    return tuple(polygons)


def pad_geometry_signature(footprint: pcbnew.FOOTPRINT) -> tuple[object, ...]:
    """単一 pad footprint の回転不変な形状シグネチャ.

    属性・ドリル・F.Cu / F.Mask / F.Paste の輪郭（1 µm 量子化、0/90/180/270 度で
    正規化した最小表現）から成り、回転同値な pad が同じ値になる。

    Raises:
        FootprintLibraryError: pad に F.Cu / F.Mask / F.Paste の形状が無い
    """
    pad = next(iter(footprint.Pads()))
    drill = pad.GetDrillSize()
    drill_dimensions = tuple(
        sorted(
            (
                round(drill.x / _SIGNATURE_QUANTUM_NM),
                round(drill.y / _SIGNATURE_QUANTUM_NM),
            )
        )
    )
    geometries = tuple(
        _rotated_geometry_signature(footprint, angle)
        for angle in (0.0, 90.0, 180.0, 270.0)
    )
    return (
        int(pad.GetAttribute()),
        int(pad.GetDrillShape()),
        drill_dimensions,
        min(geometries),
    )


def _rotated_geometry_signature(
    footprint: pcbnew.FOOTPRINT, angle: float
) -> tuple[object, ...]:
    rotated = duplicate_footprint(footprint)
    rotated.SetPosition(vector(0.0, 0.0))
    rotated.SetOrientationDegrees(angle)
    pad = next(iter(rotated.Pads()))
    raw: list[tuple[str, tuple[tuple[int, int], ...]]] = []
    all_points: list[tuple[int, int]] = []
    for layer_name, layer in _SIGNATURE_LAYERS:
        if not pad.GetLayerSet().Contains(layer):
            continue
        shape = effective_pad_polygon(pad, layer)
        for index in range(shape.OutlineCount()):
            points = tuple(
                (
                    round(point.x / _SIGNATURE_QUANTUM_NM),
                    round(point.y / _SIGNATURE_QUANTUM_NM),
                )
                for point in shape.Outline(index).CPoints()
            )
            if len(points) >= 3:
                raw.append((layer_name, points))
                all_points.extend(points)
    if not all_points:
        raise FootprintLibraryError("padにF.Cu/F.Mask/F.Paste形状がありません")
    min_x = min(point[0] for point in all_points)
    min_y = min(point[1] for point in all_points)
    normalized = [
        (
            layer_name,
            _canonical_contour(tuple((x - min_x, y - min_y) for x, y in points)),
        )
        for layer_name, points in raw
    ]
    return tuple(sorted(normalized))


def _canonical_contour(
    points: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    candidates: list[tuple[tuple[int, int], ...]] = []
    for sequence in (points, tuple(reversed(points))):
        candidates.extend(
            sequence[index:] + sequence[:index] for index in range(len(sequence))
        )
    return min(candidates)
