"""基板ごとの塗布設定（override）: L0–L4 階層 override の解決と、基板ごとの保存.

塗布実行と UI プレビューが同一の解決規則を共有するため、装置非依存の
純ロジックとしてここに集約する。

- :class:`PasteSettingsModel.base` が ``machine.toml`` 由来の全項目確定値
  （:class:`~pcbasm.pasting.params.PasteParams`）
- :class:`PasteSettingsModel.levels` が L0–L4 の疎な :class:`LevelSetting` 列
- 解決時は各 pad で L0→L4 を辿り、非 ``None`` 項目で上書きする（最も具体的な階層の値を採用する）。
  ``enabled`` も同じ規則で、明示の無い pad は有効

設定値の基準は ``machine.toml`` の ``[solder]`` 値で、基板 JSON には
明示 override（L0 を含む ``levels``）と基板上の座標設定だけを保持する。
ダウンロード時のファイル名は :func:`board_settings_export_filename` が決める
（保存先の名前は board_id で、ダウンロード時のファイル名とは異なる）。

保存レイアウト::

    data/webui/board_settings/<board_id>.json

保存形式（schema v1）::

    {
        "version": 1,
        "source_pcb": "<pcb_browse_root からの相対 posix パス>",
        "board_signature": "<基板構成ハッシュ>",   # 任意
        "settings": {
            "levels": [{"key": ["L2", "U1"], "enabled": null, "override": {...}}, ...],
            "initial_purge_point": [12.5, 8.0],            # 任意（未設定 = 面積最大 pad）
            "flow_calibration_points": [[20.0, 8.0], ...]  # 任意（空 = 補正しない）
        }
    }

版の扱い: キー／型が変わるときだけ ``BOARD_SETTINGS_SCHEMA_VERSION`` を上げ、
:func:`decode_board_settings` が版で分岐して旧版を純関数で新版 dict に写す。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self

import attrs
from shapely import Point, Polygon

from pcbasm.atomic import write_text_atomic
from pcbasm.board import Board, Pad
from pcbasm.config.solder import SolderConfig
from pcbasm.document import omittable, structure_document
from pcbasm.geometry import Point2d
from pcbasm.pasting.hierarchy import HierKey, PadHierarchy, PadHierarchyNode
from pcbasm.pasting.params import (
    PASTE_PARAM_NAMES,
    PasteParams,
    PasteParamsPatch,
    PasteParamValue,
    validate_param_values,
)

BOARD_SETTINGS_SCHEMA_VERSION = 1
_LABEL = "基板の塗布設定"

# 書き出し名で path 片として使えない文字（空白を含む）。日本語の基板名を置き換えないよう
# ASCII 以外は残し、Content-Disposition 側で RFC 5987 の filename* に載せる
_UNSAFE_FILENAME = re.compile(r'[\x00-\x1f/\\:*?"<>|\s]+')


@attrs.frozen
class LevelSetting:
    """1 階層ノードに紐づく明示設定.

    Attributes:
        key: 階層キー（L0–L4）
        enabled: 有効/無効（``None`` = 継承）
        patch: パラメータの差分上書き
    """

    key: HierKey
    enabled: bool | None = None
    patch: PasteParamsPatch = PasteParamsPatch()

    @property
    def is_empty(self) -> bool:
        return self.enabled is None and self.patch.is_empty


@attrs.frozen
class ResolvedSetting:
    """1 pad / 1 ノードに対する解決済み設定."""

    enabled: bool
    params: PasteParams


@attrs.frozen
class PasteSettingsModel:
    """基板 1 枚分の塗布設定モデル.

    Attributes:
        base: machine.toml 由来のデフォルト（全項目確定）
        initial_purge_point: 初回パージの座標（``None`` = 面積最大の有効 pad の中心）
        flow_calibration_points: 運転時補正の測定位置（塗る順）
            （空 = 未設定。補正しない）
        levels: L0–L4 の疎な明示設定（キーは重複しない）
    """

    base: PasteParams
    initial_purge_point: Point2d | None = None
    flow_calibration_points: tuple[Point2d, ...] = ()
    levels: tuple[LevelSetting, ...] = ()

    @classmethod
    def from_config(cls, config: SolderConfig) -> Self:
        """Override の無い初期モデル（machine.toml デフォルトのみ）."""
        return cls(base=config.defaults)

    def level(self, key: HierKey) -> LevelSetting | None:
        for setting in self.levels:
            if setting.key == key:
                return setting
        return None

    def with_level_patch(
        self,
        key: HierKey,
        *,
        values: Mapping[str, PasteParamValue] | None = None,
        clear: Sequence[str] = (),
        enabled: bool | None = None,
        enabled_sent: bool = False,
    ) -> Self:
        """1 ノードの明示設定を upsert / clear した新しいモデルを返す.

        - ``values`` の項目で patch を上書きし、``clear`` の項目を継承に戻す
        - ``enabled_sent`` が False のときは既存の ``enabled`` を保持する
        - 結果が空（``enabled`` も patch も無し）ならそのノードを ``levels`` から除く
        """
        current = self.level(key) or LevelSetting(key)
        updated = LevelSetting(
            key,
            enabled=enabled if enabled_sent else current.enabled,
            patch=current.patch.updated(values, clear=clear),
        )
        if updated.is_empty:
            levels = tuple(s for s in self.levels if s.key != key)
        elif self.level(key) is None:
            levels = (*self.levels, updated)
        else:
            levels = tuple(updated if s.key == key else s for s in self.levels)
        return attrs.evolve(self, levels=levels)

    def with_pads_enabled(self, l4_keys: Iterable[HierKey], *, enabled: bool) -> Self:
        """指定 L4 ノード群の ``enabled`` を一括設定した新モデルを返す."""
        model = self
        for key in l4_keys:
            model = model.with_level_patch(key, enabled=enabled, enabled_sent=True)
        return model

    def with_initial_purge_point(self, point: Point2d | None) -> Self:
        """初回パージの座標を設定した新モデルを返す（``None`` で自動へ戻す）."""
        return attrs.evolve(self, initial_purge_point=point)

    def with_flow_calibration_points(self, points: Sequence[Point2d]) -> Self:
        """運転時補正の測定位置を設定した新モデルを返す.

        空の並びは未設定で、運転時の補正を行わない。
        """
        return attrs.evolve(self, flow_calibration_points=tuple(points))

    def without_levels(self, keys: Iterable[HierKey]) -> Self:
        """指定キーの明示設定を除いた新モデルを返す."""
        drop = set(keys)
        return attrs.evolve(
            self, levels=tuple(s for s in self.levels if s.key not in drop)
        )

    def find_orphans(self, hierarchy: PadHierarchy) -> list[HierKey]:
        """現階層に存在しない設定キー（孤児）を返す."""
        existing = hierarchy.all_keys()
        return [setting.key for setting in self.levels if setting.key not in existing]


def _resolve_chain(
    keys: Iterable[HierKey], model: PasteSettingsModel
) -> ResolvedSetting:
    """L0 から最具体へ並んだ ``keys`` の明示設定を順に重ねて解決する."""
    params = model.base
    enabled = True
    for key in keys:
        setting = model.level(key)
        if setting is None:
            continue
        params = params.patched(setting.patch)
        if setting.enabled is not None:
            enabled = setting.enabled
    return ResolvedSetting(enabled=enabled, params=params)


def resolve_pad_settings(
    hierarchy: PadHierarchy, model: PasteSettingsModel
) -> dict[str, ResolvedSetting]:
    """各 pad の解決済み設定を返す（``hierarchy.node_keys_for_pad`` の L0→L4 順に重ねる）."""
    return {
        pad.ref: _resolve_chain(hierarchy.node_keys_for_pad(pad), model)
        for pad in hierarchy.iter_pads()
    }


def resolve_node_settings(
    hierarchy: PadHierarchy, model: PasteSettingsModel
) -> dict[HierKey, ResolvedSetting]:
    """各階層ノードの解決済み設定を返す（ルートから自ノードまでのパスで重ねる）.

    UI の階層表が各ノード行に解決済み値を表示するための算出。
    """
    result: dict[HierKey, ResolvedSetting] = {}
    for path in _node_paths(hierarchy.root, ()):
        result[path[-1]] = _resolve_chain(path, model)
    return result


def _node_paths(
    node: PadHierarchyNode, ancestors: tuple[HierKey, ...]
) -> Iterator[tuple[HierKey, ...]]:
    path = (*ancestors, node.key)
    yield path
    for child in node.children:
        yield from _node_paths(child, path)


@attrs.frozen
class OwnOverrideSummary:
    """ノード自身の明示設定の集計（UI の行バッジ表示用）.

    Attributes:
        enabled: enabled を明示しているか
        fields: 上書きされたフィールド名（UI 列順）
        count: fields + enabled の総数
    """

    enabled: bool
    fields: tuple[str, ...]
    count: int


@attrs.frozen
class DescendantOverrideSummary:
    """子孫ノード（自身は含まない）の明示設定の集計（UI の継承マーカー表示用）.

    Attributes:
        enabled_count: enabled を明示した子孫ノード数
        field_counts: フィールドごとの子孫 override 数（UI 列順、0 も含む）
        fields: 子孫に override があるフィールド名（UI 列順）
        node_count: 明示設定を持つ子孫ノード数
        count: 子孫の明示項目の総数
    """

    enabled_count: int
    field_counts: dict[str, int]
    fields: tuple[str, ...]
    node_count: int
    count: int


def own_override_summary(setting: LevelSetting | None) -> OwnOverrideSummary:
    """ノード自身の明示設定を集計する（継承は数えない）."""
    enabled = setting is not None and setting.enabled is not None
    fields = tuple(setting.patch.to_dict()) if setting is not None else ()
    return OwnOverrideSummary(
        enabled=enabled, fields=fields, count=len(fields) + (1 if enabled else 0)
    )


def descendant_override_summary(
    node: PadHierarchyNode, model: PasteSettingsModel
) -> DescendantOverrideSummary:
    """``node`` の子孫（自身は含めない）の明示設定を集計する."""
    field_counts = {name: 0 for name in PASTE_PARAM_NAMES}
    enabled_count = 0
    node_count = 0
    total = 0
    for descendant in _iter_descendants(node):
        setting = model.level(descendant.key)
        if setting is None or setting.is_empty:
            continue
        own = own_override_summary(setting)
        if own.enabled:
            enabled_count += 1
        for name in own.fields:
            field_counts[name] += 1
        total += own.count
        node_count += 1
    return DescendantOverrideSummary(
        enabled_count=enabled_count,
        field_counts=field_counts,
        fields=tuple(name for name in PASTE_PARAM_NAMES if field_counts[name] > 0),
        node_count=node_count,
        count=total,
    )


def _iter_descendants(node: PadHierarchyNode) -> Iterator[PadHierarchyNode]:
    for child in node.children:
        yield child
        yield from _iter_descendants(child)


class StoredBoardSettingsError(ValueError):
    """保存済みの基板の塗布設定を復元できない（理由とファイルの場所を運転者へ返す）.

    ``source_pcb`` は読めなかった基板で、画面はこの基板に限って初期化を出す。
    """

    def __init__(self, message: str, *, source_pcb: str) -> None:
        super().__init__(message)
        self.source_pcb = source_pcb


class BoardOverrides:
    """基板ごとの塗布設定 JSON を読み書きするストア.

    pad PATCH 経路の read-modify-write は :meth:`update` に集約し、インスタンス
    内ロックで直列化する。プロセス内で 1 インスタンスを共有すること
    （HTTP 経路とジョブワーカーが別インスタンスを持つとロックで直列化できない）。

    :meth:`prune`、import（:meth:`model_from_doc` → :meth:`save`）、初期化（:meth:`save`）は
    アップロード済み doc や呼び出し側が持つモデルからの全量上書きなので
    ロックの対象外で、pad PATCH と同時に走ったときの原子性は保証しない。
    """

    def __init__(self, data_dir: Path) -> None:
        """ストアを初期化する.

        Args:
            data_dir: WebUI のデータディレクトリ（保存先は
                ``data_dir/board_settings``）
        """
        self._root = data_dir / "board_settings"
        self._update_lock = threading.Lock()

    def board_id(self, source_pcb: str) -> str:
        """PCB 相対パスから安定した基板 ID（SHA-256 先頭 16 hex）を導出する."""
        digest = hashlib.sha256(source_pcb.encode("utf-8")).hexdigest()
        return digest[:16]

    def load_or_init(
        self,
        source_pcb: str,
        base_config: SolderConfig,
        *,
        board_signature: str | None = None,
    ) -> PasteSettingsModel:
        """保存済み差分を読み込む。無ければ machine.toml 由来の初期値を返す.

        ファイルが存在すれば JSON を復元する。存在しなければ ``base_config`` を
        デフォルトに据えた新規モデルを返す（この時点では保存しない）。
        保存時と基板署名が異なる場合も初期値に戻す。

        Raises:
            StoredBoardSettingsError: 保存済みの JSON を復元できない場合
        """
        path = self._path(source_pcb)
        if not path.is_file():
            return PasteSettingsModel.from_config(base_config)
        doc = json.loads(path.read_text(encoding="utf-8"))
        decoded, error = decode_board_settings(doc, base=base_config.defaults)
        if decoded is None:
            raise StoredBoardSettingsError(
                f"保存済みの{_LABEL}を読めません: {error} ({path})",
                source_pcb=source_pcb,
            )
        if _signature_mismatch(decoded, board_signature):
            return PasteSettingsModel.from_config(base_config)
        return decoded.model

    def export_doc(
        self,
        source_pcb: str,
        model: PasteSettingsModel,
        *,
        board_signature: str | None = None,
    ) -> dict:
        """ダウンロード用の保存 JSON ドキュメントを返す."""
        return encode_board_settings(
            model, source_pcb=source_pcb, board_signature=board_signature
        )

    def model_from_doc(
        self,
        doc: dict,
        base_config: SolderConfig,
        *,
        board_signature: str | None = None,
        expected_source_pcb: str | None = None,
    ) -> PasteSettingsModel:
        """アップロードされた保存 JSON を検証し、設定モデルへ復元する.

        Raises:
            ValueError: 復元できない／PCB や基板構成が一致しない場合
        """
        decoded, error = decode_board_settings(doc, base=base_config.defaults)
        if decoded is None:
            raise ValueError(error)
        if (
            expected_source_pcb is not None
            and decoded.source_pcb != expected_source_pcb
        ):
            raise ValueError(
                f"PCB が一致しません: {decoded.source_pcb} != {expected_source_pcb}"
            )
        if _signature_mismatch(decoded, board_signature):
            raise ValueError("基板構成が現在の PCB と一致しません")
        return decoded.model

    def save(
        self,
        source_pcb: str,
        model: PasteSettingsModel,
        *,
        board_signature: str | None = None,
    ) -> None:
        """設定モデルを即時保存する（単発上書き専用）.

        保存済み内容を読んで変換する編集（read-modify-write）には使わない。
        並行編集が互いを上書きするため :meth:`update` を使うこと。
        """
        path = self._path(source_pcb)
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = encode_board_settings(
            model, source_pcb=source_pcb, board_signature=board_signature
        )
        write_text_atomic(path, json.dumps(doc, ensure_ascii=False, indent=2))

    def update(
        self,
        source_pcb: str,
        base_config: SolderConfig,
        *,
        board_signature: str | None = None,
        mutate: Callable[[PasteSettingsModel], PasteSettingsModel],
    ) -> PasteSettingsModel:
        """保存済み設定を読み直して変換し、保存した結果を返す.

        ロック内で「再 :meth:`load_or_init` → ``mutate`` → atomic write」を
        行うため、同時編集でも先行の変更が失われない（lost update の排除）。

        ``mutate`` には :class:`PasteSettingsModel` の純変換だけを渡すこと。
        I/O やロックを取る処理を渡すとロック保持時間が伸び、デッドロックの経路にもなる。
        """
        with self._update_lock:
            current = self.load_or_init(
                source_pcb, base_config, board_signature=board_signature
            )
            model = mutate(current)
            self.save(source_pcb, model, board_signature=board_signature)
            return model

    def prune(
        self,
        source_pcb: str,
        model: PasteSettingsModel,
        hierarchy: PadHierarchy,
        *,
        outline: Polygon,
        board_signature: str | None = None,
    ) -> PasteSettingsModel:
        """現基板に無い設定（孤児キーと外形外の座標）を除いて保存する."""
        pruned = model.without_levels(model.find_orphans(hierarchy))
        if _outside(pruned.initial_purge_point, outline):
            pruned = pruned.with_initial_purge_point(None)
        kept = [
            point
            for point in pruned.flow_calibration_points
            if not _outside(point, outline)
        ]
        if len(kept) != len(pruned.flow_calibration_points):
            pruned = pruned.with_flow_calibration_points(kept)
        self.save(source_pcb, pruned, board_signature=board_signature)
        return pruned

    def _path(self, source_pcb: str) -> Path:
        return self._root / f"{self.board_id(source_pcb)}.json"


@attrs.frozen
class BoardSession:
    """選択中の基板 1 枚分の塗布設定の文脈（基板・pad 階層・署名・保存済みの設定）.

    Attributes:
        source_pcb: ``pcb_browse_root`` からの相対 posix パス（保存先の鍵）
        base_config: ``machine.toml`` の ``[solder]``
        board: 基板データ
        hierarchy: ``board`` から組んだ pad 階層
        board_signature: ``hierarchy`` の署名（保存時と違えば設定を初期値に戻す）
        model: 保存済みの塗布設定（無ければ ``base_config`` の初期値）
    """

    source_pcb: str
    base_config: SolderConfig
    board: Board
    hierarchy: PadHierarchy
    board_signature: str
    model: PasteSettingsModel

    @classmethod
    def load(
        cls,
        overrides: BoardOverrides,
        source_pcb: str,
        board: Board,
        base_config: SolderConfig,
    ) -> Self:
        """基板から階層と署名を組み、保存済みの塗布設定を読む.

        Raises:
            StoredBoardSettingsError: 保存済みの JSON を復元できない場合
        """
        session = cls.initial(source_pcb, board, base_config)
        return attrs.evolve(
            session,
            model=overrides.load_or_init(
                source_pcb, base_config, board_signature=session.board_signature
            ),
        )

    @classmethod
    def initial(cls, source_pcb: str, board: Board, base_config: SolderConfig) -> Self:
        """保存済みの設定を読まずに、``base_config`` の初期値で組む（設定を丸ごと上書きするときに使う）."""
        hierarchy = PadHierarchy.build(board.components, board.pads)
        return cls(
            source_pcb=source_pcb,
            base_config=base_config,
            board=board,
            hierarchy=hierarchy,
            board_signature=hierarchy.signature(),
            model=PasteSettingsModel.from_config(base_config),
        )


@attrs.frozen
class DecodedBoardSettings:
    """Decode 結果（モデルと保存時のメタ情報）."""

    model: PasteSettingsModel
    source_pcb: str
    board_signature: str | None


@attrs.frozen
class _StoredLevel:
    key: HierKey
    enabled: bool | None
    override: dict[str, PasteParamValue]


@attrs.frozen
class _StoredSettings:
    levels: tuple[_StoredLevel, ...]
    initial_purge_point: tuple[float, float] | None = omittable(None)
    flow_calibration_points: tuple[tuple[float, float], ...] = omittable(())


@attrs.frozen
class _StoredBoardSettings:
    """保存 JSON（schema v1）の形."""

    version: Literal[1]
    source_pcb: str
    settings: _StoredSettings
    board_signature: str | None = omittable(None)


def encode_board_settings(
    model: PasteSettingsModel,
    *,
    source_pcb: str,
    board_signature: str | None = None,
) -> dict[str, Any]:
    """モデルを保存 JSON ドキュメント（dict）に変換する."""
    settings: dict[str, Any] = {
        "levels": [
            {
                "key": list(setting.key),
                "enabled": setting.enabled,
                "override": setting.patch.to_dict(),
            }
            for setting in model.levels
        ]
    }
    if model.initial_purge_point is not None:
        point = model.initial_purge_point
        settings["initial_purge_point"] = [point.x, point.y]
    if model.flow_calibration_points:
        settings["flow_calibration_points"] = [
            [point.x, point.y] for point in model.flow_calibration_points
        ]
    doc: dict[str, Any] = {
        "version": BOARD_SETTINGS_SCHEMA_VERSION,
        "source_pcb": source_pcb,
        "settings": settings,
    }
    if board_signature is not None:
        doc["board_signature"] = board_signature
    return doc


def decode_board_settings(
    doc: Mapping[str, Any], *, base: PasteParams
) -> tuple[DecodedBoardSettings | None, str | None]:
    """保存 JSON ドキュメントを現在の machine.toml デフォルト ``base`` に重ねて復元する.

    形式（未知 key・欠落 key・型）と override の値を strict に検証する。
    書き出した JSON をブラウザで読み込むと ``1.0`` が ``1`` になるので、float の欄に int を受ける。

    Returns:
        ``(decoded, None)`` または ``(None, エラー文)``
    """
    version = doc.get("version")
    if version != BOARD_SETTINGS_SCHEMA_VERSION:
        return None, f"未対応の{_LABEL}です: version={version!r}"
    invalid = f"{_LABEL} schema v{BOARD_SETTINGS_SCHEMA_VERSION} が不正です"
    try:
        stored = structure_document(doc, _StoredBoardSettings, int_as_float=True)
    except Exception as error:
        return None, f"{invalid}: {error}"
    settings = stored.settings
    for level in settings.levels:
        if (message := validate_param_values(level.override)) is not None:
            return None, f"{invalid}: {message} (key={list(level.key)})"
    purge = settings.initial_purge_point
    model = PasteSettingsModel(
        base=base,
        initial_purge_point=None if purge is None else Point2d(*purge),
        flow_calibration_points=tuple(
            Point2d(*point) for point in settings.flow_calibration_points
        ),
        levels=tuple(
            LevelSetting(
                key=level.key,
                enabled=level.enabled,
                patch=PasteParamsPatch(**level.override),
            )
            for level in settings.levels
        ),
    )
    return (
        DecodedBoardSettings(
            model=model,
            source_pcb=stored.source_pcb,
            board_signature=stored.board_signature,
        ),
        None,
    )


def board_settings_export_filename(
    source_pcb: str, created_at: datetime | None = None
) -> str:
    """書き出し用のファイル名 ``<基板名>-paste-overrides-<YYYYMMDDTHHMMSS>.json`` を作る.

    日時書式はリポジトリの他の書き出し（塗布量較正・データセット）と揃える。

    Args:
        source_pcb: ``pcb_browse_root`` からの相対 posix パス
        created_at: 書き出し時刻（既定は現在時刻）
    """
    stem = _UNSAFE_FILENAME.sub("-", PurePosixPath(source_pcb).stem).strip("-")
    moment = (created_at or datetime.now()).strftime("%Y%m%dT%H%M%S")
    return f"{stem or 'board'}-paste-overrides-{moment}.json"


def _signature_mismatch(
    decoded: DecodedBoardSettings, board_signature: str | None
) -> bool:
    return (
        board_signature is not None
        and decoded.board_signature is not None
        and decoded.board_signature != board_signature
    )


def _outside(point: Point2d | None, outline: Polygon) -> bool:
    """設定済みの座標が基板外形の外にあるか（未設定は外扱いしない）."""
    return point is not None and not outline.covers(Point(point.x, point.y))
