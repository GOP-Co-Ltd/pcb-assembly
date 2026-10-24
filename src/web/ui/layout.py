"""ページ構成の表示定義（タブ / feature / テンプレート）.

ここに置くのは純粋な表示定義だけ。
装置の事実（フレーム提供の有無・progress_stage 文字列・ジョブのパラメータ定義）は
ここには置かない。それらは backend が `GET /api/jobs` の `JobSpecInfo` で自己申告し、
frontend はその値をテンプレートへ渡すだけにする。

設定ページの項目・見出し・並び順も backend が `GET /api/settings/machine` で返す。
`settings_sections` はそれを画面とカードへまとめるだけで、並べ替えない。
"""

from __future__ import annotations

from collections.abc import Sequence

import attrs

from web.contract.models import SettingsField

# tab → (グループ名、案内、feature slug 列)。サイドバーと入口画面で共用する表示定義。
FEATURE_GROUPS: dict[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    "dev": (
        (
            "PCB ユーティリティ",
            "設計データの抽出と塗布範囲の確認。",
            ("extract_pcb", "make_fill_coverage_pcb"),
        ),
        (
            "機体の管理",
            "接続状態、通知音、設定履歴とバックアップ、ソフトウェアを確認します。",
            ("klipper_status", "audio", "config_history", "update"),
        ),
    ),
    "pasting": (
        (
            "日常の塗布",
            "基板の塗布設定、高さの計測、ペーストの準備。",
            ("paste_solder", "height_plane", "loading"),
        ),
        (
            "ノズル・プローブ設定",
            "機体を使い始めるときや工具を交換したときの調整。",
            ("nozzle_cap", "toolhead_offset", "probe_guide"),
        ),
        (
            "吐出・塗布量の校正",
            "吐出特性を測定し、保存したデータから校正を整えます。",
            (
                "dispense_calibration",
                "paste_volume_calibration",
                "paste_volume_refit",
                "paste_dataset_finalize",
            ),
        ),
        (
            "テスト基板",
            "塗布の確認やキャリブレーションに使う基板を作成します。",
            ("paste_test_board", "generate_rect_pcb"),
        ),
    ),
    "posctrl": (
        (
            "映像と検出",
            "カメラの映像を見ながら、銅箔の検出条件を調整します。",
            ("camera_preview", "copper_detection"),
        ),
        (
            "機体の校正",
            "カメラ、基準点、移動軸の位置関係を確認します。",
            ("camera_calibration", "reference_point_setup", "orthogonality_test"),
        ),
        (
            "基板の確認と作成",
            "基板上の位置を巡回して確認し、校正用の基板を作成します。",
            ("board_tour", "generate_grid_pcb"),
        ),
    ),
}

# URL の検証に使う全 feature。グループ定義と別々に機能を登録しない。
TABS: dict[str, tuple[str, ...]] = {
    tab: tuple(slug for _label, _description, slugs in groups for slug in slugs)
    for tab, groups in FEATURE_GROUPS.items()
}

# tab slug → 表示名（ヘッダのタブラベル）
TAB_LABELS: dict[str, str] = {
    "dev": "開発",
    "pasting": "はんだ塗布",
    "posctrl": "位置合わせ",
}

# 一括管理ページ（全マシンを 1 画面に並べる）。マシン非依存なので ``/m/{machine_id}`` を
# 付けない。TABS には入れず、header のタブ列では TABS の後ろに prefix なしのリンクとして常に出す
BULK_PATH = "/bulk"
BULK_LABEL = "一括管理"

# 非ジョブ feature slug → 表示名（サイドバー / 見出し）。ジョブは backend の
# JobSpecInfo.label を正とする。未定義の slug は `_` を空白に置き換えて単語化した名前で代用する
FEATURE_LABELS: dict[str, str] = {
    "klipper_status": "Klipper ステータス",
    "audio": "通知音",
    "config_history": "設定履歴とバックアップ",
    "update": "ソフトウェア更新",
    "probe_guide": "ロードセルプローブ ガイド",
    "nozzle_cap": "ノズル位置の設定（キャップ / クリーニング）",
    "paste_test_board": "テスト塗布基板生成",
    "camera_preview": "カメラプレビュー",
    "copper_detection": "銅箔検出調整",
}

