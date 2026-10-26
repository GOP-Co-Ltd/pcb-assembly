"""移行で machine.toml がどう変わるかを、書き換えずに差分で表示する.

使い方: ``python -m pcbasm.migrate --check PATH``
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

from pcbasm.migrate.machine_toml import MigrationError, migrate_text


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pcbasm.migrate",
        description="machine.toml の移行で変わる箇所を、書き換えずに表示する",
    )
    parser.add_argument(
        "--check", metavar="PATH", type=Path, required=True, help="machine.toml"
    )
    path: Path = parser.parse_args().check
    original = path.read_text()
    try:
        migrated, changes = migrate_text(original)
    except MigrationError as exc:
        print(f"移行できません: {exc}", file=sys.stderr)
        return 1
    if not changes:
        print("移行は要りません")
        return 0
    sys.stdout.writelines(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            migrated.splitlines(keepends=True),
            str(path),
            f"{path}（移行後）",
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
