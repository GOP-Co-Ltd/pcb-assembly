"""手順が呼び出し側へ経過を渡し、中断を受け取る口.

撮影や塗布の手順は pcbasm に置き、web のジョブはこの口にログ・進捗・中断・画像の表示を結ぶ。
web の ``JobContext`` がこれを継承する。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pcbasm.vision import Image


class ProcedureHooks(ABC):
    @abstractmethod
    def log(self, message: str) -> None:
        """運転者向けの 1 行を出す."""

    @abstractmethod
    def progress(self, stage: str, percent: float | None = None) -> None:
        """いまの段階と進み具合 [%] を出す."""

    @abstractmethod
    def checkpoint(self) -> None:
        """中断を要求されていれば例外を送出する."""

    @abstractmethod
    def frame(self, image: Image) -> None:
        """撮った画像を表示へ流す."""
