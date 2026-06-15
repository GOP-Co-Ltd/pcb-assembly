"""Pad の階層 group-by ロジック.

塗布設定の override 解決（``pcbasm.pasting.settings``）と UI の表示が
同一の階層を共有するため、装置非依存の純ロジックとしてここに集約する。

階層は L0–L4 の5段で、下位ほど具体的:

- **L0** ``("L0",)`` 全部品デフォルト
- **L1** ``("L1", package)`` 同規格パーツ（``Component.package``）
- **L2** ``("L2", designator)`` 各コンポーネント
- **L3** ``("L3", designator, shape_label)`` designator 内の同形状 pad
- **L4** ``("L4", designator, pad_number)`` 個々の pad

L3 の形状分類は :class:`PadShapeKey` による。回転配置された同型 pad が
同一キーになるよう、最小回転外接矩形の短辺/長辺で正規化する。
"""

from collections.abc import Iterator, Sequence

import attrs
from shapely import Polygon

from pcbasm.pcb.board import Component, Pad

# 階層ノードのキー（例 ("L3", "U1", "0.50x0.90mm")）
HierKey = tuple[str, ...]

# 形状量子化の既定単位（mm／mm²）
DEFAULT_SHAPE_QUANTUM = 0.01


def _mrr_edge_lengths(polygon: Polygon) -> tuple[float, float]:
    """最小回転外接矩形の (短辺, 長辺) を返す.

    回転配置された同型 pad が同じ値になるよう、辺長を短辺/長辺に 正規化する。矩形が縮退している場合は (0.0, 0.0) を返す。
    """
    mrr = polygon.minimum_rotated_rectangle
    if not isinstance(mrr, Polygon) or mrr.is_empty:
        return (0.0, 0.0)
    coords = list(mrr.exterior.coords)
    if len(coords) < 5:
        return (0.0, 0.0)
    # 連続する2頂点間の距離が辺長。矩形なので隣接2辺で短辺/長辺が決まる。
    side1 = (
        (coords[1][0] - coords[0][0]) ** 2 + (coords[1][1] - coords[0][1]) ** 2
    ) ** 0.5
    side2 = (
        (coords[2][0] - coords[1][0]) ** 2 + (coords[2][1] - coords[1][1]) ** 2
    ) ** 0.5
    return (min(side1, side2), max(side1, side2))


@attrs.frozen
class PadShapeKey:
    """Pad の形状・サイズ分類キー.

    熱パッドと信号ピンを区別するため、量子化した面積と回転不変の
    最小外接矩形短辺/長辺、カスタム形状フラグで pad をグループ化する。

    Attributes:
        area_q: ``round(pad.area / quantum)``（量子化面積）
        short_q: 最小回転外接矩形の短辺 / quantum を round した値
        long_q: 同 長辺 / quantum を round した値
        is_custom_shape: カスタム形状かどうか
    """

    area_q: int
    short_q: int
    long_q: int
    is_custom_shape: bool

    @classmethod
    def of(cls, pad: Pad, *, quantum: float = DEFAULT_SHAPE_QUANTUM) -> "PadShapeKey":
        """Pad から形状キーを生成する.

        Args:
            pad: 対象 pad
            quantum: 量子化単位（mm／mm²、既定 0.01）

        Returns:
            形状キー
        """
        short, long = _mrr_edge_lengths(pad.polygon)
        return cls(
            area_q=round(pad.area / quantum),
            short_q=round(short / quantum),
            long_q=round(long / quantum),
            is_custom_shape=pad.is_custom_shape,
        )

    @property
    def label(self) -> str:
        """人間可読なラベル（UI 表示用、例 ``"0.50x0.90mm"``）.

        カスタム形状の場合は短辺×長辺の外接寸法に加え面積を併記する。
        """
        # PadShapeKey は量子化値しか保持しないため、既定 quantum を仮定して
        # 表示用の寸法を復元する（label はあくまで人間向けの近似表示）。
        short_mm = self.short_q * DEFAULT_SHAPE_QUANTUM
        long_mm = self.long_q * DEFAULT_SHAPE_QUANTUM
        if self.is_custom_shape:
            area_mm2 = self.area_q * DEFAULT_SHAPE_QUANTUM
            return f"custom {short_mm:.2f}x{long_mm:.2f}mm ({area_mm2:.2f}mm2)"
        return f"{short_mm:.2f}x{long_mm:.2f}mm"


