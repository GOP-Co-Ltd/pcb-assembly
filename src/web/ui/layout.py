"""ページ構成の表示定義（タブ / feature / テンプレート / 設定セクション）.

ここに置くのは純粋な表示定義だけ。
装置の事実（フレーム提供の有無・progress_stage 文字列・ジョブのパラメータ定義）は
ここには置かない。それらは backend が `GET /api/jobs` の `JobSpecInfo` で自己申告し、
frontend はその値をテンプレートへ渡すだけにする。

`SETTINGS_SECTIONS` / `settings_sections` は設定ページの階層表示専用なので frontend だけが
持つ（backend は `GET /api/settings/machine` で項目と値を返すだけで、表示のまとめ方も
並び順も知らない）。
"""

from __future__ import annotations

from collections.abc import Sequence

import attrs

from web.api.models import SettingsField
from web.ui.machines import MachineEndpoint

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
            "接続状態、通知音、ソフトウェアを確認します。",
            ("klipper_status", "audio", "update"),
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
    "pnp": (),
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
    "pnp": "部品実装",
    "posctrl": "位置合わせ",
}

# 一括管理ページ（全マシンを 1 画面に並べる）。マシン非依存なので ``/m/{machine_id}`` を
# 付けない。ヘッダのタブとしては TABS の後ろに常に出す
BULK_PATH = "/bulk"
BULK_LABEL = "一括管理"

# 一括管理のセクション（machine_type → 見出し）。該当機体が無くても出す
_BULK_SECTION_TITLES: tuple[tuple[str, str], ...] = (
    ("paste", "はんだペースト"),
    ("pnp", "PnP"),
)
_BULK_UNKNOWN_SLUG = "unknown"
_BULK_UNKNOWN_TITLE = "種別不明"


@attrs.frozen
class BulkSection:
    """一括管理の 1 セクション（同じ machine_type の機体を登録順に並べる）."""

    slug: str
    title: str
    # 種別不明のセクションでは None
    machine_type: str | None
    machines: tuple[MachineEndpoint, ...]


def bulk_sections(machines: Sequence[MachineEndpoint]) -> tuple[BulkSection, ...]:
    """登録機体を machine_type ごとのセクションに分ける.

    はんだペースト・PnP のセクションは機体が無くても出す。machine_type が未設定か
    未知の機体は末尾の「種別不明」セクションにまとめる（該当があるときだけ出す）。
    一覧から黙って消えると、登録したのに表示されない理由が分からなくなるため。
    """
    known = [machine_type for machine_type, _title in _BULK_SECTION_TITLES]
    sections = [
        BulkSection(
            slug=machine_type,
            title=title,
            machine_type=machine_type,
            machines=tuple(m for m in machines if m.machine_type == machine_type),
        )
        for machine_type, title in _BULK_SECTION_TITLES
    ]
    if unknown := tuple(m for m in machines if m.machine_type not in known):
        sections.append(
            BulkSection(
                slug=_BULK_UNKNOWN_SLUG,
                title=_BULK_UNKNOWN_TITLE,
                machine_type=None,
                machines=unknown,
            )
        )
    return tuple(sections)


