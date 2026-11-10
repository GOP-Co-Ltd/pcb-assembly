#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
#
# pnp-motion ファーム（BTT SKR Pico 用の .uf2）をビルドする。
#
# upstream の Klipper を klipper.commit の commit で取得し、patches/ を当て、
# core/ と module/ を src/pnp/ へ写してビルドする。出力は ${BUILD_DIR}/pnp-motion.uf2。
#
# 環境変数:
#   BUILD_DIR     ビルド用ディレクトリ（既定: ${XDG_CACHE_HOME:-~/.cache}/pcbasm/pnp-motion）。
#                 リポジトリの外に置き、Klipper のソースを pytest・pyright・ruff の対象に入れない
#   KLIPPER_REPO  Klipper の取得元（既定: upstream）。手元の clone を指せばネットワークなしで取得できる

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${BUILD_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/pcbasm/pnp-motion}"
# make -C は作業ディレクトリを移すので、絶対パスにしておく
mkdir -p "$BUILD_DIR"
BUILD_DIR="$(cd "$BUILD_DIR" && pwd)"
KLIPPER_REPO="${KLIPPER_REPO:-https://github.com/Klipper3d/klipper.git}"
KLIPPER_COMMIT="$(tr -d '[:space:]' <"${HERE}/klipper.commit")"
KLIPPER_DIR="${BUILD_DIR}/klipper"

die() {
    echo "エラー: $1" >&2
    exit 1
}

check_tools() {
    local tool
    # Klipper の導入スクリプトの依存（KIAUH が入れる）に含まれる
    for tool in git make arm-none-eabi-gcc g++; do
        command -v "$tool" >/dev/null || die "${tool} がありません"
    done
}

# 固定した commit の Klipper を、前回の patch と生成物を捨てた状態で用意する
fetch_klipper() {
    if [ ! -d "${KLIPPER_DIR}/.git" ]; then
        git clone --quiet --no-checkout "$KLIPPER_REPO" "$KLIPPER_DIR"
    fi
    if ! git -C "$KLIPPER_DIR" cat-file -e "${KLIPPER_COMMIT}^{commit}" 2>/dev/null; then
        git -C "$KLIPPER_DIR" fetch --quiet "$KLIPPER_REPO" "$KLIPPER_COMMIT"
    fi
    git -C "$KLIPPER_DIR" checkout --quiet --force --detach "$KLIPPER_COMMIT"
    git -C "$KLIPPER_DIR" clean --quiet -fdx
}

apply_sources() {
    local patch
    local version
    for patch in "${HERE}"/patches/*.patch; do
        git -C "$KLIPPER_DIR" apply "$patch"
    done
    mkdir -p "${KLIPPER_DIR}/src/pnp"
    cp "${HERE}"/core/*.[ch] "${HERE}"/module/*.c "${KLIPPER_DIR}/src/pnp/"
    # ファームが返す版（このディレクトリを最後に変えた commit）。ホストが接続時に確かめる。
    # ファームに関係しない commit では変わらないので、書き込み直しが要らない
    version="$(git -C "$HERE" log -1 --format=%h -- . 2>/dev/null)"
    if [ -n "$(git -C "$HERE" status --porcelain -- . 2>/dev/null)" ]; then
        version="${version}-dirty"
    fi
    printf '#define PNP_MOTION_VERSION "%s"\n' "$version" >"${KLIPPER_DIR}/src/pnp/pnp_version.h"
}

build() {
    local out="${BUILD_DIR}/out/"
    rm -rf "$out"
    cp "${HERE}/skr-pico.config" "${BUILD_DIR}/.config"
    make -s -C "$KLIPPER_DIR" KCONFIG_CONFIG="${BUILD_DIR}/.config" OUT="$out" olddefconfig
    make -s -C "$KLIPPER_DIR" KCONFIG_CONFIG="${BUILD_DIR}/.config" OUT="$out" -j"$(nproc)"
    cp "${out}klipper.uf2" "${BUILD_DIR}/pnp-motion.uf2"
    cp "${out}klipper.dict" "${BUILD_DIR}/pnp-motion.dict"
}

main() {
    check_tools
    fetch_klipper
    apply_sources
    build
    echo "ビルド: ${BUILD_DIR}/pnp-motion.uf2"
    echo "データ辞書: ${BUILD_DIR}/pnp-motion.dict"
}

main "$@"
