"""基板ごとの塗布設定の永続化ストア.

塗布設定（``PasteSettingsModel``）を基板ごとの JSON として保存する。
真実の源は基板ごとの JSON で、初回は ``machine.toml`` の
``[paste_dispenser]`` 値で L0（base）を埋め、以後は独立して再現性を保つ。

保存レイアウト::

    data/webui/board_settings/<machine>/<board_id>.json

JSON はネスト方式で pcbasm のモデル dict（``settings``）と webui の
メタ情報（version / source_pcb / machine）を分離する::

    {
        "version": 1,
        "source_pcb": "<pcb_browse_root からの相対 posix パス>",
        "machine": "<マシン名>",
        "board_signature": "<現在の基板構成ハッシュ>",
        "settings": <settings_to_dict(model)>,
    }
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import attrs

from pcbasm.config import PasteDispenser
from pcbasm.pasting import (
    PasteSettingsModel,
    base_override_from_config,
    find_orphans,
    settings_from_dict,
    settings_to_dict,
)
from pcbasm.pcb import PadHierarchy

_SCHEMA_VERSION = 1


def board_signature(hierarchy: PadHierarchy) -> str:
    """Pad 階層から基板構成の安定ハッシュを作る."""
    records = []
    for pad in hierarchy.iter_pads():
        records.append(
            {
                "id": f"{pad.designator}.{pad.pad_number}",
                "layer": pad.layer.value,
                "node_keys": [list(key) for key in hierarchy.node_keys_for_pad(pad)],
                "polygon": [[x, y] for x, y in pad.polygon.exterior.coords],
            }
        )
    payload = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BoardSettingsStore:
    """基板ごとの塗布設定 JSON を読み書きするストア."""

    def __init__(self, data_dir: Path, *, legacy_root: Path | None = None) -> None:
        """ストアを初期化する.

        Args:
            data_dir: WebUI のデータディレクトリ（保存先は
                ``data_dir/board_settings``）
            legacy_root: 旧レイアウト ``data/board_settings`` の読込専用パス
        """
        self._root = data_dir / "board_settings"
        self._legacy_root = legacy_root

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
        machine: str,
        source_pcb: str,
        base_config: PasteDispenser,
        *,
        board_signature: str | None = None,
    ) -> PasteSettingsModel:
        """保存済み設定を読み込む。無ければ machine.toml 由来の初期値を返す.

        ファイルが存在すれば JSON を復元する。存在しなければ
        ``base_config`` を L0 デフォルトに据えた新規モデルを返す
        （**この時点では保存しない** = 編集が入るまでファイルを作らない）。

        Args:
            machine: マシン名
            source_pcb: ``pcb_browse_root`` からの相対 posix パス
            base_config: マシンのペーストディスペンサー設定（L0 初期値）

        Returns:
            復元または初期化した :class:`PasteSettingsModel`

        Raises:
            ValueError: JSON の schema version が未知の場合
        """
        path = self._path(machine, source_pcb)
        doc_path = self._doc_path(machine, source_pcb)
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
            return settings_from_dict(doc["settings"])
        return self._fresh(base_config)

    def export_doc(
        self,
        machine: str,
        source_pcb: str,
        model: PasteSettingsModel,
        *,
        board_signature: str | None = None,
    ) -> dict:
        """ダウンロード用の保存 JSON ドキュメントを返す."""
        return self._doc(machine, source_pcb, model, board_signature=board_signature)

    def model_from_doc(
        self,
        doc: dict,
        *,
        board_signature: str | None = None,
        expected_machine: str | None = None,
        expected_source_pcb: str | None = None,
    ) -> PasteSettingsModel:
        """アップロードされた保存 JSON を検証し、設定モデルへ復元する."""
        self._check_version(doc, Path("<uploaded board settings>"))
        if expected_machine is not None and doc.get("machine") != expected_machine:
            raise ValueError(
                f"マシンが一致しません: {doc.get('machine')} != {expected_machine}"
            )
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
        return settings_from_dict(doc["settings"])

    def _fresh(self, base_config: PasteDispenser) -> PasteSettingsModel:
        return PasteSettingsModel(
            base=base_override_from_config(base_config),
            base_enabled=True,
            levels={},
        )

    def save(
        self,
        machine: str,
        source_pcb: str,
        model: PasteSettingsModel,
        *,
        board_signature: str | None = None,
    ) -> None:
        """設定モデルを即時保存する.

        Args:
            machine: マシン名
            source_pcb: ``pcb_browse_root`` からの相対 posix パス
            model: 保存する設定モデル
        """
        path = self._path(machine, source_pcb)
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = self._doc(machine, source_pcb, model, board_signature=board_signature)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    def prune(
        self,
        machine: str,
        source_pcb: str,
        model: PasteSettingsModel,
        hierarchy: PadHierarchy,
        *,
        board_signature: str | None = None,
    ) -> PasteSettingsModel:
        """現階層に存在しない設定キー（孤児）を除去して保存する.

        Args:
            machine: マシン名
            source_pcb: ``pcb_browse_root`` からの相対 posix パス
            model: 入力の設定モデル
            hierarchy: 現在の pad 階層

        Returns:
            孤児を除いた設定モデル（孤児が無ければ ``model`` と等価）
        """
        orphans = set(find_orphans(model, hierarchy))
        if not orphans:
            self.save(machine, source_pcb, model, board_signature=board_signature)
            return model
        levels = {
            key: setting for key, setting in model.levels.items() if key not in orphans
        }
        pruned = attrs.evolve(model, levels=levels)
        self.save(machine, source_pcb, pruned, board_signature=board_signature)
        return pruned

    def _doc(
        self,
        machine: str,
        source_pcb: str,
        model: PasteSettingsModel,
        *,
        board_signature: str | None,
    ) -> dict:
        doc = {
            "version": _SCHEMA_VERSION,
            "source_pcb": source_pcb,
            "machine": machine,
            "settings": settings_to_dict(model),
        }
        if board_signature is not None:
            doc["board_signature"] = board_signature
        return doc

    def _path(self, machine: str, source_pcb: str) -> Path:
        return self._root / machine / f"{self.board_id(source_pcb)}.json"

    def _doc_path(self, machine: str, source_pcb: str) -> Path | None:
        path = self._path(machine, source_pcb)
        if path.is_file():
            return path
        if self._legacy_root is None:
            return None
        legacy_path = self._legacy_root / machine / f"{self.board_id(source_pcb)}.json"
        return legacy_path if legacy_path.is_file() else None

    def _check_version(self, doc: dict, path: Path) -> None:
        version = doc.get("version")
        if version != _SCHEMA_VERSION:
            raise ValueError(
                f"未知の board_settings schema version です: {version} "
                f"(対応 v{_SCHEMA_VERSION}, {path})"
            )
