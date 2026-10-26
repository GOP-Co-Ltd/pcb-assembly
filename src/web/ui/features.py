"""唯一の機能表（タブ・グループ・テンプレートと、旧 URL の対応）.

機能のページを 1 つ足すときは ``FEATURES`` に 1 行足す。slug は URL
（``/m/{machine_id}/{slug}``）で、ジョブのページではジョブ名と同じにする。
ジョブのパラメータ・プレビューの有無・表示名は backend が ``GET /api/jobs`` で申告するので、ここには置かない。
タブとグループは表示上のまとまりで、この表だけが持つ（backend は知らない）。

機能のページには、それを使える機体の機能（``capability``）を書く（None はどの機体にもある）。
機体のタブ・サイドバー・着地点は、backend が ``GET /api/session/machine-info`` で申告する機能で絞る。
"""

from __future__ import annotations

from collections.abc import Collection

import attrs

from web.contract.models import Capability
from web.ui.models import UPDATE_INDEX_PATH


@attrs.frozen
class Feature:
    """機能のページ 1 つ."""

    slug: str
    tab: str
    # サイドバーの見出し（同じタブの中で、表の順に最初に出た位置へまとめる）
    group: str
    template: str
    # ジョブでないページの表示名。ジョブのページは表示名を backend のジョブ定義から取るので持たない
    label: str | None = None
    # このページを使える機体の機能（None はどの機体にもある）
    capability: Capability | None = None

    @property
    def is_job(self) -> bool:
        """ジョブのページか（表示名・フォーム・プレビューの有無を backend のジョブ定義から取る）."""
        return self.label is None

    def is_available(self, capabilities: Collection[Capability]) -> bool:
        """機能 ``capabilities`` の機体にこのページがあるか."""
        return self.capability is None or self.capability in capabilities


# タブ slug → 表示名（作業の頻度順。タブのリンクは、そのタブの先頭の機能を開く）
TABS: dict[str, str] = {
    "pasting": "塗布",
    "pnp": "部品実装",
    "calibration": "校正",
    "board": "基板",
    "machine": "機体",
}

