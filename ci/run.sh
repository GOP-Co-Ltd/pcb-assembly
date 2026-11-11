#!/bin/bash
# CI の job のコマンドを、ci/Containerfile の image から作った使い捨ての container で動かす。
# image は runner ごとにローカルで build し、Containerfile の hash を tag にする（registry は使わない）。
#
#   ci/run.sh <command> [args...]
#
# rootless の podman で動かすので、container 内の root は host では runner user になる。
# workspace に書いたファイルは runner user の所有になる。

set -euo pipefail

readonly IMAGE_NAME="localhost/pcbasm-ci"
# workflow が設定し、container の中でも要る環境変数
readonly PASS_ENV=(UV_NO_SYNC OPENBLAS_NUM_THREADS OMP_NUM_THREADS PYTEST_ADDOPTS)

ci_dir="$(cd "$(dirname "$0")" && pwd)"
workspace="$(dirname "${ci_dir}")"
image="${IMAGE_NAME}:$(sha256sum "${ci_dir}/Containerfile" | cut -c1-16)"

# runner は 1 台に 1 instance なので、同じ host で build が並行することはない
if ! podman image exists "${image}"; then
    podman build --tag "${image}" --file "${ci_dir}/Containerfile" "${ci_dir}"
    # 前の Containerfile の image を消す
    podman image ls --filter "reference=${IMAGE_NAME}" --format '{{.Repository}}:{{.Tag}}' |
        { grep --invert-match --line-regexp --fixed-strings "${image}" || true; } |
        xargs --no-run-if-empty podman rmi --force
    podman image prune --force
fi

env_args=()
for name in "${PASS_ENV[@]}"; do
    if [ -n "${!name+set}" ]; then
        env_args+=(--env "${name}")
    fi
done

# uv、pre-commit、pyright の node などの cache は host の runner user の ~/.cache に残し、job をまたいで使う。
# ゲームパッドのテストは /dev/uinput で仮想デバイスを作り、その /dev/input/event* を読む（keep-groups で uinput group を引き継ぐ）。
# network は host と共有する（E2E の server と mDNS のテストを host で動かしていたときと同じにする）
mkdir -p "${HOME}/.cache"
exec podman run --rm --init \
    --network host \
    --shm-size 1g \
    --device /dev/uinput \
    --group-add keep-groups \
    --volume /dev/input:/dev/input \
    --volume "${HOME}/.cache:/root/.cache" \
    --volume "${workspace}:${workspace}" \
    --workdir "${PWD}" \
    "${env_args[@]}" \
    "${image}" \
    "$@"