@attrs.frozen
class PadHierarchyNode:
    """階層ツリーの1ノード.

    Attributes:
        level: 階層レベル（0..4）
        key: 階層キー
        label: UI 表示用ラベル
        pads: このノード配下の全 pad（葉まで再帰的に含む）
        children: 子ノード（最具体の L4 では空）
    """

    level: int
    key: HierKey
    label: str
    pads: tuple[Pad, ...]
    children: tuple["PadHierarchyNode", ...]


@attrs.frozen
class PadHierarchy:
    """Pad の L0–L4 階層ツリー.

    Attributes:
        root: L0 ルートノード
    """

    root: PadHierarchyNode
    # (designator, pad_number) -> L0..L4 の5キー。build 時に構築する。
    _keys_by_pad: dict[tuple[str, str], tuple[HierKey, ...]] = attrs.field(
        factory=dict, eq=False, alias="keys_by_pad"
    )

    def iter_pads(self) -> Iterator[Pad]:
        """階層に含まれる全 pad を反復する（ルート配下の順）."""
        yield from self.root.pads

    def node_keys_for_pad(self, pad: Pad) -> list[HierKey]:
        """Pad が属する L0–L4 の5キーを返す.

        Args:
            pad: 対象 pad（``designator`` と ``pad_number`` で照合）

        Returns:
            ``[L0, L1, L2, L3, L4]`` の5要素

        Raises:
            KeyError: pad がこの階層に存在しない場合
        """
        return list(self._keys_by_pad[(pad.designator, pad.pad_number)])

    def all_keys(self) -> set[HierKey]:
        """階層に存在する全ノードキーの集合を返す."""
        keys: set[HierKey] = set()
        for pad_keys in self._keys_by_pad.values():
            keys.update(pad_keys)
        return keys


def _hier_keys_for(pad: Pad, package: str, shape_label: str) -> tuple[HierKey, ...]:
    """1 pad に対する L0–L4 のキー列を構築する."""
    return (
        ("L0",),
        ("L1", package),
        ("L2", pad.designator),
        ("L3", pad.designator, shape_label),
        ("L4", pad.designator, pad.pad_number),
    )


# 各レベルのノードラベル（key とその pad から導出）。
# L0 は固定文字列、L4 は ``designator.pad_number``、それ以外は key の末尾。
def _node_label(level: int, key: HierKey, pad: Pad) -> str:
    if level == 0:
        return "全部品"
    if level == 4:
        return f"{pad.designator}.{pad.pad_number}"
    return key[-1]


def build_pad_hierarchy(
    components: Sequence[Component],
    pads: Sequence[Pad],
    *,
    shape_quantum: float = 0.01,
) -> PadHierarchy:
    """部品と pad から L0–L4 階層ツリーを構築する.

    pad の package は designator で ``components`` から引く。対応する
    Component が無い pad は階層から除外する。

    Args:
        components: 対象部品列
        pads: 対象 pad 列
        shape_quantum: 形状量子化単位（mm／mm²、既定 0.01）

    Returns:
        構築した階層
    """
    package_by_designator = {c.designator: c.package for c in components}

    keys_by_pad: dict[tuple[str, str], tuple[HierKey, ...]] = {}
    ordered_pads: list[Pad] = []
    for pad in pads:
        package = package_by_designator.get(pad.designator)
        if package is None:
            # 対応する Component が無い pad は除外
            continue
        shape_label = PadShapeKey.of(pad, quantum=shape_quantum).label
        keys_by_pad[(pad.designator, pad.pad_number)] = _hier_keys_for(
            pad, package, shape_label
        )
        ordered_pads.append(pad)

    root = _build_node(0, ("L0",), ordered_pads, keys_by_pad)
    return PadHierarchy(root=root, keys_by_pad=keys_by_pad)


def _build_node(
    level: int,
    key: HierKey,
    pads: list[Pad],
    keys_by_pad: dict[tuple[str, str], tuple[HierKey, ...]],
) -> PadHierarchyNode:
    """``key`` のノードを構築し、配下を次レベルの key で再帰グループ化する.

    L4（葉）では子を持たない。出現順は ``pads`` の順をそのまま使う。
    """
    children: tuple[PadHierarchyNode, ...] = ()
    if level < 4:
        groups: dict[HierKey, list[Pad]] = {}
        for pad in pads:
            child_key = keys_by_pad[(pad.designator, pad.pad_number)][level + 1]
            groups.setdefault(child_key, []).append(pad)
        children = tuple(
            _build_node(level + 1, child_key, child_pads, keys_by_pad)
            for child_key, child_pads in groups.items()
        )
    return PadHierarchyNode(
        level=level,
        key=key,
        label=_node_label(level, key, pads[0]) if pads else "全部品",
        pads=tuple(pads),
        children=children,
    )
