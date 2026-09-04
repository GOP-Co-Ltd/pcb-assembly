"""基板ごとの塗布設定の永続化ストア.

塗布設定（``PasteSettingsModel``）の基板ごとの差分だけを JSON として保存する。
真実の源は ``machine.toml`` の ``[paste_dispenser]`` 値で、基板 JSON は
L0（全部品）を含む明示 override だけを保持する。

保存レイアウト::

    data/webui/board_settings/<board_id>.json

JSON はネスト方式で pcbasm の階層 override dict（``settings.levels``）と
webui のメタ情報（version / source_pcb）を分離する::

    {
        "version": 1,
        "source_pcb": "<pcb_browse_root からの相対 posix パス>",
        "board_signature": "<現在の基板構成ハッシュ>",
        "settings": {"levels": [...]},
    }
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from pathlib import Path

import attrs

from pcbasm.config import PasteDispenser
from pcbasm.pasting.settings import (
    PASTE_OVERRIDE_FIELDS,
    LevelSetting,
    PasteOverride,
    PasteSettingsModel,
    base_override_from_config,
    find_orphans,
    settings_from_dict,
    settings_to_dict,
)
from pcbasm.pcb import Pad, PadHierarchy
from web.api.atomic import write_text_atomic

_SCHEMA_VERSION = 1


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
            legacy_root: 旧レイアウト ``data/board_settings`` の読込専用パス。
                machine セグメント除去により、実在しうる旧データ
                ``data/board_settings/<machine>/<board_id>.json`` とは一致しない
        """
        self._root = data_dir / "board_settings"
        self._legacy_root = legacy_root
        self._update_lock = threading.Lock()

    def board_id(self, source_pcb: str) -> str:
        """PCB 相対パスから安定した基板 ID を導出する.

        Args:
            source_pcb: ``pcb_browse_root`` からの相対 posix パス

        Returns:
            ``source_pcb`` の SHA-256 先頭 16 hex 文字
        """
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

        ファイルが存在すれば JSON を復元する。存在しなければ
        ``base_config`` をデフォルトに据えた新規モデルを返す
        （**この時点では保存しない** = 編集が入るまでファイルを作らない）。

        Args:
            source_pcb: ``pcb_browse_root`` からの相対 posix パス
            base_config: マシンのペーストディスペンサー設定（L0 初期値）

        Returns:
            復元または初期化した :class:`PasteSettingsModel`

        Raises:
            ValueError: JSON の schema version が未知の場合
        """
        path = self._path(source_pcb)
        doc_path = self._doc_path(source_pcb)
        if doc_path is not None:
            path = doc_path
            doc = json.loads(path.read_text(encoding="utf-8"))
            self._check_version(doc, path)
            saved_signature = doc.get("board_signature")
            if (
                board_signature is not None
                and isinstance(saved_signature, str)
                and saved_signature != board_signature
            ):
                return self._fresh(base_config)
            return self._model_from_settings(doc["settings"], base_config)
        return self._fresh(base_config)

    def export_doc(
        self,
        source_pcb: str,
        model: PasteSettingsModel,
        *,
        board_signature: str | None = None,
    ) -> dict:
        """ダウンロード用の保存 JSON ドキュメントを返す."""
        return self._doc(source_pcb, model, board_signature=board_signature)

    def model_from_doc(
        self,
        doc: dict,
        base_config: PasteDispenser,
        *,
        board_signature: str | None = None,
        expected_source_pcb: str | None = None,
    ) -> PasteSettingsModel:
        """アップロードされた保存 JSON を検証し、設定モデルへ復元する."""
        self._check_version(doc, Path("<uploaded board settings>"))
        if (
            expected_source_pcb is not None
            and doc.get("source_pcb") != expected_source_pcb
        ):
            raise ValueError(
                f"PCB が一致しません: {doc.get('source_pcb')} != {expected_source_pcb}"
            )
        saved_signature = doc.get("board_signature")
        if (
            board_signature is not None
            and isinstance(saved_signature, str)
            and saved_signature != board_signature
        ):
            raise ValueError("基板構成が現在の PCB と一致しません")
        return self._model_from_settings(doc["settings"], base_config)

    def _fresh(self, base_config: PasteDispenser) -> PasteSettingsModel:
        return PasteSettingsModel(
            base=base_override_from_config(base_config),
            base_enabled=True,
            levels={},
        )

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

        Args:
            source_pcb: ``pcb_browse_root`` からの相対 posix パス
            model: 保存する設定モデル
        """
        path = self._path(source_pcb)
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = self._doc(source_pcb, model, board_signature=board_signature)
        self._write_doc(path, doc)

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

        ``mutate`` には :class:`PasteSettingsModel` の純変換だけを渡すこと
        （``with_level_patch`` / ``with_pads_enabled`` /
        ``with_initial_purge_pad_id`` 等）。I/O やロックを取る処理を渡すと
        ロック保持時間が伸び、デッドロックの経路にもなる。PCB のパース・
        階層構築・入力検証はロック外で済ませてから呼ぶ。

        Args:
            source_pcb: ``pcb_browse_root`` からの相対 posix パス
            base_config: マシンのペーストディスペンサー設定（L0 初期値）
            board_signature: 現在の基板構成ハッシュ
            mutate: 読み直したモデルを受け取り、保存するモデルを返す純関数

        Returns:
            保存した :class:`PasteSettingsModel`
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
        """現階層に存在しない設定キー（孤児）を除去して保存する.

        Args:
            source_pcb: ``pcb_browse_root`` からの相対 posix パス
            model: 入力の設定モデル
            hierarchy: 現在の pad 階層

        Returns:
            孤児を除いた設定モデル（孤児が無ければ ``model`` と等価）
        """
        orphans = set(find_orphans(model, hierarchy))
        known_pad_ids = {hierarchy.pad_id_for_pad(pad) for pad in hierarchy.iter_pads()}
        purge_pad_orphan = (
            model.initial_purge_pad_id is not None
            and model.initial_purge_pad_id not in known_pad_ids
        )
        if not orphans and not purge_pad_orphan:
            self.save(source_pcb, model, board_signature=board_signature)
            return model
        levels = {
            key: setting for key, setting in model.levels.items() if key not in orphans
        }
        pruned = attrs.evolve(
            model,
            levels=levels,
            initial_purge_pad_id=(
                None if purge_pad_orphan else model.initial_purge_pad_id
            ),
        )
        self.save(source_pcb, pruned, board_signature=board_signature)
        return pruned

    def _doc(
        self,
        source_pcb: str,
        model: PasteSettingsModel,
        *,
        board_signature: str | None,
    ) -> dict:
        doc = {
            "version": _SCHEMA_VERSION,
            "source_pcb": source_pcb,
            "settings": self._settings_doc(model),
        }
        if board_signature is not None:
            doc["board_signature"] = board_signature
        return doc

    def _write_doc(self, path: Path, doc: dict) -> None:
        """保存 JSON を同一 directory 内の atomic replace で書き込む."""
        write_text_atomic(path, json.dumps(doc, ensure_ascii=False, indent=2))

    def _settings_doc(self, model: PasteSettingsModel) -> dict:
        """基板固有 override だけを保存する settings dict を返す."""
        data = settings_to_dict(model)
        result: dict[str, object] = {"levels": data["levels"]}
        if data.get("initial_purge_pad_id") is not None:
            result["initial_purge_pad_id"] = data["initial_purge_pad_id"]
        return result

    def _model_from_settings(
        self, settings: dict, base_config: PasteDispenser
    ) -> PasteSettingsModel:
        """保存 settings を現在の machine.toml デフォルトに重ねて復元する."""
        base = base_override_from_config(base_config)
        stored = settings_from_dict(settings)
        levels = dict(stored.levels)
        if ("L0",) not in levels:
            legacy_l0 = self._legacy_l0_setting(stored, base)
            if legacy_l0 is not None:
                levels[("L0",)] = legacy_l0
        return PasteSettingsModel(
            base=base,
            base_enabled=True,
            initial_purge_pad_id=stored.initial_purge_pad_id,
            levels=levels,
        )

    def _legacy_l0_setting(
        self, stored: PasteSettingsModel, current_base: PasteOverride
    ) -> LevelSetting | None:
        """旧 v1 の base/base_enabled を L0 override へ必要分だけ移行する."""
        values = {
            field: saved
            for field in PASTE_OVERRIDE_FIELDS
            if (saved := getattr(stored.base, field)) is not None
            and saved != getattr(current_base, field)
        }
        enabled = False if stored.base_enabled is False else None
        if enabled is None and not values:
            return None
        return LevelSetting(enabled=enabled, override=PasteOverride(**values))

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

    def _check_version(self, doc: dict, path: Path) -> None:
        version = doc.get("version")
        if version != _SCHEMA_VERSION:
            raise ValueError(
                f"未知の board_settings schema version です: {version} "
                f"(対応 v{_SCHEMA_VERSION}, {path})"
            )
