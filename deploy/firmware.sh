#!/bin/bash
# BTT SKR Pico v1.0 に MCU firmware をビルドして書き込む（install.sh firmware が呼ぶ）。
#   klipper     KIAUH が入れた ~/klipper（Klippy と同じ版）の Klipper firmware。はんだ塗布機
#   pnp-motion  Klipper に速度指令のモジュールを足したもの（src/firmware/pnp-motion）。部品実装機
# 引数を省くと、どれを書き込むか（またはスキップするか）を選んでもらう。

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIRMWARE_CONFIG="${PROJECT_ROOT}/deploy/firmware/btt-skr-pico-v1-usb.config"
KLIPPER_DIR="${HOME}/klipper"
PNP_MOTION_DIR="${PROJECT_ROOT}/src/firmware/pnp-motion"
FIRMWARES=(klipper pnp-motion)
MEDIA_USER="${SUDO_USER:-${USER:-$(id -un)}}"
RPI_RP2_VOLUME="/media/${MEDIA_USER}/RPI-RP2"
SERIAL_BY_ID_DIR="/dev/serial/by-id"
VOLUME_WAIT_SECONDS=60
SERIAL_WAIT_SECONDS=30

die() {
    echo "エラー: $1" >&2
    exit 1
}

choose_number() {
    local prompt="$1"
    local minimum="$2"
    local maximum="$3"
    local choice

    while true; do
        read -rp "$prompt" choice || die "中止しました"
        if [[ "$choice" =~ ^[0-9]+$ ]] &&
            [ "$choice" -ge "$minimum" ] && [ "$choice" -le "$maximum" ]; then
            printf '%s' "$choice"
            return
        fi
        echo "無効な選択です。${minimum}から${maximum}の間で入力してください。" >&2
    done
}

# 書き込む firmware を返す。$1 があれば確かめて返し、空なら選んでもらう（スキップなら空）
choose_firmware() {
    local firmware="${1:-}"
    local choice

    if [ -n "$firmware" ]; then
        case " ${FIRMWARES[*]} " in
            *" ${firmware} "*) printf '%s' "$firmware" ;;
            *) die "知らない firmware です: ${firmware}（${FIRMWARES[*]}）" ;;
        esac
        return
    fi
    echo "MCU firmware（BTT SKR Pico v1.0）を書き込みます:" >&2
    echo "  1. klipper（はんだ塗布機。KIAUH の ~/klipper からビルド）" >&2
    echo "  2. pnp-motion（部品実装機。src/firmware/pnp-motion からビルド）" >&2
    echo "  3. 今回はスキップ" >&2
    choice="$(choose_number "番号を選択してください (1-3): " 1 3)"
    [ "$choice" -eq 3 ] || printf '%s' "${FIRMWARES[$((choice - 1))]}"
}

find_rpi_rp2_volume() {
    [ -d "$RPI_RP2_VOLUME" ] || return 1
    printf '%s' "$RPI_RP2_VOLUME"
}

wait_for_rpi_rp2_volume() {
    local elapsed=0
    local volume

    echo "${RPI_RP2_VOLUME} を待っています（最大 ${VOLUME_WAIT_SECONDS} 秒）..." >&2
    while [ "$elapsed" -le "$VOLUME_WAIT_SECONDS" ]; do
        if volume="$(find_rpi_rp2_volume)"; then
            printf '%s' "$volume"
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    return 1
}

find_klipper_serial() {
    local serial_path

    [ -d "$SERIAL_BY_ID_DIR" ] || return 1
    while IFS= read -r serial_path; do
        case "$(basename "$serial_path")" in
            *Klipper* | *klipper*)
                printf '%s' "$serial_path"
                return 0
                ;;
        esac
    done < <(find "$SERIAL_BY_ID_DIR" -mindepth 1 -maxdepth 1 -print 2>/dev/null | sort)
    return 1
}

wait_for_klipper_serial() {
    local elapsed=0
    local serial_path

    echo "Klipper の serial ID を待っています（最大 ${SERIAL_WAIT_SECONDS} 秒）..." >&2
    while [ "$elapsed" -le "$SERIAL_WAIT_SECONDS" ]; do
        if serial_path="$(find_klipper_serial)"; then
            printf '%s' "$serial_path"
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    return 1
}

