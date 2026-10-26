"""はんだ塗布（pcbasm.pasting）を呼ぶジョブ."""

from __future__ import annotations

from pathlib import Path

import attrs

from pcbasm.pasting.overrides import BoardOverrides


@attrs.frozen
class PasteStores:
    """塗布のジョブが読み書きする永続データ（Backend が組み、catalog の登録時に渡す）.

    Attributes:
        overrides: 基板ごとの塗布設定。HTTP 経路と同じインスタンスを渡す（別だと更新ロックで直列化できない）
        paste_dataset_dir: 塗布量校正の材料にする dataset の置き場所
        paste_volume_calibration_dir: 塗布量校正の置き場所
    """

    overrides: BoardOverrides
    paste_dataset_dir: Path
    paste_volume_calibration_dir: Path
