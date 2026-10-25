"""フォームのまとめ方（ジョブフォームのまとまりと、設定ページの画面・カード）.

ここに置くのは純粋な表示の組み立てだけ。タブ・機能・テンプレートは `web.ui.features` が持つ。
装置の事実（フレーム提供の有無・progress_stage 文字列・ジョブのパラメータ定義とそのまとまり）は
ここには置かない。それらは backend が `GET /api/jobs` の `JobSpecInfo` で自己申告する。

設定ページの項目・見出し・並び順も backend が `GET /api/machine_config` で返す。
`param_sections` と `settings_sections` は backend が返した順のまままとめるだけで、並べ替えない。
"""

from __future__ import annotations

from collections.abc import Sequence

import attrs

from web.contract.models import ParamSpecInfo, SettingsField


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


@attrs.frozen
class ParamSection:
    """描画用のジョブフォームのまとまり 1 つ（``label`` が None ならまとめずに並べる）."""

    label: str | None
    advanced: bool
    specs: tuple[ParamSpecInfo, ...]


def param_sections(specs: Sequence[ParamSpecInfo]) -> list[ParamSection]:
    """Backend が返した順のパラメータを、``ParamSpec.group`` のまとまりへ分ける."""
    tree: dict[tuple[str | None, bool], list[ParamSpecInfo]] = {}
    for spec in specs:
        key = (
            (None, False)
            if spec.group is None
            else (spec.group.label, spec.group.advanced)
        )
        tree.setdefault(key, []).append(spec)
    return [
        ParamSection(label=label, advanced=advanced, specs=tuple(members))
        for (label, advanced), members in tree.items()
    ]
