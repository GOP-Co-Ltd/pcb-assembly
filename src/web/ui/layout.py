"""フォームの表示定義（ジョブフォームのセクション分けと、設定ページのまとめ方）.

ここに置くのは純粋な表示定義だけ。タブ・機能・テンプレートは `web.ui.features` が持つ。
装置の事実（フレーム提供の有無・progress_stage 文字列・ジョブのパラメータ定義）は
ここには置かない。それらは backend が `GET /api/jobs` の `JobSpecInfo` で自己申告し、
frontend はその値をテンプレートへ渡すだけにする。

設定ページの項目・見出し・並び順も backend が `GET /api/machine_config` で返す。
`settings_sections` はそれを画面とカードへまとめるだけで、並べ替えない。
"""

from __future__ import annotations

from collections.abc import Sequence

import attrs

from web.contract.models import SettingsField

# dispense_calibration フォームのセクション分け（表示のみ）。
# ①②③ の依存順に沿ってパラメータを視覚的にグルーピングする。
DISPENSE_CALIBRATION_PARAM_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "銅板（キャリブ後固定）",
        ("board_width", "board_height"),
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
