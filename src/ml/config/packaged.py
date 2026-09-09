"""Wheel へ同梱する設定 group の所在.

設定は package の中に置き、成果物と同じ wheel で配布する。

install 済みの環境が、リポジトリの作業ツリーを持たなくても同じ設定を読めるため。

``importlib.resources`` は使わない。

wheel は zip ではなく展開して install されるので、素の :class:`~pathlib.Path` で
足りる（``src/pcbasm/config.py`` も同じ扱い）。
"""

from __future__ import annotations

from pathlib import Path

import attrs

_CONFIGURATION_DIRECTORY_NAME = "conf"
_LAYER_SUFFIX = ".toml"


@attrs.frozen
class PackagedConfiguration:
    """同梱設定の root ディレクトリ.

    直下のディレクトリが group、その中の ``*.toml`` が option になる。
    """

    root: Path

    @classmethod
    def locate(cls) -> PackagedConfiguration:
        """この package に同梱された設定 root を指す."""

        return cls(root=Path(__file__).parent / _CONFIGURATION_DIRECTORY_NAME)

    def validate(self) -> str | None:
        """設定 root が実在するディレクトリかを検証する."""

        if not self.root.is_dir():
            return f"同梱設定のディレクトリがありません: {self.root}"
        return None

    def group_names(self) -> tuple[str, ...]:
        """整列済みの group 名を返す."""

        if not self.root.is_dir():
            return ()
        return tuple(
            sorted(entry.name for entry in self.root.iterdir() if entry.is_dir())
        )

    def option_names(self, group: str) -> tuple[str, ...]:
        """``group`` が持つ整列済みの option 名を返す.

        group が実在しなければ空 tuple を返す。
        """

        directory = self.root / group
        if not directory.is_dir():
            return ()
        return tuple(sorted(path.stem for path in directory.glob(f"*{_LAYER_SUFFIX}")))


__all__ = [
    "PackagedConfiguration",
]
