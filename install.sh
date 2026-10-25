#!/bin/bash
# ホスト導入の唯一の入口。clone から WebUI が開くまでを段に分け、どの段も冪等で単独でも再実行できる。
# sudo を付けずに実行する（必要な操作は段の中で sudo する）。

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="${PROJECT_ROOT}/deploy"
UV_VERSION="0.12.5"
MACHINE_STAGES=(preflight packages python klipper hardware machine services)
UI_ONLY_STAGES=(preflight packages python services)
BASE_PACKAGES=(curl make git git-lfs)
MACHINE_PACKAGES=(alsa-utils v4l-utils kicad python3-picamera2)
# 以下はテスト用の継ぎ目。既定値は実機のもの
SUDO="${SUDO:-sudo}"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
SUDOERS_DIR="${SUDOERS_DIR:-/etc/sudoers.d}"
VISUDO="${VISUDO:-/usr/sbin/visudo}"
SERIAL_BY_ID_DIR="${SERIAL_BY_ID_DIR:-/dev/serial/by-id}"
UI_ONLY=false

# パスに & を含めても置換で化けないようにする（bash 5.2 の既定は on）
shopt -u patsub_replacement 2>/dev/null || true

usage() {
    cat <<EOF
Usage: $(basename "$0") [--ui-only] [<段> [引数]]

段を省くと全段を順に実行する（機体: ${MACHINE_STAGES[*]}）。
--ui-only は frontend 専用ホスト向け（${UI_ONLY_STAGES[*]}。services は ui だけ）。

段
  preflight          Debian 13 (trixie) aarch64、ホスト名、clone の位置を確かめる
  packages           apt のパッケージと Git LFS の実体
  python             版を固定した uv と venv（無ければ make setup、あれば uv sync で同期だけ）
  klipper            KIAUH で Klipper を入れ（導入済みなら飛ばす）、独自 kinematics を symlink する
  hardware           boot overlay（カメラ・スピーカー）と MCU firmware（deploy/hardware.sh）
  machine            deploy/templates/ から config/machine.toml と printer.cfg を置く
  services [対象]    systemd unit（api / ui / all。既定 all）と更新用 sudoers を設置して起動する
  render <対象>      api / ui の unit、または sudoers を標準出力に描く（systemd に触らない）
EOF
}

die() {
    echo "エラー: $1" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "必要なコマンドが見つかりません: $1"
}

require_non_root() {
    [ "${EUID}" -ne 0 ] || die "sudo を付けずに実行してください。必要な操作は段の中で sudo を使います。"
}

# fill <テンプレート> <名前=値>...: テンプレートの @名前@ を値に置き換えて標準出力に書く
fill() {
    local text pair
    text="$(<"$1")"
    shift
    for pair in "$@"; do
        text="${text//@${pair%%=*}@/${pair#*=}}"
    done
    printf '%s\n' "${text}"
}

# --- preflight ---

stage_preflight() {
    local codename arch host
    require_non_root
    codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-}")"
    [ "${codename}" = "trixie" ] || die "Debian 13 (trixie) が前提です（このホスト: ${codename:-不明}）"
    arch="$(uname -m)"
    [ "${UI_ONLY}" = true ] || [ "${arch}" = "aarch64" ] ||
        die "機体は aarch64 の Raspberry Pi が前提です（このホスト: ${arch}）"
    # ホスト名は mDNS の機体 ID になる。既定名のままの機体が 2 台あると区別できない
    host="$(hostname)"
    case "${host}" in
        raspberrypi | localhost)
            die "ホスト名が既定の ${host} のままです。sudo hostnamectl set-hostname <一意な名前> で変えてください"
            ;;
    esac
    [ "${PROJECT_ROOT}" = "${HOME}/pcb-assembly" ] ||
        die "clone は ${HOME}/pcb-assembly に置いてください（現在: ${PROJECT_ROOT}）"
    echo "preflight: ${codename} ${arch} ${host} ${PROJECT_ROOT}"
}