# feature ごとのテンプレート（登録する全機能に必要）
# job.html はカメラ preview を持たない汎用ジョブページ（タブ横断で共用）
FEATURE_TEMPLATES: dict[tuple[str, str], str] = {
    ("dev", "extract_pcb"): "job.html",
    ("dev", "make_fill_coverage_pcb"): "job.html",
    ("dev", "klipper_status"): "dev/klipper_status.html",
    ("dev", "audio"): "dev/audio.html",
    ("dev", "config_history"): "dev/config_history.html",
    # ジョブではないので JOB_TEMPLATES には入れない（装置ロックを取らない独立経路）
    ("dev", "update"): "dev/update.html",
    ("pasting", "paste_solder"): "pasting/paste_solder.html",
    ("pasting", "height_plane"): "pasting/job.html",
    ("pasting", "loading"): "pasting/loading.html",
    ("pasting", "dispense_calibration"): "pasting/dispense_calibration.html",
    (
        "pasting",
        "paste_volume_calibration",
    ): "pasting/paste_volume_calibration.html",
    ("pasting", "paste_dataset_finalize"): "pasting/job.html",
    ("pasting", "paste_volume_refit"): "pasting/job.html",
    (
        "pasting",
        "paste_test_board",
    ): "pasting/paste_test_board.html",
    ("pasting", "generate_rect_pcb"): "pasting/job.html",
    ("pasting", "toolhead_offset"): "pasting/job.html",
    ("pasting", "probe_guide"): "pasting/probe_guide.html",
    ("pasting", "nozzle_cap"): "pasting/nozzle_cap.html",
    ("posctrl", "camera_preview"): "posctrl/camera_preview.html",
    ("posctrl", "copper_detection"): "posctrl/copper_detection.html",
    ("posctrl", "camera_calibration"): "posctrl/camera_calibration.html",
    ("posctrl", "board_tour"): "posctrl/job.html",
    ("posctrl", "orthogonality_test"): "posctrl/job.html",
    ("posctrl", "reference_point_setup"): "posctrl/reference_point_setup.html",
    ("posctrl", "generate_grid_pcb"): "job.html",
}

# ジョブコンテキスト（job_name / param_specs）を注入するテンプレート
JOB_TEMPLATES = frozenset(
    {
        "job.html",
        "pasting/job.html",
        "pasting/loading.html",
        "pasting/dispense_calibration.html",
        "pasting/paste_volume_calibration.html",
        "pasting/paste_solder.html",
        "posctrl/job.html",
        "posctrl/camera_calibration.html",
        "posctrl/reference_point_setup.html",
    }
)

LOADING_ROTATION_PARAMS = ("rotations", "rate", "accel", "retract_rotations")

# dispense_calibration フォームのセクション分け（表示のみ）。
# ①②③ の依存順に沿ってパラメータを視覚的にグルーピングする。
DISPENSE_CALIBRATION_PARAM_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "銅板・位置合わせ（キャリブ後固定）",
        ("board_width", "board_height", "tolerance"),
    ),
    (
        "線の共通設定（実行中変更可）",
        ("line_length", "line_count", "line_amount", "row_pitch", "removal_z_offset"),
    ),
    (
        "② max_dispense_rate（吐出効率の落ち検出）",
        ("rate_min", "rate_max", "rate_divisions"),
    ),
    (
        "③ max_fill_speed（連続塗布の最大速度）",
        ("speed_min", "speed_max", "speed_divisions"),
    ),
)

# paste_volume_calibration フォームのセクション分け（表示のみ）。
# 項目数が多く縦一列だと読みにくいため、依存関係の近いものをまとめて段組みにする。
# 全パラメータを漏れなく含める（欠けた項目はフォームから消える）。
PASTE_VOLUME_CALIBRATION_PARAM_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "銅板",
        (
            "plate_width",
            "plate_height",
            "edge_margin",
            "tolerance",
        ),
    ),
    (
        "セル格子",
        ("cell_size", "cell_gap", "crop_size"),
    ),
    (
        "吐出量スイープ",
        (
            "volume_min",
            "volume_max",
            "volume_divisions",
            "samples_per_volume",
            "blank_count",
            "shuffle_seed",
        ),
    ),
    (
        "ローディング",
        (
            "loading_amount",
            "loading_rotations",
            "loading_rate",
            "loading_accel",
            "loading_retract_rotations",
        ),
    ),
    (
        "塗布と撮影",
        ("paste_height", "view_count", "view_offset"),
    ),
    (
        "ペースト",
        ("paste_id", "paste_lot"),
    ),
    (
        "円検出のハイパラ",
        (
            "min_contrast",
            "contrast_percentile",
            "threshold_floor_ratio",
            "open_kernel_px",
            "min_area_px",
            "require_blank_zero",
        ),
    ),
    (
        "校正の保存と検証",
        ("save_name", "volume_calibration"),
    ),
)


@attrs.frozen
class SettingsGroup:
    """描画用の設定カード 1 枚（値入り）."""

    label: str
    fields: tuple[SettingsField, ...]


@attrs.frozen
class SettingsSection:
    """描画用の設定セクション 1 画面（値入り）."""

    slug: str
    label: str
    groups: tuple[SettingsGroup, ...]

    @property
    def field_count(self) -> int:
        """このセクションに並ぶ設定項目の総数（ナビのバッジ表示用）."""
        return sum(len(group.fields) for group in self.groups)


def settings_sections(fields: Sequence[SettingsField]) -> list[SettingsSection]:
    """Backend が表示する順に返した設定項目を、画面（section）とカード（group）へまとめる."""
    tree: dict[tuple[str, str], dict[str, list[SettingsField]]] = {}
    for field in fields:
        groups = tree.setdefault((field.section, field.section_label), {})
        groups.setdefault(field.group, []).append(field)
    return [
        SettingsSection(
            slug=slug,
            label=label,
            groups=tuple(
                SettingsGroup(label=group, fields=tuple(members))
                for group, members in groups.items()
            ),
        )
        for (slug, label), groups in tree.items()
    ]
