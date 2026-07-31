"""ページ構成の表示知識（タブ / feature / テンプレート / 設定セクション）.

`web.api.routers.pages` が持っていた**純粋な表示知識だけ**を frontend 側へ移した。
装置の事実（フレーム提供の有無・progress_stage 文字列・ジョブのパラメータ定義）は
ここには置かない。それらは backend が `GET /api/jobs` の `JobSpecInfo` で自己申告し、
frontend はその値をテンプレートへ渡すだけにする。

`SECTION_LABELS` / `section_of` は設定ページの階層表示専用なので frontend だけが持つ
（backend は `GET /api/settings/machine` で項目と値を返すだけで、表示のまとめ方を知らない）。
"""

from __future__ import annotations

from itertools import groupby

from web.api.models import SettingsField

# tab → feature slug 列（ヘッダのタブ表示順）
TABS: dict[str, tuple[str, ...]] = {
    "dev": (
        "extract_pcb",
        "make_fill_coverage_pcb",
        "klipper_status",
    ),
    "pasting": (
        "paste_solder",
        "height_plane",
        "loading",
        "dispense_calibration",
        "generate_rect_pcb",
        "toolhead_offset",
        "probe_guide",
        "nozzle_cap",
    ),
    "pnp": (),
    "posctrl": (
        "camera_preview",
        "copper_detection",
        "camera_calibration",
        "reference_point_setup",
        "board_tour",
        "orthogonality_test",
        "generate_grid_pcb",
    ),
}

# tab slug → 表示名（ヘッダのタブラベル）
TAB_LABELS: dict[str, str] = {
    "dev": "開発",
    "pasting": "はんだ塗布",
    "pnp": "部品実装",
    "posctrl": "位置合わせ",
}

# 非ジョブ feature slug → 表示名（サイドバー / 見出し）。ジョブは backend の
# JobSpecInfo.label を正とする。未定義は単語化フォールバック
FEATURE_LABELS: dict[str, str] = {
    "klipper_status": "Klipper ステータス",
    "probe_guide": "ロードセルプローブ ガイド",
    "nozzle_cap": "ノズルキャップ位置の設定",
    "camera_preview": "カメラプレビュー",
    "copper_detection": "銅箔検出調整",
}

# feature 実装予定の Phase（プレースホルダ表示用）
TAB_PHASES: dict[str, str] = {
    "dev": "Phase 3",
    "pasting": "Phase 5",
    "pnp": "将来",
    "posctrl": "Phase 2/4",
}

# 専用テンプレートを持つ feature（無いものは feature.html プレースホルダ）
# job.html はカメラ preview を持たない汎用ジョブページ（タブ横断で共用）
FEATURE_TEMPLATES: dict[tuple[str, str], str] = {
    ("dev", "extract_pcb"): "job.html",
    ("dev", "make_fill_coverage_pcb"): "job.html",
    ("dev", "klipper_status"): "dev/klipper_status.html",
    ("pasting", "paste_solder"): "pasting/paste_solder.html",
    ("pasting", "height_plane"): "pasting/job.html",
    ("pasting", "loading"): "pasting/loading.html",
    ("pasting", "dispense_calibration"): "pasting/dispense_calibration.html",
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
        "pasting/paste_solder.html",
        "posctrl/job.html",
        "posctrl/camera_calibration.html",
        "posctrl/reference_point_setup.html",
    }
)

# はんだ塗布ページに即保存フォームで載せる auto しきい値（machine 全体設定）
PASTE_AUTO_THRESHOLD_KEYS = (
    "paste_dispenser.auto_line_aspect_ratio",
    "paste_dispenser.auto_area_short_side_factor",
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

# 設定セクション（key のドット区切り親パス）→ UI 表示名。
# settings ページの階層表示に使う
SECTION_LABELS: dict[str, str] = {
    # トップレベル（bare key）は section_of が生キーを返すため、明示的にラベルを持たせる
    "machine_name": "マシン",
    "paste_dispenser": "ペーストディスペンサー",
    "paste_dispenser.toolhead": "ペーストディスペンサー / ツールヘッド",
    "paste_dispenser.pad_align": "ペーストディスペンサー / パッド位置合わせ",
    "probe": "プローブ",
    "reference_point": "基準点",
    "reference_point.offsets": "基準点 / コーナーオフセット",
    "nozzle_cap": "ノズルキャップ",
    "camera": "カメラ",
    "camera.crop": "カメラ / クロップ",
}


def section_of(key: str) -> str:
    """設定 key の属するセクション（最後のドットより前）を返す."""
    return key.rsplit(".", 1)[0]


def grouped_fields(
    fields: list[SettingsField],
) -> list[tuple[str, list[SettingsField]]]:
    """設定項目をセクション単位にまとめる（定義順を保つ）."""
    return [
        (SECTION_LABELS.get(section, section), list(group))
        for section, group in groupby(fields, key=lambda f: section_of(f.key))
    ]
