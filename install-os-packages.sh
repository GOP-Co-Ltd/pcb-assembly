#!/bin/bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ref=""

if [ "${1:-}" = "--ref" ]; then
    ref="${2:-}"
    if [ -z "${ref}" ] || [ "${#}" -ne 2 ]; then
        echo "Usage: $0 [--ref <git-ref>]" >&2
        exit 2
    fi
elif [ "${#}" -ne 0 ]; then
    echo "Usage: $0 [--ref <git-ref>]" >&2
    exit 2
fi

manifest="${PROJECT_ROOT}/deploy/os-packages.txt"
temporary=""
if [ -n "${ref}" ]; then
    temporary="$(mktemp)"
    trap 'rm -f "${temporary}"' EXIT
    git -C "${PROJECT_ROOT}" show "${ref}:deploy/os-packages.txt" >"${temporary}"
    manifest="${temporary}"
fi

packages=()
while IFS= read -r line; do
    line="${line%%#*}"
    read -r line <<<"${line}"
    [ -n "${line}" ] && packages+=("${line}")
done <"${manifest}"

if [ "${#packages[@]}" -eq 0 ]; then
    echo "エラー: OS package manifest が空です: ${manifest}" >&2
    exit 1
fi

sudo apt-get update
sudo apt-get install -y "${packages[@]}"
