"""基板ごとの塗布設定（override）の保存と、選択中の基板 1 枚分の塗布設定の文脈.

設定値の基準は ``machine.toml`` の ``[paste_dispenser]`` 値で、基板 JSON には
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
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Self

import attrs
from shapely import Point, Polygon

from pcbasm.atomic import write_text_atomic
from pcbasm.board import Board
from pcbasm.config.dispenser import PasteDispenserConfig
from pcbasm.geometry import Point2d
from pcbasm.pasting.hierarchy import PadHierarchy
from pcbasm.pasting.params import PasteParams, PasteParamsPatch
from pcbasm.pasting.settings import LevelSetting, PasteSettingsModel
from pcbasm.validators import is_finite_number

BOARD_SETTINGS_SCHEMA_VERSION = 1

# 書き出し名で path 片として使えない文字（空白を含む）。日本語の基板名を置き換えないよう
# ASCII 以外は残し、Content-Disposition 側で RFC 5987 の filename* に載せる
_UNSAFE_FILENAME = re.compile(r'[\x00-\x1f/\\:*?"<>|\s]+')


class BoardOverrides:
    """基板ごとの塗布設定 JSON を読み書きするストア.

    pad PATCH 経路の read-modify-write は :meth:`update` に集約し、インスタンス
    内ロックで直列化する。プロセス内で 1 インスタンスを共有すること
    （HTTP 経路とジョブワーカーが別インスタンスを持つとロックで直列化できない）。

    :meth:`prune` と import（:meth:`model_from_doc` → :meth:`save`）は
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
        base_config: PasteDispenserConfig,
        *,
        board_signature: str | None = None,
    ) -> PasteSettingsModel:
        """保存済み差分を読み込む。無ければ machine.toml 由来の初期値を返す.

        ファイルが存在すれば JSON を復元する。存在しなければ ``base_config`` を
        デフォルトに据えた新規モデルを返す（この時点では保存しない）。
        保存時と基板署名が異なる場合も初期値に戻す。

        Raises:
            ValueError: JSON を復元できない（schema version が未知など）場合
        """
        path = self._path(source_pcb)
        if not path.is_file():
            return PasteSettingsModel.from_config(base_config)
        doc = json.loads(path.read_text(encoding="utf-8"))
        decoded = self._decode(doc, base_config, where=str(path))
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
        base_config: PasteDispenserConfig,
        *,
        board_signature: str | None = None,
        expected_source_pcb: str | None = None,
    ) -> PasteSettingsModel:
        """アップロードされた保存 JSON を検証し、設定モデルへ復元する.

        Raises:
            ValueError: 復元できない／PCB や基板構成が一致しない場合
        """
        decoded = self._decode(doc, base_config, where="<uploaded board settings>")
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
        base_config: PasteDispenserConfig,
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

    def _decode(
        self, doc: dict, base_config: PasteDispenserConfig, *, where: str
    ) -> DecodedBoardSettings:
        decoded, error = decode_board_settings(doc, base=base_config.defaults)
        if decoded is None:
            raise ValueError(f"{error} ({where})")
        return decoded

    def _path(self, source_pcb: str) -> Path:
        return self._root / f"{self.board_id(source_pcb)}.json"


@attrs.frozen
class BoardSession:
    """選択中の基板 1 枚分の塗布設定の文脈（基板・pad 階層・署名・保存済みの設定）.

    Attributes:
        source_pcb: ``pcb_browse_root`` からの相対 posix パス（保存先の鍵）
        base_config: ``machine.toml`` の ``[paste_dispenser]``
        board: 基板データ
        hierarchy: ``board`` から組んだ pad 階層
        board_signature: ``hierarchy`` の署名（保存時と違えば設定を初期値に戻す）
        model: 保存済みの塗布設定（無ければ ``base_config`` の初期値）
    """

    source_pcb: str
    base_config: PasteDispenserConfig
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
        base_config: PasteDispenserConfig,
    ) -> Self:
        """基板から階層と署名を組み、保存済みの塗布設定を読む.

        Raises:
            ValueError: 保存済みの JSON を復元できない場合
        """
        hierarchy = PadHierarchy.build(board.components, board.pads)
        signature = hierarchy.signature()
        return cls(
            source_pcb=source_pcb,
            base_config=base_config,
            board=board,
            hierarchy=hierarchy,
            board_signature=signature,
            model=overrides.load_or_init(
                source_pcb, base_config, board_signature=signature
            ),
        )


@attrs.frozen
class DecodedBoardSettings:
    """Decode 結果（モデルと保存時のメタ情報）."""

    model: PasteSettingsModel
    source_pcb: str | None
    board_signature: str | None


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

    Returns:
        ``(decoded, None)`` または ``(None, エラー文)``
    """
    version = doc.get("version")
    if version != BOARD_SETTINGS_SCHEMA_VERSION:
        return None, (
            f"未知の board_settings schema version です: {version} "
            f"(対応 v{BOARD_SETTINGS_SCHEMA_VERSION})"
        )
    settings = doc.get("settings")
    if not isinstance(settings, Mapping):
        return None, "settings がありません"

    levels = tuple(
        _level_from_entry(entry) for entry in _as_sequence(settings.get("levels"))
    )
    source_pcb = doc.get("source_pcb")
    board_signature = doc.get("board_signature")
    model = PasteSettingsModel(
        base=base,
        initial_purge_point=_point(settings.get("initial_purge_point")),
        flow_calibration_points=_points(settings.get("flow_calibration_points")),
        levels=levels,
    )
    return (
        DecodedBoardSettings(
            model=model,
            source_pcb=source_pcb if isinstance(source_pcb, str) else None,
            board_signature=(
                board_signature if isinstance(board_signature, str) else None
            ),
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


def _level_from_entry(entry: Mapping[str, Any]) -> LevelSetting:
    return LevelSetting(
        key=tuple(entry["key"]),
        enabled=entry.get("enabled"),
        patch=PasteParamsPatch.from_dict(entry.get("override") or {}),
    )


def _as_sequence(value: object) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, str) else []


def _points(value: object) -> tuple[Point2d, ...]:
    """保存値 ``[[x, y], ...]`` を Point2d の並びへ戻す.

    形が違う要素は :func:`_point` と同じく通知せずに捨てる。
    """
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(point for entry in value if (point := _point(entry)) is not None)


def _point(value: object) -> Point2d | None:
    """保存値 ``[x, y]`` を Point2d へ戻す（形が違えば ``None``）.

    不正な保存内容でページを開けなくならないよう、pad id と同じく通知せずに捨てる。
    """
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return None
    if len(value) != 2:
        return None
    x, y = value
    if not is_finite_number(x) or not is_finite_number(y):
        return None
    return Point2d(float(x), float(y))


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
