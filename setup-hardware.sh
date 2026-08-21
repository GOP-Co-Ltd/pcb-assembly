#!/bin/bash
# Raspberry Pi のカメラ・スピーカーと Klipper MCU firmware を対話形式で設定する。

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIRMWARE_FILE="${PROJECT_ROOT}/klipper/firmwares/btt-skr-pico-v1-usb.klipper.uf2"
CAMERA_BLOCK_BEGIN="# BEGIN pcb-assembly camera"
CAMERA_BLOCK_END="# END pcb-assembly camera"
SPEAKER_BLOCK_BEGIN="# BEGIN pcb-assembly speaker"
SPEAKER_BLOCK_END="# END pcb-assembly speaker"
VOLUME_SEARCH_ROOTS="${PCBASM_VOLUME_SEARCH_ROOTS:-/media:/run/media}"
SERIAL_BY_ID_DIR="${PCBASM_SERIAL_BY_ID_DIR:-/dev/serial/by-id}"
VOLUME_WAIT_SECONDS="${PCBASM_VOLUME_WAIT_SECONDS:-60}"
SERIAL_WAIT_SECONDS="${PCBASM_SERIAL_WAIT_SECONDS:-30}"

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

read_overlay() {
    local prompt="$1"
    local overlay

    while true; do
        read -rp "$prompt" overlay || die "中止しました"
        if [[ "$overlay" =~ ^[a-zA-Z0-9._+-]+(,[a-zA-Z0-9._+=:+-]+)*$ ]]; then
            printf '%s' "$overlay"
            return
        fi
        echo "overlay 名を入力してください（空白や dtoverlay= は含めません）。" >&2
    done
}

detect_boot_config() {
    if [ -n "${PCBASM_BOOT_CONFIG:-}" ]; then
        printf '%s' "$PCBASM_BOOT_CONFIG"
    elif [ -f /boot/firmware/config.txt ]; then
        printf '%s' /boot/firmware/config.txt
    elif [ -f /boot/config.txt ]; then
        printf '%s' /boot/config.txt
    else
        die "/boot/firmware/config.txt または /boot/config.txt が見つかりません"
    fi
}

strip_managed_blocks() {
    local source_file="$1"
    local destination_file="$2"

    awk \
        -v camera_begin="$CAMERA_BLOCK_BEGIN" \
        -v camera_end="$CAMERA_BLOCK_END" \
        -v speaker_begin="$SPEAKER_BLOCK_BEGIN" \
        -v speaker_end="$SPEAKER_BLOCK_END" \
        '
        $0 == camera_begin || $0 == speaker_begin { managed = 1; next }
        $0 == camera_end || $0 == speaker_end { managed = 0; next }
        !managed && $0 ~ /^[[:space:]]*camera_auto_detect=/ { next }
        !managed { print }
        ' "$source_file" >"$destination_file"
}

copy_file() {
    local source_file="$1"
    local destination_file="$2"

    if [ -w "$destination_file" ]; then
        cp "$source_file" "$destination_file"
    else
        sudo cp "$source_file" "$destination_file"
    fi
}

backup_file() {
    local source_file="$1"
    local backup_file="$2"

    if [ -w "$source_file" ]; then
        cp "$source_file" "$backup_file"
    else
        sudo cp "$source_file" "$backup_file"
    fi
}

write_boot_config() {
    local boot_config="$1"
    local camera_overlay="$2"
    local speaker_overlay="$3"
    local temporary_file
    local backup_file

    [ -f "$boot_config" ] || die "boot 設定が見つかりません: ${boot_config}"
    temporary_file="$(mktemp)"
    backup_file="${boot_config}.pcbasm.bak"
    strip_managed_blocks "$boot_config" "$temporary_file"
    {
        echo ""
        echo "[all]"
        echo "$CAMERA_BLOCK_BEGIN"
        echo "camera_auto_detect=0"
        echo "dtoverlay=${camera_overlay}"
        echo "$CAMERA_BLOCK_END"
        if [ -n "$speaker_overlay" ]; then
            echo "$SPEAKER_BLOCK_BEGIN"
            echo "dtoverlay=${speaker_overlay}"
            echo "$SPEAKER_BLOCK_END"
        fi
    } >>"$temporary_file"

    backup_file "$boot_config" "$backup_file"
    copy_file "$temporary_file" "$boot_config"
    rm -f "$temporary_file"
    echo "更新: ${boot_config}"
    echo "バックアップ: ${backup_file}"
}

find_rpi_rp2_volume() {
    local search_root
    local volume
    local roots=()

    IFS=: read -r -a roots <<<"$VOLUME_SEARCH_ROOTS"
    for search_root in "${roots[@]}"; do
        [ -d "$search_root" ] || continue
        volume="$(find "$search_root" -maxdepth 3 -type d -name RPI-RP2 -print -quit 2>/dev/null)"
        if [ -n "$volume" ]; then
            printf '%s' "$volume"
            return 0
        fi
    done
    return 1
}