build_klipper_firmware() {
    # KIAUH が入れた ~/klipper で、ホストの Klippy と同じ版の firmware を $1 へビルドする。
    # ~/klipper の .config と out/ は使わない（KIAUH の操作と混ざらないようにするため）。
    local build_dir="$1"
    local tool

    [ -d "$KLIPPER_DIR" ] || die "${KLIPPER_DIR} がありません（先に klipper 段を実行してください）"
    [ -f "$FIRMWARE_CONFIG" ] || die "firmware の設定が見つかりません: ${FIRMWARE_CONFIG}"
    # Klipper の導入スクリプトの依存（KIAUH が入れる）に含まれる
    for tool in arm-none-eabi-gcc g++ make; do
        command -v "$tool" >/dev/null || die "${tool} がありません（KIAUH で Klipper を入れ直してください）"
    done

    cp "$FIRMWARE_CONFIG" "${build_dir}/.config"
    echo "Klipper firmware をビルドします（${KLIPPER_DIR}）..."
    make -s -C "$KLIPPER_DIR" KCONFIG_CONFIG="${build_dir}/.config" OUT="${build_dir}/out/" olddefconfig
    make -s -C "$KLIPPER_DIR" KCONFIG_CONFIG="${build_dir}/.config" OUT="${build_dir}/out/" \
        -j"$(nproc)" >"${build_dir}/build.log" 2>&1 ||
        die "ビルドに失敗しました。ログ: ${build_dir}/build.log"
    echo "ビルド: ${build_dir}/out/klipper.uf2"
}

build_pnp_motion_firmware() {
    # pnp-motion をビルドする。初回は submodule の Klipper を GitHub から取得する。
    # 出力は ${PNP_MOTION_DIR}/build/pnp-motion.uf2
    local build_dir="$1"

    echo "pnp-motion firmware をビルドします（${PNP_MOTION_DIR}）..."
    "${PNP_MOTION_DIR}/build.sh" >"${build_dir}/build.log" 2>&1 ||
        die "ビルドに失敗しました。ログ: ${build_dir}/build.log"
    echo "ビルド: ${PNP_MOTION_DIR}/build/pnp-motion.uf2"
}

flash_btt_skr_pico() {
    local firmware="$1"
    local build_dir
    local firmware_file
    local volume
    local serial_path

    build_dir="$(mktemp -d)"
    case "$firmware" in
        klipper)
            build_klipper_firmware "$build_dir"
            firmware_file="${build_dir}/out/klipper.uf2"
            ;;
        pnp-motion)
            build_pnp_motion_firmware "$build_dir"
            firmware_file="${PNP_MOTION_DIR}/build/pnp-motion.uf2"
            ;;
    esac

    echo ""
    echo "BTT SKR Pico v1.0 を bootloader モードにします:"
    echo "  1. 電源を切ります"
    echo "  2. BOOT のジャンパ pin を挿します"
    echo "  3. USB を接続した状態で RESET を押して起動します"
    echo "  4. RPI-RP2 ボリュームが ${RPI_RP2_VOLUME} に現れることを確認します"
    echo ""
    read -rp "準備できたら Enter を押してください: " || die "中止しました"

    volume="$(wait_for_rpi_rp2_volume)" ||
        die "RPI-RP2 が見つかりません。接続とマウント状態を確認してください"
    echo "検出: ${volume}"
    cp "$firmware_file" "${volume}/"
    sync
    echo "書き込み: ${firmware_file} -> ${volume}/"
    rm -rf "$build_dir"

    echo "BOOT ジャンパ pin を外してください。firmware の起動を待ちます。"
    serial_path="$(wait_for_klipper_serial)" ||
        die "${SERIAL_BY_ID_DIR} に Klipper が現れませんでした。接続を確認してください"
    echo "確認OK: ${serial_path}"
    echo "${SERIAL_BY_ID_DIR}:"
    ls -l "$SERIAL_BY_ID_DIR"
}

main() {
    local firmware

    firmware="$(choose_firmware "${1:-}")"
    if [ -z "$firmware" ]; then
        echo "MCU firmware の書き込みをスキップしました。"
        return
    fi
    flash_btt_skr_pico "$firmware"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
