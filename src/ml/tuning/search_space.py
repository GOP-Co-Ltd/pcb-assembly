"""探索する parameter とその分布の宣言.

parameter 名は設定の dotted path そのままにする。

探索した値を ``ConfigComposition`` の上書きへ素通しできるので、探索の run と単発の
run が同じ設定経路を通る。

分布は tagged union にせず、1 個のクラスと ``kind`` で表す。

union の structure hook を strict converter へ足さずに TOML から読めるようにする
ため（``kind`` に対して不要なフィールドが埋まっていないことは
:meth:`ParameterDistribution.validate` が理由文字列で弾く）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal, cast

import attrs
import optuna

from ml.artifact.fingerprint import fingerprint_json
from ml.experiment.logger import Scalar

type DistributionKind = Literal["float", "integer", "categorical"]

DISTRIBUTION_KINDS: tuple[DistributionKind, ...] = ("float", "integer", "categorical")

_DEFAULT_INTEGER_STEP = 1


@attrs.frozen
class ParameterDistribution:
    """1 個の parameter を引く範囲.

    ``kind`` ごとに意味を持つフィールドが違う。使わないフィールドは既定値のまま
    残す（``float`` に ``choices`` を書くと :meth:`validate` が理由を返す）。

    ``low`` / ``high`` は ``kind="integer"`` でも ``2.0`` と float で書く。

    attrs は型を変換しないので ``low=2`` は int のまま入り、``2`` と ``2.0`` で
    :attr:`SearchSpace.fingerprint` が変わる。同じ論理空間が別 study へ分かれるので
    :meth:`validate` が int の境界を拒否する。
    """

    kind: DistributionKind
    low: float | None = None
    high: float | None = None
    log: bool = False
    step: int | None = None
    choices: tuple[Scalar, ...] = ()

    def validate(self) -> str | None:
        """``kind`` に対してフィールドが揃っているかを検証する."""

        if self.kind not in DISTRIBUTION_KINDS:
            return f"kind は {list(DISTRIBUTION_KINDS)} のいずれかが必要です: {self.kind!r}"
        if self.kind == "categorical":
            return self._validate_categorical()
        return self._validate_numeric()

    def suggest(self, trial: optuna.trial.Trial, *, name: str) -> Scalar:
        """この分布から 1 個の値を引く.

        呼ぶ前に :meth:`validate` を通す前提。分布として成立していない組み合わせは
        ここでは理由を返せない（optuna へ渡す値が決まらない）。
        """

        if self.kind == "categorical":
            # ``suggest_categorical`` の戻り型は ``None`` を含むが、渡した ``choices`` は
            # ``tuple[Scalar, ...]`` なので None は返り得ない。到達不能な防御分岐は置かず
            # narrowing だけ cast で済ませる（裁定 5）。
            return cast("Scalar", trial.suggest_categorical(name, list(self.choices)))
        # ``low`` / ``high`` が None でないことは :meth:`validate` が保証する（裁定 5）。
        low, high = cast("float", self.low), cast("float", self.high)
        if self.kind == "float":
            return trial.suggest_float(name, low, high, log=self.log)
        return trial.suggest_int(
            name,
            int(low),
            int(high),
            step=self.step if self.step is not None else _DEFAULT_INTEGER_STEP,
            log=self.log,
        )

    def _validate_categorical(self) -> str | None:
        if not self.choices:
            return "categorical は choices が必要です"
        if self.low is not None or self.high is not None:
            return f"categorical に low / high は書けません: {self.low}, {self.high}"
        if self.log:
            return "categorical に log は書けません"
        if self.step is not None:
            return f"categorical に step は書けません: {self.step}"
        return None

    def _validate_numeric(self) -> str | None:
        if self.choices:
            return f"{self.kind} に choices は書けません: {list(self.choices)}"
        if self.low is None or self.high is None:
            return f"{self.kind} は low と high が必要です: {self.low}, {self.high}"
        for name, bound in (("low", self.low), ("high", self.high)):
            # ``2`` と ``2.0`` で fingerprint が変わる。TOML 側は strict converter が
            # int を拒むので、Python 側も float だけを受けて表現を 1 つに保つ。
            if type(bound) is not float:
                return (
                    f"{name} は float で書いてください（{name}=2 と {name}=2.0 で "
                    f"fingerprint が変わります）: {bound!r}"
                )
        if not math.isfinite(self.low) or not math.isfinite(self.high):
            return f"low と high は有限値が必要です: {self.low}, {self.high}"
        if self.low >= self.high:
            return f"low < high が必要です: {self.low}, {self.high}"
        if self.kind == "integer" and not (
            self.low.is_integer() and self.high.is_integer()
        ):
            # 黙って切り捨てると、記録した範囲と実際に探した範囲が食い違う。
            return f"integer は整数の low と high が必要です: {self.low}, {self.high}"
        if self.log and self.low <= 0:
            return f"log スケールは正の low が必要です: {self.low}"
        if self.kind == "float" and self.step is not None:
            return f"float に step は書けません: {self.step}"
        if self.step is not None and self.step < 1:
            return f"step は正の整数が必要です: {self.step}"
        if self.log and self.step is not None and self.step != _DEFAULT_INTEGER_STEP:
            # optuna は log スケールで step != 1 を常に拒否する。validate で塞がないと
            # suggest が投げ、``catch=(Exception,)`` に飲まれて全 trial が黙って失敗する。
            return f"log スケールでは step を 1 以外にできません: {self.step}"
        return None


@attrs.frozen
class SearchSpace:
    """探索する parameter 一式.

    キーは設定の dotted path（例 ``trainer.learning_rate``）。TOML では
    ``[search_space."trainer.learning_rate"]`` と引用キーで書く。
    """

    parameters: Mapping[str, ParameterDistribution]

    def validate(self) -> str | None:
        """探索空間として成立しているかを検証する."""

        if not self.parameters:
            return "search space は 1 個以上の parameter が必要です"
        for name, distribution in sorted(self.parameters.items()):
            if not name:
                return "parameter 名は空にできません"
            if error := distribution.validate():
                return f"{name}: {error}"
        return None

    @property
    def fingerprint(self) -> str:
        """探索空間の内容から決まる fingerprint.

        parameter の並び順には依存しない（``canonical_json`` がキーを整列する）。
        """

        return fingerprint_json(
            {
                name: attrs.asdict(distribution)
                for name, distribution in self.parameters.items()
            }
        )

    def suggest(self, trial: optuna.trial.Trial) -> dict[str, Scalar]:
        """全 parameter を引き、dotted path から値への mapping を返す."""

        return {
            name: distribution.suggest(trial, name=name)
            for name, distribution in sorted(self.parameters.items())
        }


__all__ = [
    "DISTRIBUTION_KINDS",
    "DistributionKind",
    "ParameterDistribution",
    "SearchSpace",
]
