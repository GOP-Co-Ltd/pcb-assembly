#!/bin/bash
# Raspberry Pi のカメラ・スピーカーと Klipper MCU firmware を対話形式で設定する（install.sh hardware が呼ぶ）。
# 再実行すると、boot 設定の管理ブロックを置き換える。

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIRMWARE_CONFIG="${PROJECT_ROOT}/deploy/firmware/btt-skr-pico-v1-usb.config"
KLIPPER_DIR="${HOME}/klipper"
CAMERA_BLOCK_BEGIN="# BEGIN pcb-assembly camera"
CAMERA_BLOCK_END="# END pcb-assembly camera"
SPEAKER_BLOCK_BEGIN="# BEGIN pcb-assembly speaker"
SPEAKER_BLOCK_END="# END pcb-assembly speaker"
MEDIA_USER="${SUDO_USER:-${USER:-$(id -un)}}"
RPI_RP2_VOLUME="/media/${MEDIA_USER}/RPI-RP2"
SERIAL_BY_ID_DIR="/dev/serial/by-id"
VOLUME_WAIT_SECONDS=60
SERIAL_WAIT_SECONDS=30
# 選択肢に出す dtoverlay 名と説明（"overlay|説明"）。先頭が本装置の標準構成。
CAMERA_OVERLAYS=(
    "ov9281|グローバルシャッター・モノクロ"
    "imx708|Camera Module 3"
    "imx477|HQ Camera"
    "imx296|Global Shutter Camera"
    "imx219|Camera Module 2"
    "ov5647|Camera Module 1"
)
SPEAKER_OVERLAYS=(
    "max98357a|MAX98357A I2S アンプ"
    "hifiberry-dac|HiFiBerry DAC / PCM5102A 系"
    "hifiberry-dacplus|HiFiBerry DAC+ / PCM5122 系"
    "iqaudio-dacplus|Raspberry Pi DAC+ / IQaudIO DAC+"
    "googlevoicehat-soundcard|Google Voice HAT"
)

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

choose_overlay() {
    # 候補一覧と「手入力」を表示して overlay 名を返す。
    # $3 が空でなければ「未設定」も選択肢に加え、選ばれたら空文字を返す。
    local label="$1"
    local -n candidates="$2"
    local unset_label="${3:-}"
    local count="${#candidates[@]}"
    local manual=$((count + 1))
    local maximum="$manual"
    local index
    local choice

    echo "使用する${label}:" >&2
    for index in "${!candidates[@]}"; do
        echo "  $((index + 1)). ${candidates[$index]%%|*} (${candidates[$index]#*|})" >&2
    done
    echo "  ${manual}. その他（overlay 名を手入力）" >&2
    if [ -n "$unset_label" ]; then
        maximum=$((manual + 1))
        echo "  ${maximum}. ${unset_label}" >&2
    fi

    choice="$(choose_number "番号を選択してください (1-${maximum}): " 1 "$maximum")"
    if [ "$choice" -le "$count" ]; then
        printf '%s' "${candidates[$((choice - 1))]%%|*}"
    elif [ "$choice" -eq "$manual" ]; then
        read_overlay "${label}の overlay 名を入力してください: "
    fi
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
    if [ -f /boot/firmware/config.txt ]; then
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

camera_overlay_for_port() {
    # CAMERA port 1 は driver 名だけ、port 0 は ",cam0" を付けた overlay 指定になる。
    local driver="$1"
    local port="$2"

    if [ "$port" -eq 0 ]; then
        printf '%s' "${driver},cam0"
    else
        printf '%s' "$driver"
    fi
}

write_boot_config() {
    # $4 が空なら 2nd カメラの dtoverlay 行を書かない。
    local boot_config="$1"
    local camera_overlay="$2"
    local speaker_overlay="$3"
    local second_camera_overlay="${4:-}"
    local temporary_file
    local backup_file

    [ -f "$boot_config" ] || die "boot 設定が見つかりません: ${boot_config}"
    temporary_file="$(mktemp)"
    backup_file="${boot_config}.pcbasm.bak"
    strip_managed_blocks "$boot_config" "$temporary_file"
    # [all] も管理ブロックの中に置く（外に足すと再実行のたびに増える）
    {
        echo "$CAMERA_BLOCK_BEGIN"
        echo "[all]"
        echo "camera_auto_detect=0"
        echo "dtoverlay=${camera_overlay}"
        if [ -n "$second_camera_overlay" ]; then
            echo "dtoverlay=${second_camera_overlay}"
        fi
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

flash_btt_skr_pico() {
    local build_dir
    local firmware_file
    local volume
    local serial_path

    build_dir="$(mktemp -d)"
    build_klipper_firmware "$build_dir"
    firmware_file="${build_dir}/out/klipper.uf2"

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
    local boot_config
    local camera_port
    local camera_driver
    local camera_overlay
    local second_camera_port
    local second_camera_overlay=""
    local speaker_overlay
    local klipper_choice
    local answer

    boot_config="$(detect_boot_config)"

    echo "カメラを設定します。"
    echo "  0. CAMERA port 0 (CAM/DISP0)"
    echo "  1. CAMERA port 1 (CAM/DISP1)"
    camera_port="$(choose_number "使用する camera port を選択してください (0-1): " 0 1)"

    echo ""
    camera_driver="$(choose_overlay "カメラドライバ" CAMERA_OVERLAYS)"
    camera_overlay="$(camera_overlay_for_port "$camera_driver" "$camera_port")"

    echo ""
    second_camera_port=$((1 - camera_port))
    read -rp "2nd カメラ（CAMERA port ${second_camera_port}）を設定しますか? [y/N]: " answer || answer=""
    case "$answer" in
        [yY])
            echo ""
            camera_driver="$(choose_overlay "2nd カメラのドライバ" CAMERA_OVERLAYS)"
            second_camera_overlay="$(camera_overlay_for_port "$camera_driver" "$second_camera_port")"
            ;;
    esac

    echo ""
    speaker_overlay="$(choose_overlay "スピーカー" SPEAKER_OVERLAYS \
        "未設定（pcbasm 管理のスピーカー設定を削除）")"

    echo ""
    echo "boot 設定の変更内容:"
    echo "  config: ${boot_config}"
    echo "  camera: dtoverlay=${camera_overlay}"
    if [ -n "$second_camera_overlay" ]; then
        echo "  2nd camera: dtoverlay=${second_camera_overlay}"
    else
        echo "  2nd camera: 未設定"
    fi
    if [ -n "$speaker_overlay" ]; then
        echo "  speaker: dtoverlay=${speaker_overlay}"
    else
        echo "  speaker: 未設定"
    fi
    read -rp "この内容で更新しますか? [y/N]: " answer || answer=""
    case "$answer" in
        [yY]) write_boot_config "$boot_config" "$camera_overlay" "$speaker_overlay" "$second_camera_overlay" ;;
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
    echo "ハードウェア設定が完了しました。boot 設定の反映には再起動が必要です（sudo reboot）。"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
