"""点塗布の実素材（tests/data/paste-volume）から、塗布量の校正に使う session を組む."""

import json
import shutil
from pathlib import Path
from typing import Any

from tests.support.paths import PASTE_DATASET_METADATA, TEST_DATA_DIR

PASTE_VOLUME_MATERIAL_DIR = TEST_DATA_DIR / "paste-volume"

# 実素材 `tests/data/paste-volume/` の収集条件（出典は同 directory の README）
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
    document = json.loads(PASTE_DATASET_METADATA.read_text(encoding="utf-8"))
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