# --- packages ---

stage_packages() {
    local -a packages=("${BASE_PACKAGES[@]}")
    [ "${UI_ONLY}" = true ] || packages+=("${MACHINE_PACKAGES[@]}")
    ${SUDO} apt-get update
    ${SUDO} apt-get install -y "${packages[@]}"
    # LFS 無しで clone したときにポインタのまま残ったファイルを実体に置き換える
    git lfs install
    git -C "${PROJECT_ROOT}" lfs pull
}

# --- python ---

stage_python() {
    if [ "$(uv --version 2>/dev/null | cut -d' ' -f2)" != "${UV_VERSION}" ]; then
        curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
    fi
    export PATH="${HOME}/.local/bin:${PATH}"
    # 既にある venv は作り直さず同期だけする。稼働中のサービスが使っており、
    # 自己更新が --inexact で残した機体ごとの group も消えるため（引数は自己更新と同じ）
    if [ -d "${PROJECT_ROOT}/.venv" ]; then
        (cd "${PROJECT_ROOT}" && uv sync --locked --inexact)
    else
        make -C "${PROJECT_ROOT}" setup
    fi
}

# --- klipper ---

stage_klipper() {
    local kinematics_dir="${HOME}/klipper/klippy/kinematics"
    if [ ! -d "${HOME}/klipper" ]; then
        [ -d "${HOME}/kiauh" ] || git clone https://github.com/dw-0/kiauh.git "${HOME}/kiauh"
        echo "KIAUH で Klipper、Moonraker、Mainsail を入れてください。"
        "${HOME}/kiauh/kiauh.sh"
    fi
    [ -d "${kinematics_dir}" ] || die "Klipper が見つかりません: ${kinematics_dir}（KIAUH で Klipper を入れてください）"
    ln -sfn "${PROJECT_ROOT}/klipper/inversed_corexy.py" "${kinematics_dir}/inversed_corexy.py"
    echo "kinematics: ${kinematics_dir}/inversed_corexy.py -> ${PROJECT_ROOT}/klipper/inversed_corexy.py"
    # ロードセルプローブ（[load_cell_probe]）の依存。scipy は drift / notch フィルタ用
    "${HOME}/klippy-env/bin/pip" install numpy scipy
}

# --- hardware ---

stage_hardware() {
    "${DEPLOY_DIR}/hardware.sh"
}

# --- machine ---

# KIAUH が置く printer.cfg（kinematics: none）か、まだ無いか
is_klipper_stub() {
    [ ! -e "$1" ] || grep -Eq '^[[:space:]]*kinematics[[:space:]]*:[[:space:]]*none' "$1"
}

choose_from() {
    local prompt="$1" choice
    shift
    if [ "$#" -eq 1 ]; then
        printf '%s' "$1"
        return
    fi
    local i=1 item
    for item in "$@"; do
        echo "  ${i}. ${item}" >&2
        i=$((i + 1))
    done
    while true; do
        read -rp "${prompt} (1-$#): " choice || die "中止しました"
        if [[ "${choice}" =~ ^[0-9]+$ ]] && [ "${choice}" -ge 1 ] && [ "${choice}" -le "$#" ]; then
            printf '%s' "${!choice}"
            return
        fi
    done
}

