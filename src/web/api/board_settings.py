"""基板ごとの塗布設定の永続化ストア.

塗布設定（``PasteSettingsModel``）の基板ごとの差分だけを JSON として保存する。
JSON の形と変換規則（schema version・legacy 移行を含む）は
:mod:`pcbasm.pasting.persist` が正典で、ここはファイルの所在・atomic write・
read-modify-write の直列化・基板署名の照合だけを担う。

保存レイアウト::

    data/webui/board_settings/<board_id>.json
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from pathlib import Path

from pcbasm.config import PasteDispenser
from pcbasm.pasting.params import PasteParams
from pcbasm.pasting.persist import (
    DecodedBoardSettings,
    decode_board_settings,
    encode_board_settings,
)
from pcbasm.pasting.settings import PasteSettingsModel, find_orphans
from pcbasm.pcb import PadHierarchy
from web.api.atomic import write_text_atomic


class BoardSettingsStore:
    """基板ごとの塗布設定 JSON を読み書きするストア.

    pad PATCH 経路の read-modify-write は :meth:`update` に集約し、インスタンス
    内ロックで直列化する。**プロセス内で 1 インスタンスを共有すること**
    （HTTP 経路とジョブワーカーが別インスタンスを持つとロックが効かない）。

    :meth:`prune` と import（:meth:`model_from_doc` → :meth:`save`）は
    アップロード済み doc や呼び出し側が持つモデルからの**全量上書き**なので
    ロックの対象外で、pad PATCH と同時に走ったときの原子性は保証しない。
    """

    def __init__(self, data_dir: Path, *, legacy_root: Path | None = None) -> None:
        """ストアを初期化する.

        Args:
            data_dir: WebUI のデータディレクトリ（保存先は
                ``data_dir/board_settings``）
            legacy_root: 旧レイアウト ``data/board_settings`` の読込専用パス
        """
        self._root = data_dir / "board_settings"
        self._legacy_root = legacy_root
        self._update_lock = threading.Lock()

    def board_id(self, source_pcb: str) -> str:
        """PCB 相対パスから安定した基板 ID（SHA-256 先頭 16 hex）を導出する."""
        digest = hashlib.sha256(source_pcb.encode("utf-8")).hexdigest()
        return digest[:16]

    def load_or_init(
        self,
        source_pcb: str,
        base_config: PasteDispenser,
        *,
        board_signature: str | None = None,
    ) -> PasteSettingsModel:
        """保存済み差分を読み込む。無ければ machine.toml 由来の初期値を返す.

        ファイルが存在すれば JSON を復元する。存在しなければ ``base_config`` を
        デフォルトに据えた新規モデルを返す（**この時点では保存しない**）。
        保存時と基板署名が異なる場合も初期値に戻す。

        Raises:
            ValueError: JSON を復元できない（schema version が未知など）場合
        """
        doc_path = self._doc_path(source_pcb)
        if doc_path is None:
            return PasteSettingsModel.from_config(base_config)
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        decoded = self._decode(doc, base_config, where=str(doc_path))
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
        base_config: PasteDispenser,
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
        base_config: PasteDispenser,
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
        board_signature: str | None = None,
    ) -> PasteSettingsModel:
        """現階層に存在しない設定キー（孤児）と不在の初回パージ pad を除去して保存する."""
        orphans = find_orphans(model, hierarchy)
        known_pad_ids = {hierarchy.pad_id_for_pad(pad) for pad in hierarchy.iter_pads()}
        pruned = model.without_levels(orphans)
        if (
            pruned.initial_purge_pad_id is not None
            and pruned.initial_purge_pad_id not in known_pad_ids
        ):
            pruned = pruned.with_initial_purge_pad_id(None)
        self.save(source_pcb, pruned, board_signature=board_signature)
        return pruned

    def _decode(
        self, doc: dict, base_config: PasteDispenser, *, where: str
    ) -> DecodedBoardSettings:
        decoded, error = decode_board_settings(
            doc, base=PasteParams.from_config(base_config)
        )
        if decoded is None:
            raise ValueError(f"{error} ({where})")
        return decoded

    def _path(self, source_pcb: str) -> Path:
        return self._root / f"{self.board_id(source_pcb)}.json"

    def _doc_path(self, source_pcb: str) -> Path | None:
        path = self._path(source_pcb)
        if path.is_file():
            return path
        if self._legacy_root is None:
            return None
        legacy_path = self._legacy_root / f"{self.board_id(source_pcb)}.json"
        return legacy_path if legacy_path.is_file() else None


def _signature_mismatch(
    decoded: DecodedBoardSettings, board_signature: str | None
) -> bool:
    return (
        board_signature is not None
        and decoded.board_signature is not None
        and decoded.board_signature != board_signature
    )
