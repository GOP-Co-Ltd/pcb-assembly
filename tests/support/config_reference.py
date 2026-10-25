"""docs/reference/config.md を、設定ページと同じ項目の一覧（setting_fields）から作る.

書き直すときは ``uv run python -m tests.support.config_reference > docs/reference/config.md`` を実行し、``make format`` で表を整える。
"""

from pcbasm.config.fields import SettingField, setting_fields

_HEADER = """\
# 設定キー一覧

[ドキュメント一覧](../../README.md) · [ファイルと環境変数](files.md)

`config/machine.toml` に書けるキーのうち、WebUI の設定ページ（機体タブの「マシン設定」）に並ぶ項目の一覧。
節は設定ページの 1 画面、カードは画面の中の見出しにあたる。
既定値のあるキーは書かなければ既定値で動き、設定ページでは空欄に既定値が薄く出る。
既定値の無いキーを書き忘れると設定エラーになる。

設定ページに出ない節が 2 つある。

- `[klipper]`: Moonraker の `host` と `port`。既定値は無く、必ず書く（テンプレートは `localhost` と `7125`）
- `[tact]`: 予約の節。書いてあっても中身は読まない

値の範囲は書込時と起動時に確かめ、範囲外なら設定エラーになる（[保守ガイド](../maintenance.md) の設定エラーからの復旧）。

このファイルは設定のスキーマから生成する。手で直さず、`uv run python -m tests.support.config_reference > docs/reference/config.md` の後に `make format` を実行する。
"""

_COLUMNS = ("カード", "キー", "表示名", "単位", "値")

_KINDS = {
    "float": "数値",
    "int": "整数",
    "str": "文字列",
    "float_or_auto": "数値か `auto`",
    "float_pair": "`[x, y]`",
    "float_triple": "座標。3 つそろえるか、すべて書かない",
}


def _key(field: SettingField) -> str:
    if field.set:
        parent = field.key.rsplit(".", 1)[0]
        return " / ".join(f"`{parent}.{name}`" for name in field.set)
    return f"`{field.key}`"


def _value(field: SettingField) -> str:
    if field.kind == "choice":
        text = " / ".join(f"`{value}`（{label}）" for value, label in field.choices)
    else:
        text = _KINDS[field.kind]
    if field.minimum is not None:
        text += f"（{field.minimum:g} 以上）"
    if field.clearable:
        text += "。空欄で無効"
    return text


def _sections() -> list[tuple[str, list[tuple[str, ...]]]]:
    """節の見出しと、その表の行（``_COLUMNS`` の順）を設定ページの並びで返す."""
    result: list[tuple[str, list[tuple[str, ...]]]] = []
    for field in setting_fields():
        heading = f"{field.section_label}（`[{field.section}]`）"
        if not result or result[-1][0] != heading:
            result.append((heading, []))
        result[-1][1].append(
            (field.group, _key(field), field.label, field.unit or "", _value(field))
        )
    return result


def _row(cells: tuple[str, ...]) -> str:
    return "| " + " | ".join(cells) + " |"


def render() -> str:
    """docs/reference/config.md の全文."""
    parts = [_HEADER]
    for heading, rows in _sections():
        table = [_row(_COLUMNS), _row(tuple("---" for _ in _COLUMNS)), *map(_row, rows)]
        parts.append(f"## {heading}\n\n" + "\n".join(table) + "\n")
    return "\n".join(parts)


if __name__ == "__main__":
    print(render(), end="")
