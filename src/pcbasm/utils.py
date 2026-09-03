import logging
import math
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import TypeGuard

PROJECT_ROOT = Path(__file__).parent.parent.parent


def setup_logging(
    level: int = logging.INFO,
    namespaces: Iterable[str] = ("__main__", "pcbasm"),
) -> None:
    """Setup basic logging configuration.

    Args:
        level: The logging level. Defaults to INFO.
        namespaces: The namespaces to log.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )

    for namespace in namespaces:
        logger = logging.getLogger(namespace)
        logger.setLevel(level)
        logger.addHandler(handler)


def get_class_module_path(cls: type) -> str:
    """Get the module path of a class.

    Args:
        cls: The class to get the module path.

    Returns:
        str: The module path of the class.
    """
    return f"{cls.__module__}.{cls.__name__}"


def is_finite_number(value: object) -> TypeGuard[int | float]:
    """``bool`` を除く有限な ``int`` / ``float`` かを判定する.

    float に収まらない巨大な int（``math.isfinite`` が ``OverflowError`` を投げる）
    も ``False`` とする。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int) and abs(value) > sys.float_info.max:
        return False
    return math.isfinite(value)