# 非ジョブ feature slug → 表示名（サイドバー / 見出し）。ジョブは backend の
# JobSpecInfo.label を正とする。未定義の slug は `_` を空白に置き換えて単語化した名前で代用する
FEATURE_LABELS: dict[str, str] = {
    "klipper_status": "Klipper ステータス",
    "audio": "通知音",
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

# ノズル位置ページに即保存フォームで載せるクリーニング設定。
# 位置 XYZ は記録ボタンの管轄なので、同じ画面に手打ち欄を並べない
# （どちらが正しい値か曖昧になる。緊急時に手で入力する経路は /settings に残す）。
NOZZLE_CLEAN_SETTING_KEYS = (
    "paste_dispenser.nozzle_clean.press_depth",
    "paste_dispenser.nozzle_clean.purge_ul",
    "paste_dispenser.nozzle_clean.stroke",
    "paste_dispenser.nozzle_clean.passes",
    "paste_dispenser.nozzle_clean.wipe_speed",
)

# 上のうち 0 を受け付けないキー（0 は「その工程を行わない」を意味しない）
POSITIVE_ONLY_MACHINE_KEYS = frozenset({"paste_dispenser.nozzle_clean.wipe_speed"})

# はんだ塗布ページに即保存フォームで載せる auto しきい値（machine 全体設定）
PASTE_AUTO_THRESHOLD_KEYS = (
    "paste_dispenser.auto_line_aspect_ratio",
    "paste_dispenser.auto_area_short_side_factor",
)

# はんだ塗布ページに即保存フォームで載せる pad 逐次位置合わせ設定
PASTE_PAD_REFINEMENT_KEYS = ("paste_dispenser.pad_align.refine_max_short_side",)

# はんだ塗布ページに即保存フォームで載せる運転時流量キャリブレーション設定。
# 測定位置だけは基板ごとの設定なので pad editor 側で編集する（表示は同じ箱に並べる）。
PASTE_FLOW_CALIBRATION_KEYS = (
    "paste_dispenser.flow_calibration.calibration_file",
    "paste_dispenser.flow_calibration.amount_ul",
    "paste_dispenser.flow_calibration.crop_size_mm",
    "paste_dispenser.flow_calibration.settle_seconds",
)

# 上のうち、保存済み校正の <select> へ差し替えるキー
PASTE_FLOW_CALIBRATION_FILE_KEY = "paste_dispenser.flow_calibration.calibration_file"

# 空欄の保存が「未入力」ではなく「無効にする」を意味するキー。
# 既定では空欄は保存しないので、これが無いと一度設定した値を消せない。
CLEARABLE_MACHINE_KEYS = frozenset({PASTE_FLOW_CALIBRATION_FILE_KEY})

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
class SettingsGroupSpec:
    """設定カード 1 枚の宣言.

    Attributes:
        label: カードの見出し
        keys: カードへ並べる設定 key（この順に描く）
    """

    label: str
    keys: tuple[str, ...]


@attrs.frozen
class SettingsSectionSpec:
    """設定ページの左ナビ 1 項目 = 右ペイン 1 画面の宣言.

    Attributes:
        slug: ナビの選択状態と URL hash に使う識別子
        label: ナビとパネル見出しの表示名
        groups: 画面に並べるカード
    """

    slug: str
    label: str
    groups: tuple[SettingsGroupSpec, ...]


# 設定ページの構成。並び順はここの定義に従い、backend の `MACHINE_FIELDS` の定義順には
# 依存しない（依存させると、backend が入れ子セクションを途中に挟んだ時点で親セクションの
# カードが 2 枚に分かれる）。宣言漏れの項目は「未分類」に入るだけで消えないが、
# tests/web/ui/test_layout.py で MACHINE_FIELDS との双方向の網羅を検査している。
SETTINGS_SECTIONS: tuple[SettingsSectionSpec, ...] = (
    SettingsSectionSpec(
        "paste_dispenser",
        "ペーストディスペンサー",
        (
            SettingsGroupSpec(
                "吐出の基本",
                (
                    "paste_dispenser.rotations_per_ul",
                    "paste_dispenser.nozzle_diameter",
                    "paste_dispenser.solder_paste_density",
                    "paste_dispenser.ul_per_mm2",
                ),
            ),
            SettingsGroupSpec(
                "吐出の速度",
                (
                    "paste_dispenser.max_dispense_rate",
                    "paste_dispenser.dispense_accel",
                    "paste_dispenser.max_fill_speed",
                ),
            ),
            SettingsGroupSpec(
                "リトラクト",
                (
                    "paste_dispenser.retract_amount",
                    "paste_dispenser.retract_rate",
                    "paste_dispenser.retract_accel_factor",
                ),
            ),
            SettingsGroupSpec(
                "プライム・パージ",
                (
                    "paste_dispenser.prime_extra_delay",
                    "paste_dispenser.initial_purge_ul",
                ),
            ),
            SettingsGroupSpec(
                "塗布方式",
                (
                    "paste_dispenser.dispense_mode",
                    "paste_dispenser.line_direction",
                    "paste_dispenser.auto_line_aspect_ratio",
                    "paste_dispenser.auto_area_short_side_factor",
                ),
            ),
            SettingsGroupSpec(
                "塗布経路",
                (
                    "paste_dispenser.bead_width_factor",
                    "paste_dispenser.overlap",
                    "paste_dispenser.boundary_margin",
                ),
            ),
            SettingsGroupSpec(
                "Z 高さ",
                ("paste_dispenser.paste_height", "paste_dispenser.lift_height"),
            ),
            SettingsGroupSpec(
                "ツールヘッド",
                ("paste_dispenser.toolhead.x", "paste_dispenser.toolhead.y"),
            ),
            SettingsGroupSpec(
                "パッド位置合わせ",
                (
                    "paste_dispenser.pad_align.region_size_px",
                    "paste_dispenser.pad_align.region_overlap",
                    "paste_dispenser.pad_align.board_edge_margin",
                    "paste_dispenser.pad_align.refine_max_short_side",
                    "paste_dispenser.pad_align.search_window",
                    "paste_dispenser.pad_align.max_correction",
                    "paste_dispenser.pad_align.max_passes",
                    "paste_dispenser.pad_align.converge_tolerance",
                    "paste_dispenser.pad_align.canny_low",
                    "paste_dispenser.pad_align.canny_high",
                    "paste_dispenser.pad_align.blur_ksize",
                ),
            ),
            SettingsGroupSpec(
                "流量キャリブレーション",
                (
                    "paste_dispenser.flow_calibration.calibration_file",
                    "paste_dispenser.flow_calibration.amount_ul",
                    "paste_dispenser.flow_calibration.crop_size_mm",
                    "paste_dispenser.flow_calibration.settle_seconds",
                ),
            ),
            SettingsGroupSpec(
                "ノズルキャップ",
                (
                    "paste_dispenser.nozzle_cap.x",
                    "paste_dispenser.nozzle_cap.y",
                    "paste_dispenser.nozzle_cap.z",
                ),
            ),
            SettingsGroupSpec(
                "ノズルクリーニング",
                (
                    "paste_dispenser.nozzle_clean.x",
                    "paste_dispenser.nozzle_clean.y",
                    "paste_dispenser.nozzle_clean.z",
                    "paste_dispenser.nozzle_clean.press_depth",
                    "paste_dispenser.nozzle_clean.purge_ul",
                    "paste_dispenser.nozzle_clean.stroke",
                    "paste_dispenser.nozzle_clean.passes",
                    "paste_dispenser.nozzle_clean.wipe_speed",
                ),
            ),
        ),
    ),
    SettingsSectionSpec(
        "probe",
        "プローブ",
        (
            SettingsGroupSpec(
                "プローブ",
                (
                    "probe.lift_height",
                    "probe.min_radius",
                    "probe.board_edge_margin",
                    "probe.min_samples",
                    "probe.max_samples",
                ),
            ),
        ),
    ),
    SettingsSectionSpec(
        "reference_point",
        "基準点",
        (
            SettingsGroupSpec(
                "位置",
                (
                    "reference_point.x",
                    "reference_point.y",
                    "reference_point.target_diameter",
                ),
            ),
            SettingsGroupSpec(
                "コーナーオフセット",
                (
                    "reference_point.offsets.top_left",
                    "reference_point.offsets.top_right",
                    "reference_point.offsets.bottom_left",
                    "reference_point.offsets.bottom_right",
                ),
            ),
        ),
    ),
    SettingsSectionSpec(
        "camera",
        "カメラ",
        (
            SettingsGroupSpec(
                "デバイス",
                (
                    "camera.calibration_file",
                    "camera.device_id",
                    "camera.width",
                    "camera.height",
                    "camera.fps",
                    "camera.format",
                ),
            ),
            SettingsGroupSpec("クロップ", ("camera.crop.width", "camera.crop.height")),
        ),
    ),
    SettingsSectionSpec(
        "settle_detection",
        "静定待ち・検出",
        (
            SettingsGroupSpec("静定待ち", ("settle.move_sec", "settle.probe_sec")),
            SettingsGroupSpec(
                "統計検出",
                ("detection.sample_count", "detection.minimum_sample_count"),
            ),
        ),
    ),
    SettingsSectionSpec(
        "audio",
        "通知音",
        (SettingsGroupSpec("通知音", ("audio.device", "audio.volume")),),
    ),
)

# 宣言から漏れた項目を受けるセクション（backend が項目を足して frontend が追随していない
# 状態）。設定ページは machine.toml の唯一の編集画面なので、項目を捨てず末尾へ出す
UNCATEGORIZED_SLUG = "uncategorized"
UNCATEGORIZED_LABEL = "未分類"


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


def section_of(key: str) -> str:
    """設定 key の属するセクション（最後のドットより前）を返す."""
    return key.rsplit(".", 1)[0]


def settings_sections(fields: Sequence[SettingsField]) -> list[SettingsSection]:
    """設定項目を `SETTINGS_SECTIONS` の宣言どおりの表示ツリーへ組む.

    backend が返さなかった項目は空のカード・セクションを作らない。

    宣言に無い項目は末尾の「未分類」セクションへ TOML セクション単位でまとめる。
    """
    by_key = {field.key: field for field in fields}
    placed: set[str] = set()
    sections: list[SettingsSection] = []
    for spec in SETTINGS_SECTIONS:
        groups = tuple(
            SettingsGroup(label=group.label, fields=present)
            for group in spec.groups
            if (present := tuple(by_key[key] for key in group.keys if key in by_key))
        )
        placed.update(field.key for group in groups for field in group.fields)
        if groups:
            sections.append(
                SettingsSection(slug=spec.slug, label=spec.label, groups=groups)
            )
    if undeclared := [field for field in fields if field.key not in placed]:
        sections.append(_uncategorized_section(undeclared))
    return sections


def _uncategorized_section(fields: list[SettingsField]) -> SettingsSection:
    """宣言漏れの項目を TOML セクション単位でまとめる（見出しは生のセクション名）."""
    buckets: dict[str, list[SettingsField]] = {}
    for field in fields:
        buckets.setdefault(section_of(field.key), []).append(field)
    return SettingsSection(
        slug=UNCATEGORIZED_SLUG,
        label=UNCATEGORIZED_LABEL,
        groups=tuple(
            SettingsGroup(label=section, fields=tuple(group))
            for section, group in buckets.items()
        ),
    )