wait_for_rpi_rp2_volume() {
    local elapsed=0
    local volume

    echo "RPI-RP2 ボリュームを待っています（最大 ${VOLUME_WAIT_SECONDS} 秒）..." >&2
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

flash_btt_skr_pico() {
    local volume
    local serial_path

    [ -f "$FIRMWARE_FILE" ] || die "firmware が見つかりません: ${FIRMWARE_FILE}"

    echo ""
    echo "BTT SKR Pico v1.0 を bootloader モードにします:"
    echo "  1. 電源を切ります"
    echo "  2. BOOT のジャンパ pin を挿します"
    echo "  3. USB を接続した状態で RESET を押して起動します"
    echo "  4. RPI-RP2 ボリュームが /media 以下に現れることを確認します"
    echo ""
    read -rp "準備できたら Enter を押してください: " || die "中止しました"

    volume="$(wait_for_rpi_rp2_volume)" ||
        die "RPI-RP2 が見つかりません。接続とマウント状態を確認してください"
    echo "検出: ${volume}"
    cp "$FIRMWARE_FILE" "${volume}/"
    sync
    echo "書き込み: ${FIRMWARE_FILE} -> ${volume}/"

    echo "BOOT ジャンパ pin を外してください。firmware の起動を待ちます。"
    serial_path="$(wait_for_klipper_serial)" ||
        die "${SERIAL_BY_ID_DIR} に Klipper が現れませんでした。接続を確認してください"
    echo "確認OK: ${serial_path}"
    echo "${SERIAL_BY_ID_DIR}:"
    ls -l "$SERIAL_BY_ID_DIR"
}

main() {
    local boot_config
    local camera_port
    local camera_driver_choice
    local camera_driver
    local camera_overlay
    local speaker_choice
    local speaker_overlay=""
    local klipper_choice
    local answer

    boot_config="$(detect_boot_config)"

    echo "カメラを設定します。"
    echo "  0. CAMERA port 0 (CAM/DISP0)"
    echo "  1. CAMERA port 1 (CAM/DISP1)"
    camera_port="$(choose_number "使用する camera port を選択してください (0-1): " 0 1)"

    echo ""
    echo "使用するカメラドライバ:"
    echo "  1. ov9281"
    echo "  2. 手入力"
    camera_driver_choice="$(choose_number "番号を選択してください (1-2): " 1 2)"
    if [ "$camera_driver_choice" -eq 1 ]; then
        camera_driver=ov9281
    else
        camera_driver="$(read_overlay "カメラの overlay 名を入力してください: ")"
    fi
    camera_overlay="$camera_driver"
    if [ "$camera_port" -eq 0 ]; then
        camera_overlay="${camera_overlay},cam0"
    fi

    echo ""
    echo "使用するスピーカー:"
    echo "  1. MAX98357A"
    echo "  2. その他（overlay 名を手入力）"
    echo "  3. 未設定（pcbasm 管理のスピーカー設定を削除）"
    speaker_choice="$(choose_number "番号を選択してください (1-3): " 1 3)"
    case "$speaker_choice" in
        1) speaker_overlay=max98357a ;;
        2) speaker_overlay="$(read_overlay "スピーカーの overlay 名を入力してください: ")" ;;
        3) speaker_overlay="" ;;
    esac

    echo ""
    echo "boot 設定の変更内容:"
    echo "  config: ${boot_config}"
    echo "  camera: dtoverlay=${camera_overlay}"
    if [ -n "$speaker_overlay" ]; then
        echo "  speaker: dtoverlay=${speaker_overlay}"
    else
        echo "  speaker: 未設定"
    fi
    read -rp "この内容で更新しますか? [y/N]: " answer || answer=""
    case "$answer" in
        [yY]) write_boot_config "$boot_config" "$camera_overlay" "$speaker_overlay" ;;
        *) die "boot 設定の更新を中止しました" ;;
    esac

    echo ""
    echo "Klipper firmware を設定します:"
    echo "  1. BTT SKR Pico v1.0"
    echo "  2. 今回はスキップ"
    klipper_choice="$(choose_number "デバイスを選択してください (1-2): " 1 2)"
    if [ "$klipper_choice" -eq 1 ]; then
        flash_btt_skr_pico
    else
        echo "Klipper firmware の書き込みをスキップしました。"
    fi

    echo ""
    echo "ハードウェア設定が完了しました。boot 設定の反映には再起動が必要です。"
    read -rp "今すぐ Raspberry Pi を再起動しますか? [y/N]: " answer || answer=""
    case "$answer" in
        [yY]) sudo reboot ;;
        *) echo "後で次のコマンドを実行してください: sudo reboot" ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
