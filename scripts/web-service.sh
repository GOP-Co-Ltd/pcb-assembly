#!/bin/bash
# 旧版の自己更新が unit の drift 判定に呼ぶ互換の入口。install.sh へ委譲するだけ。
# 両機が install.sh を含む版を越えたら消す。

set -euo pipefail

INSTALL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/install.sh"

case "${1:-}" in
    render) exec "${INSTALL}" render "${2:-api}" ;;
    install) exec "${INSTALL}" services "${2:-api}" ;;
    *)
        echo "Usage: $(basename "$0") <render|install> [api|ui|all]（install.sh を使ってください）" >&2
        exit 2
        ;;
esac
