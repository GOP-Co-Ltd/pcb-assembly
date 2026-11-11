#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
#
# pnp-motion ファーム（BTT SKR Pico 用の .uf2）をビルドする。
#
# submodule の klipper/（upstream の Klipper を commit で固定）を build/klipper/ へ書き出し、
# patches/ を当て、core/ と module/ を src/pnp/ へ写してビルドする。
# submodule は書き換えない（patch は書き出した側に当てる）。出力は build/pnp-motion.uf2。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUBMODULE="${HERE}/klipper"
BUILD_DIR="${HERE}/build"
KLIPPER_DIR="${BUILD_DIR}/klipper"
# ファームの中身を決めるもの。ファームの版（PNP_MOTION_VERSION）は、これを最後に変えた commit
FIRMWARE_SOURCES=(core module patches klipper skr-pico.config build.sh)

die() {
    echo "エラー: $1" >&2
    exit 1
}

check_tools() {
    local tool
    # Klipper の導入スクリプトの依存（KIAUH が入れる）に含まれる
    for tool in git make patch tar arm-none-eabi-gcc g++; do
        command -v "$tool" >/dev/null || die "${tool} がありません"
    done
}

# submodule を、親が記録した commit に揃える（未取得なら取得し、その commit がなければ fetch する）。
# 自己更新（fetch → merge --ff-only）は submodule を動かさないので、ビルドのたびに揃える
sync_submodule() {
    git -C "$HERE" submodule update --init -- klipper
}

# 記録された commit の Klipper を、前回の patch と生成物のない状態で書き出す
export_klipper() {
    local commit
    # submodule の作業ツリーではなく、親の index が記録した commit を書き出す
    # （submodule の中で checkout を動かしていても、固定した版でビルドする）
    commit="$(git -C "$HERE" ls-files --stage -- klipper | awk '{print $2}')"
    [ -n "$commit" ] || die "submodule の commit が見つかりません"
    rm -rf "$KLIPPER_DIR"
    mkdir -p "$KLIPPER_DIR"
    git -C "$SUBMODULE" archive "$commit" | tar -x -C "$KLIPPER_DIR"
    # Klipper はデータ辞書の版を .git か klippy/.version から取る。書き出しには .git がないので書いておく
    git -C "$SUBMODULE" describe --always --tags --long "$commit" >"${KLIPPER_DIR}/klippy/.version"
}

apply_sources() {
    local patch
    local version
    for patch in "${HERE}"/patches/*.patch; do
        # 文脈がずれたら当てずに失敗させる（Klipper を上げたときに、ずれた当たり方に気づけるように）
        patch --batch --fuzz=0 --quiet --strip=1 --directory="$KLIPPER_DIR" <"$patch"
    done
    mkdir -p "${KLIPPER_DIR}/src/pnp"
    cp "${HERE}"/core/*.[ch] "${HERE}"/module/*.c "${KLIPPER_DIR}/src/pnp/"
    # ファームが返す版（ファームのソースを最後に変えた commit）。ホストが接続時に確かめる。
    # ファームに関係しない commit では変わらないので、書き込み直しが要らない
    version="$(git -C "$HERE" log -1 --format=%h -- "${FIRMWARE_SOURCES[@]}")"
    if [ -n "$(git -C "$HERE" status --porcelain -- "${FIRMWARE_SOURCES[@]}")" ]; then
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
    sync_submodule
    export_klipper
    apply_sources
    build
    echo "ビルド: ${BUILD_DIR}/pnp-motion.uf2"
    echo "データ辞書: ${BUILD_DIR}/pnp-motion.dict"
}

main "$@"
