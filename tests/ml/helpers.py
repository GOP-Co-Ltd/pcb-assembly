"""``tests/ml`` 専用のテストヘルパー.

``ml`` は装置ドメインを知らない ML 基盤なので、そのテストも装置ドメインの
テストヘルパー (``tests.helpers``) に依存させない。``tests.helpers`` は
pcbnew / picamera2 を module 冒頭で import するため、Raspberry Pi と KiCAD が
無い GPU 学習ワークステーションでは読み込めない。``tests/ml`` がそれを参照すると
ML の開発サイクルが装置依存に引きずられる。この分離は
``tests/ml/test_architecture.py`` が機械検証する。
"""

from collections.abc import Callable
from functools import cache, wraps
from pathlib import Path
from typing import ParamSpec, TypeVar

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent

ML_SOURCE_ROOT = PROJECT_ROOT / "src" / "ml"

_P = ParamSpec("_P")
_R = TypeVar("_R")


@cache
def _inductor_compile_available() -> bool:
    """``torch.compile`` の inductor backend が使えるか小さな関数で確認する.

    Inductor は C++ compiler と生成コードのビルドに依存するため、環境によっては
    初回 compile で失敗する。初回 compile に数秒かかるので結果はキャッシュする。
    """
    try:
        import torch

        def add_one(values: torch.Tensor) -> torch.Tensor:
            return values + 1

        compiled = torch.compile(add_one, backend="inductor")
        return bool(torch.equal(compiled(torch.zeros(2)), torch.ones(2)))
    except Exception:
        return False


def skip_if_no_inductor(test: Callable[_P, _R]) -> Callable[_P, _R]:
    """inductor backend が使えない環境ではテストを skip する."""

    @wraps(test)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        if not _inductor_compile_available():
            pytest.skip("torch.compile の inductor backend が使えません")
        return test(*args, **kwargs)

    return wrapper
