"""TOML の層と CLI 上書きから 1 個の設定を組み立てる境界.

層は順序付きのリストで、後の層が前の層に勝つ。

mapping は再帰的に merge するが、list は merge せず置換する。

list の要素単位の合成は「どの要素と対応するか」を決める規則が要るうえ、
設定として意味の定まらない中間状態を作るため採らない。

上書きの値は ``tomllib`` に解釈させる。

TOML の scalar 規則を 1 箇所に保ち、``true`` や ``1.0e-4`` の読み方を二重に
定義しないため。

解釈できない token は文字列として扱う。

shell がクォートを食うので、``monitor=mae`` を TOML 文字列として渡す手段が
実質無い。型の正しさは strict converter が守る。

ただし引用符で始まる token は理由を返す。

``monitor="loss`` は引用符を書こうとして失敗した形であり、``str`` フィールドは
converter が守れないので黙って ``'"loss'`` を採用させない。

合成の失敗は例外ではなく理由文字列で返す（``memory/feedback_no_try_catch.md``）。
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

import attrs
from cattrs import Converter

from ml.serialization import structure_strictly

type ConfigMapping = Mapping[str, object]

_LAYER_SUFFIX = ".toml"
_OVERRIDE_SEPARATOR = "="
_PATH_SEPARATOR = "."
_QUOTE_CHARACTERS = ('"', "'")


@attrs.frozen
class ConfigComposition:
    """1 個の設定を作るための層と上書きの指定.

    ``layer_paths`` は積む順、``overrides`` は ``key=value`` 形式の token。

    どちらも実際の読み込みは :meth:`compose` まで遅延する。
    """

    layer_paths: tuple[Path, ...] = ()
    overrides: tuple[str, ...] = ()

    @classmethod
    def from_arguments(
        cls,
        arguments: Sequence[str],
        *,
        configuration_root: Path,
        base_names: Sequence[str] = (),
    ) -> tuple[ConfigComposition | None, str | None]:
        """CLI の token 列を層の指定と上書きへ振り分ける.

        ``name=value`` の ``name`` が ``configuration_root`` 直下のディレクトリとして
        実在すれば group 選択とみなし、``configuration_root / name / value.toml``
        を層として積む。

        実在しなければ設定値の上書きとみなす。

        判定をディレクトリの実在に委ねたのは、``run_kind=finetune`` のような
        非 dotted の上書きを group 選択と読み違えないため。
        """

        root = Path(configuration_root)
        base_layers = [root / f"{name}{_LAYER_SUFFIX}" for name in base_names]
        group_layers: list[Path] = []
        overrides: list[str] = []
        for argument in arguments:
            name, separator, value = argument.partition(_OVERRIDE_SEPARATOR)
            if not separator or not name:
                return None, f"引数は key=value 形式で指定してください: {argument!r}"
            group_directory = root / name
            if not group_directory.is_dir():
                overrides.append(argument)
                continue
            option = group_directory / f"{value}{_LAYER_SUFFIX}"
            if not option.is_file():
                return None, (
                    f"group {name!r} に option {value!r} がありません: {option}"
                )
            group_layers.append(option)
        return (
            cls(
                layer_paths=(*base_layers, *group_layers),
                overrides=tuple(overrides),
            ),
            None,
        )

    def validate(self) -> str | None:
        """層のパスと上書き token の形を検証する."""

        for path in self.layer_paths:
            if not path.is_file():
                return f"設定層が見つかりません: {path}"
        for override in self.overrides:
            key, separator, _ = override.partition(_OVERRIDE_SEPARATOR)
            if not separator or not key:
                return f"上書きは key=value 形式で指定してください: {override!r}"
            if any(not part for part in key.split(_PATH_SEPARATOR)):
                return f"上書きのキーに空の要素があります: {override!r}"
        return None

    def compose(self) -> tuple[dict[str, object] | None, str | None]:
        """層を順に merge し、最後に上書きを適用した素の dict を返す."""

        if error := self.validate():
            return None, error
        composed: dict[str, object] = {}
        for path in self.layer_paths:
            layer, error = _load_layer(path)
            if layer is None:
                return None, error
            _merge_into(composed, layer)
        for override in self.overrides:
            key, _, raw = override.partition(_OVERRIDE_SEPARATOR)
            value, error = _parse_override_value(raw)
            if error is not None:
                return None, error
            if error := _assign(composed, key.split(_PATH_SEPARATOR), value):
                return None, error
        return composed, None

    def structure[T](
        self,
        target: type[T],
        *,
        converter: Converter,
    ) -> tuple[T | None, str | None]:
        """合成した dict を ``target`` へ厳格に構造化する.

        converter は生成せず受け取る。

        未知キーと暗黙の型変換を拒否する境界を 1 箇所に保つため。
        """

        composed, error = self.compose()
        if composed is None:
            return None, error
        return structure_strictly(composed, target, converter=converter)


def _load_layer(path: Path) -> tuple[ConfigMapping | None, str | None]:
    """1 個の TOML 層を読み、失敗したら理由を返す."""

    try:
        with path.open("rb") as stream:
            return tomllib.load(stream), None
    except (OSError, tomllib.TOMLDecodeError) as error:
        return None, f"設定層を読めません: {path}（{error}）"


def _merge_into(target: dict[str, object], source: ConfigMapping) -> None:
    """後勝ちで ``source`` を ``target`` へ再帰 merge する."""

    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            _merge_into(existing, value)
            continue
        target[key] = value


def _parse_override_value(raw: str) -> tuple[object, str | None]:
    """上書きの値を TOML の scalar 規則で解釈する.

    TOML として解釈できない token は文字列として扱う。

    shell はクォートを食うので、``monitor=mae`` のような裸の文字列が
    そのままプロセスへ届く。

    引用符で始まる token だけは文字列へ落とさず理由を返す。

    裸の文字列に引用符は現れないので、``monitor="loss`` は引用符の閉じ忘れだと
    確定できる。到達先が ``str`` なら converter も守れない。

    型の正しさを守るのは strict converter であってこの層ではない。
    """

    try:
        return tomllib.loads(f"value = {raw}")["value"], None
    except tomllib.TOMLDecodeError:
        if raw.startswith(_QUOTE_CHARACTERS):
            return None, f"上書きの値の引用符が閉じていません: {raw!r}"
        return raw, None


def _assign(node: dict[str, object], path: Sequence[str], value: object) -> str | None:
    """Dotted path の位置へ値を書き、必要なら中間の table を作る."""

    *parents, leaf = path
    current = node
    for part in parents:
        child = current.get(part)
        if child is None:
            child = {}
            current[part] = child
        if not isinstance(child, dict):
            return (
                f"上書き先が table ではありません: {_PATH_SEPARATOR.join(path)!r} の "
                f"{part!r}"
            )
        current = child
    current[leaf] = value
    return None


__all__ = [
    "ConfigComposition",
    "ConfigMapping",
]