# printer.cfg の無名 [mcu] の serial 行だけを実デバイスに書き換える（[mcu <名前>] は触らない）
write_mcu_serial() {
    local file="$1" serial tmp
    local -a serials=()
    [ -d "${SERIAL_BY_ID_DIR}" ] && mapfile -t serials < <(find "${SERIAL_BY_ID_DIR}" -mindepth 1 -maxdepth 1 | sort)
    if [ "${#serials[@]}" -eq 0 ]; then
        echo "警告: ${SERIAL_BY_ID_DIR} に MCU がありません。${file} の [mcu] serial を手で設定してください" >&2
        return 0
    fi
    echo "MCU:" >&2
    serial="$(choose_from "MCU を選んでください" "${serials[@]}")"
    tmp="$(mktemp)"
    awk -v serial="${serial}" '
        /^\[/ { in_mcu = ($0 ~ /^\[mcu\]/) }
        in_mcu && /^[[:space:]]*serial[[:space:]]*:/ { print "serial: " serial; next }
        { print }
    ' "${file}" >"${tmp}"
    cat "${tmp}" >"${file}"
    rm -f "${tmp}"
    echo "設定: [mcu] serial = ${serial}"
}

# 既にある config/machine.toml と、SAVE_CONFIG の較正値を持つ printer.cfg には触らない。
# テンプレートから作り直すときは、その実ファイルを退避してから再実行する
stage_machine() {
    local config_dir="${PCBASM_CONFIG_DIR:-${PROJECT_ROOT}/config}"
    local klipper_cfg="${HOME}/printer_data/config/printer.cfg"
    local template="" name
    local -a templates=()
    [ -d "$(dirname "${klipper_cfg}")" ] || die "$(dirname "${klipper_cfg}") がありません（先に klipper 段を実行してください）"
    [ ! -e "${config_dir}/printer.cfg" ] || [ -L "${config_dir}/printer.cfg" ] ||
        die "${config_dir}/printer.cfg が実ファイルです。退避してから再実行してください"

    if [ ! -e "${config_dir}/machine.toml" ] || is_klipper_stub "${klipper_cfg}"; then
        for name in "${DEPLOY_DIR}"/templates/*/; do
            [ -f "${name}machine.toml" ] && [ -f "${name}printer.cfg" ] && templates+=("$(basename "${name}")")
        done
        echo "機種:" >&2
        template="${DEPLOY_DIR}/templates/$(choose_from "機種を選んでください" "${templates[@]}")"
    fi

    mkdir -p "${config_dir}"
    if [ ! -e "${config_dir}/machine.toml" ]; then
        cp "${template}/machine.toml" "${config_dir}/machine.toml"
        echo "配置: ${config_dir}/machine.toml"
    fi
    if is_klipper_stub "${klipper_cfg}"; then
        [ ! -e "${klipper_cfg}" ] || cp -p "${klipper_cfg}" "${klipper_cfg}.bak.$(date +%Y%m%d_%H%M%S)"
        cp "${template}/printer.cfg" "${klipper_cfg}"
        echo "配置: ${klipper_cfg}（反映: sudo systemctl restart klipper）"
        write_mcu_serial "${klipper_cfg}"
    fi
    # リポジトリから printer.cfg を見るための symlink（Klipper と SAVE_CONFIG は実ファイルを読み書きする）
    ln -sfn "${klipper_cfg}" "${config_dir}/printer.cfg"
}

# --- services / render ---

resolve_targets() {
    case "$1" in
        api | ui) echo "$1" ;;
        all) echo "api ui" ;;
        *) die "不正な対象: $1（api|ui|all のいずれか）" ;;
    esac
}

render_unit() {
    local target="$1" description uv_bin
    case "${target}" in
        api) description="PCB Assembly backend WebAPI" ;;
        ui) description="PCB Assembly UI frontend" ;;
        *) die "不正な対象: ${target}（api|ui のいずれか）" ;;
    esac
    uv_bin="$(command -v uv)" || die "必要なコマンドが見つかりません: uv"
    # 起動順の依存（After=pcbasm-api.service）は付けない。同居機で backend の起動失敗が frontend まで止める
    fill "${DEPLOY_DIR}/systemd/pcbasm.service.in" \
        "DESCRIPTION=${description}" \
        "USER=$(id -un)" \
        "GROUP=$(id -gn)" \
        "PROJECT_ROOT=${PROJECT_ROOT}" \
        "HOME=${HOME}" \
        "PATH=$(dirname "${uv_bin}"):/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
        "MAKE=$(command -v make)" \
        "TARGET=${target}"
}

render_sudoers() {
    fill "${DEPLOY_DIR}/sudoers" "USER=$(id -un)"
}

# unit が変わったときだけ再起動する（同じなら起動を確かめるだけ）
install_unit() {
    local target="$1" name="pcbasm-$1.service" tmp
    tmp="$(mktemp)"
    render_unit "${target}" >"${tmp}"
    if cmp -s "${tmp}" "${SYSTEMD_UNIT_DIR}/${name}"; then
        rm -f "${tmp}"
        ${SUDO} systemctl enable --now "${name}"
        echo "${name} は最新です。"
        return
    fi
    ${SUDO} install -m 0644 "${tmp}" "${SYSTEMD_UNIT_DIR}/${name}" || {
        rm -f "${tmp}"
        return 1
    }
    rm -f "${tmp}"
    ${SUDO} systemctl daemon-reload
    ${SUDO} systemctl enable "${name}"
    ${SUDO} systemctl restart "${name}"
    echo "${name} を設置して起動しました。"
}

# 許すのは systemctl restart --no-block の固定 argv 3 通りだけ。壊れた sudoers は sudo 全体を止めるので、
# visudo の検査を通るまで設置先に触らない
install_sudoers() {
    local path="${SUDOERS_DIR}/pcbasm-update" tmp units
    tmp="$(mktemp)"
    render_sudoers >"${tmp}"
    if ! "${VISUDO}" -cf "${tmp}" >/dev/null; then
        rm -f "${tmp}"
        die "sudoers が構文検査に通りません。設置しません"
    fi
    # 設置先は root 所有の 0440 なので、一般ユーザーの cmp は読めずに毎回置き直す（害は無い）
    if cmp -s "${tmp}" "${path}"; then
        echo "${path} は最新です。"
    else
        # sudo はドットを含むファイル名を読み飛ばすので拡張子を付けない
        ${SUDO} install -m 0440 "${tmp}" "${path}" || {
            rm -f "${tmp}"
            return 1
        }
        echo "${path} を設置しました。"
    fi
    rm -f "${tmp}"
    # sudo -n -l <argv> はその argv を実行できるかを終了コードで返す（一覧の文字列照合は 80 桁折り返しで外れる）
    for units in pcbasm-api.service pcbasm-ui.service "pcbasm-api.service pcbasm-ui.service"; do
        # shellcheck disable=SC2086
        ${SUDO} -n -l /usr/bin/systemctl restart --no-block ${units} >/dev/null 2>&1 ||
            echo "警告: '/usr/bin/systemctl restart --no-block ${units}' が許可されていません。" >&2
    done
}

stage_services() {
    local default=all targets target
    [ "${UI_ONLY}" = false ] || default=ui
    targets="$(resolve_targets "${1:-${default}}")"
    require_non_root
    require_command systemctl
    require_command make
    require_command uv
    for target in ${targets}; do
        install_unit "${target}"
    done
    install_sudoers
}

stage_render() {
    case "${1:-}" in
        api | ui) render_unit "$1" ;;
        sudoers) render_sudoers ;;
        *) die "render の対象は api / ui / sudoers のいずれかです" ;;
    esac
}

main() {
    local stage
    if [ "${1:-}" = "--ui-only" ]; then
        UI_ONLY=true
        shift
    fi
    case "${1:-}" in
        "")
            if [ "${UI_ONLY}" = true ]; then
                for stage in "${UI_ONLY_STAGES[@]}"; do "stage_${stage}"; done
            else
                for stage in "${MACHINE_STAGES[@]}"; do "stage_${stage}"; done
            fi
            echo "導入が完了しました。boot 設定を変えたときは sudo reboot で反映します。"
            ;;
        preflight | packages | python | klipper | hardware | machine | services | render)
            stage="$1"
            shift
            "stage_${stage}" "$@"
            ;;
        -h | --help) usage ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
}

main "$@"