FEATURES: tuple[Feature, ...] = (
    Feature(
        "paste_solder",
        "pasting",
        "日常の塗布",
        "pasting/paste_solder.html",
        capability="solder",
    ),
    Feature(
        "loading", "pasting", "日常の塗布", "pasting/loading.html", capability="solder"
    ),
    Feature(
        "height_plane", "pasting", "日常の塗布", "pasting/job.html", capability="solder"
    ),
    Feature(
        "nozzle_clean_test",
        "pasting",
        "日常の塗布",
        "pasting/job.html",
        capability="solder",
    ),
    Feature(
        "placement",
        "pnp",
        "部品実装",
        "pnp/placement.html",
        label="部品実装（準備中）",
        capability="pnp",
    ),
    # 校正タブの入口（タブのリンク先）。段の表は pcbasm.commissioning が持つ
    Feature(
        "commissioning",
        "calibration",
        "立ち上げ",
        "commissioning.html",
        label="立ち上げチェックリスト",
    ),
    Feature(
        "camera_calibration", "calibration", "カメラ", "align/camera_calibration.html"
    ),
    Feature(
        "camera_preview",
        "calibration",
        "カメラ",
        "align/camera_preview.html",
        label="カメラプレビュー",
    ),
    Feature(
        "reference_point_setup",
        "calibration",
        "位置",
        "align/reference_point_setup.html",
    ),
    Feature("board_tour", "calibration", "位置", "align/job.html"),
    Feature("orthogonality_test", "calibration", "位置", "align/job.html"),
    Feature(
        "copper_detection",
        "calibration",
        "位置",
        "align/copper_detection.html",
        label="銅箔検出調整",
    ),
    Feature(
        "nozzle_cap",
        "calibration",
        "ノズル・プローブ",
        "pasting/nozzle_cap.html",
        label="ノズル位置の設定（キャップ / クリーニング）",
        capability="solder",
    ),
    Feature(
        "probe_guide",
        "calibration",
        "ノズル・プローブ",
        "pasting/probe_guide.html",
        label="ロードセルプローブ ガイド",
    ),
    Feature(
        "toolhead_offset",
        "calibration",
        "ノズル・プローブ",
        "pasting/job.html",
        capability="solder",
    ),
    Feature(
        "dispense_calibration",
        "calibration",
        "吐出量・塗布量",
        "pasting/dispense_calibration.html",
        capability="solder",
    ),
    Feature(
        "paste_volume_calibration",
        "calibration",
        "吐出量・塗布量",
        "pasting/paste_volume_calibration.html",
        capability="solder",
    ),
    Feature(
        "paste_volume_refit",
        "calibration",
        "吐出量・塗布量",
        "pasting/job.html",
        capability="solder",
    ),
    Feature(
        "paste_dataset_finalize",
        "calibration",
        "吐出量・塗布量",
        "pasting/job.html",
        capability="solder",
    ),
    Feature(
        "paste_test_board",
        "board",
        "テスト基板",
        "pasting/paste_test_board.html",
        label="テスト塗布基板生成",
        capability="solder",
    ),
    Feature("generate_grid_pcb", "board", "テスト基板", "job.html"),
    Feature("generate_rect_pcb", "board", "テスト基板", "job.html"),
    Feature("extract_pcb", "board", "設計データ", "job.html"),
    Feature("make_fill_coverage_pcb", "board", "設計データ", "job.html"),
    Feature(
        "klipper_status",
        "machine",
        "状態と診断",
        "machine/klipper_status.html",
        label="Klipper ステータス",
    ),
    Feature("machine_check", "machine", "状態と診断", "job.html"),
    Feature(
        "machine_config",
        "machine",
        "設定",
        "machine/machine_config.html",
        label="マシン設定",
    ),
    Feature("audio", "machine", "通知音", "machine/audio.html", label="通知音"),
    Feature(
        "config_history",
        "machine",
        "バックアップと設定履歴",
        "machine/config_history.html",
        label="設定履歴とバックアップ",
    ),
)

FEATURES_BY_SLUG: dict[str, Feature] = {feature.slug: feature for feature in FEATURES}

# 旧 URL のタブ（/m/{machine_id}/{旧タブ}/{旧 slug}）
LEGACY_TABS = frozenset({"dev", "pasting", "posctrl"})

# 旧 URL で名前の変わった段 → 行き先（機体の feature slug か、機体に依らないページの絶対パス）。
# 旧 URL は /m/{machine_id}/{旧タブ}/{旧 slug}、/m/{machine_id}/{旧タブ}、/m/{machine_id}/settings の 3 形で、
# ここに無い旧 slug は同じ名前の機能へ飛ぶ。ブックマークを救うためのもので、増やさない
LEGACY: dict[str, str] = {
    "dev": "klipper_status",
    "pasting": "paste_solder",
    "posctrl": "camera_calibration",
    "settings": "machine_config",
    "update": UPDATE_INDEX_PATH,
}


def tab_features(tab: str, capabilities: Collection[Capability]) -> tuple[Feature, ...]:
    """機能 ``capabilities`` の機体にあるタブの機能を、表の順に返す（先頭がタブのリンク先）."""
    return tuple(
        feature
        for feature in FEATURES
        if feature.tab == tab and feature.is_available(capabilities)
    )


def machine_tabs(capabilities: Collection[Capability]) -> dict[str, str]:
    """機能 ``capabilities`` の機体に出すタブ（slug → 表示名。機能のページが 1 つも無いタブは出さない）."""
    return {
        tab: label for tab, label in TABS.items() if tab_features(tab, capabilities)
    }


def landing(capabilities: Collection[Capability]) -> str:
    """機体のページの着地点（`/` と `/m/{machine_id}` から飛ぶ。最初のタブの先頭の機能）.

    塗布機なら塗布（paste_solder）、部品実装だけの機体なら部品実装のページになる。
    """
    tab = next(iter(machine_tabs(capabilities)))
    return tab_features(tab, capabilities)[0].slug
