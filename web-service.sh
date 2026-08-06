#!/bin/bash

set -euo pipefail

# backend WebAPI（機体ごと）と UI frontend（1 台）を別 unit として扱う。
# unit 名 / make ターゲット / 対象名は同じ語（api, ui）で揃えてある。
LEGACY_SERVICE_NAME="pcbasm-webui.service"
DEFAULT_TARGET="api"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 以下 2 つはテスト用の seam。既定値は実機の systemd と sudo。
# テストは SYSTEMD_UNIT_DIR を一時ディレクトリにし、SUDO には `exec "$@"` するだけの
# no-op ラッパを渡して特権コマンド（install / rm）を実際に走らせ、一時ディレクトリ上の
# 実結果を観測する。systemctl は PATH 先頭のスタブが受けるので実機の systemd には触らない。
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
SUDO="${SUDO:-sudo}"
PCBASM_STATE_DIR="${PCBASM_STATE_DIR:-/var/lib/pcbasm/state}"
PCBASM_RELEASES_DIR="${PCBASM_RELEASES_DIR:-/var/lib/pcbasm/releases}"
PCBASM_RUNTIME_ROOT="$(dirname "${PCBASM_RELEASES_DIR}")"
if [ "${SYSTEMD_UNIT_DIR}" = "/etc/systemd/system" ]; then
    SUDOERS_DIR="${SUDOERS_DIR:-/etc/sudoers.d}"
else
    # systemd test seam では sudoers も同じ一時 directory に隔離する。
    SUDOERS_DIR="${SUDOERS_DIR:-${SYSTEMD_UNIT_DIR}}"
fi

usage() {
    cat <<EOF
Usage: $(basename "$0") <install|start|stop|restart|status|remove> [api|ui|all]

対象（既定: ${DEFAULT_TARGET}）
  api  backend WebAPI: pcbasm-api.service (current-api release)
  ui   UI frontend:    pcbasm-ui.service (current-ui release)
  all  api → ui の順に両方

操作
  install  unit を登録し、起動時に自動起動する
  start    サービスを起動する
  stop     サービスを一時停止する（次回のシステム起動時には自動起動する）
  restart  最新のソースでサービスを再起動する
  status   サービスの状態を表示する（all では全対象を表示する）
  remove   サービスを停止・無効化し、登録を削除する

旧 ${LEGACY_SERVICE_NAME}（= backend）の削除は install / remove の対象が
api または all のときだけ行う。ui 単体では削除せず、install ui は残っていれば
警告するだけ。
EOF
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "エラー: 必要なコマンドが見つかりません: $1" >&2
        exit 1
    fi
}

require_non_root() {
    if [ "${EUID}" -eq 0 ]; then
        echo "エラー: sudo を付けずに実行してください。必要な操作はスクリプト内で sudo を使用します。" >&2
        exit 1
    fi
}

resolve_targets() {
    case "${1:-}" in
        "")
            echo "${DEFAULT_TARGET}"
            ;;
        api | ui)
            echo "$1"
            ;;
        all)
            echo "api ui"
            ;;
        *)
            echo "エラー: 不正な対象: $1（api|ui|all のいずれか）" >&2
            return 2
            ;;
    esac
}

service_name() {
    printf 'pcbasm-%s.service\n' "$1"
}

unit_path() {
    printf '%s/pcbasm-%s.service\n' "${SYSTEMD_UNIT_DIR}" "$1"
}

service_description() {
    case "$1" in
        api)
            echo "PCB Assembly backend WebAPI"
            ;;
        ui)
            echo "PCB Assembly UI frontend"
            ;;
    esac
}

# unit テキストを stdout に組み立てるだけの関数（systemd には触らない）。
# 対象ごとに違うのは Description と ExecStart のターゲット名だけ。
#
# frontend に `After=pcbasm-api.service` を付けない: 同居機で backend の起動失敗が
# frontend まで止めてしまう。frontend は backend が落ちていても起動でき、
# 各機体への問い合わせが 503 になるだけで復帰できる。
# `After=avahi-daemon.service` も不要（mDNS は python-zeroconf 実装で avahi に依存しない）。
render_unit() {
    local target="$1" service_user service_group current executable_path
    service_user="$(id -un)"
    service_group="$(id -gn)"
    current="${PCBASM_RUNTIME_ROOT}/current-${target}"
    executable_path="${current}/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

    cat <<EOF
[Unit]
Description=$(service_description "${target}")
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=${service_user}
Group=${service_group}
WorkingDirectory=${current}
Environment="HOME=${HOME}"
Environment="PATH=${executable_path}"
Environment="PCBASM_MANAGEMENT_REPO=${PROJECT_ROOT}"
Environment="PCBASM_STATE_DIR=${PCBASM_STATE_DIR}"
Environment="PCBASM_RELEASES_DIR=${PCBASM_RELEASES_DIR}"
Environment="PCBASM_INSTALLED_SCHEMA_VERSION=$(installed_schema_version "${target}")"
Environment="PCBASM_CONFIG_DIR=${PROJECT_ROOT}/config"
Environment="PCBASM_API_DATA_DIR=${PROJECT_ROOT}/data"
Environment="PCBASM_API_UPLOAD_DIR=${PROJECT_ROOT}/uploads"
Environment="PCBASM_API_PCB_ALLOWED_ROOT=${PROJECT_ROOT}"
Environment="PCBASM_API_PCB_BROWSE_START=${PROJECT_ROOT}"
Environment="PCBASM_UI_MACHINES_FILE=${PROJECT_ROOT}/config/machines.toml"
ExecStart=${current}/.venv/bin/python -m web.${target}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

update_executable_path() {
    local uv_bin
    uv_bin="$(command -v uv)"
    printf '%s:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n' "$(dirname "${uv_bin}")"
}

installed_schema_version() {
    local target="$1" current_schema
    current_schema="${PCBASM_RUNTIME_ROOT}/current-${target}/deploy/schema-version"
    if [ ! -f "${current_schema}" ]; then
        current_schema="${PROJECT_ROOT}/deploy/schema-version"
    fi
    tr -d '[:space:]' <"${current_schema}"
}

render_update_apply_unit() {
    local target="$1" service_user service_group current executable_path
    service_user="$(id -un)"
    service_group="$(id -gn)"
    current="${PCBASM_RUNTIME_ROOT}/current-${target}"
    executable_path="$(update_executable_path)"
    cat <<EOF
[Unit]
Description=Apply PCB Assembly software update (${target})
After=network-online.target

[Service]
Type=oneshot
User=${service_user}
Group=${service_group}
WorkingDirectory=${PROJECT_ROOT}
Environment="HOME=${HOME}"
Environment="PATH=${executable_path}"
Environment="PCBASM_MANAGEMENT_REPO=${PROJECT_ROOT}"
Environment="PCBASM_STATE_DIR=${PCBASM_STATE_DIR}"
Environment="PCBASM_RELEASES_DIR=${PCBASM_RELEASES_DIR}"
Environment="PCBASM_INSTALLED_SCHEMA_VERSION=$(installed_schema_version "${target}")"
ExecStart=${current}/.venv/bin/python -m pcbasm.software_update apply --role ${target}
EOF
}

render_update_check_unit() {
    local target="$1" service_user service_group current executable_path
    service_user="$(id -un)"
    service_group="$(id -gn)"
    current="${PCBASM_RUNTIME_ROOT}/current-${target}"
    executable_path="$(update_executable_path)"
    cat <<EOF
[Unit]
Description=Check PCB Assembly software update (${target})
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=${service_user}
Group=${service_group}
WorkingDirectory=${PROJECT_ROOT}
Environment="HOME=${HOME}"
Environment="PATH=${executable_path}"
Environment="PCBASM_MANAGEMENT_REPO=${PROJECT_ROOT}"
Environment="PCBASM_STATE_DIR=${PCBASM_STATE_DIR}"
Environment="PCBASM_RELEASES_DIR=${PCBASM_RELEASES_DIR}"
Environment="PCBASM_INSTALLED_SCHEMA_VERSION=$(installed_schema_version "${target}")"
ExecStart=${current}/.venv/bin/python -m pcbasm.software_update check --role ${target}
EOF
}

render_update_timer() {
    local target="$1"
    cat <<EOF
[Unit]
Description=Periodic PCB Assembly software update check (${target})

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true
Unit=pcbasm-update-check@${target}.service

[Install]
WantedBy=timers.target
EOF
}

render_role_sudoers() {
    local target="$1" systemctl_bin
    systemctl_bin="$(command -v systemctl)"
    cat <<EOF
$(id -un) ALL=(root) NOPASSWD: ${systemctl_bin} start pcbasm-update@${target}.service --no-block
$(id -un) ALL=(root) NOPASSWD: ${systemctl_bin} start pcbasm-update-check@${target}.service --no-block
$(id -un) ALL=(root) NOPASSWD: ${systemctl_bin} restart pcbasm-${target}.service
EOF
}

render_update_sudoers() {
    render_role_sudoers api
    render_role_sudoers ui
}

replace_with_persistent_link() {
    local source="$1" destination="$2"
    mkdir -p "${source}" "$(dirname "${destination}")"
    if [ -L "${destination}" ]; then
        if [ "$(readlink -f "${destination}")" = "$(readlink -f "${source}")" ]; then
            return 0
        fi
        rm -f "${destination}"
    elif [ -d "${destination}" ]; then
        # release worktree 内の tracked snapshot だけが対象。
        rm -r "${destination}"
    elif [ -e "${destination}" ]; then
        rm -f "${destination}"
    fi
    ln -s "${source}" "${destination}"
}

prepare_initial_release() {
    local target="$1" branch revision release current temporary module
    # unit/sudoers test seam から host の release 領域へ書き込まない。
    if [ "${SYSTEMD_UNIT_DIR}" != "/etc/systemd/system" ]; then
        return 0
    fi

    branch="$(git -C "${PROJECT_ROOT}" branch --show-current)"
    if [ -z "${branch}" ]; then
        echo "エラー: 管理 checkout が detached HEAD です。" >&2
        return 1
    fi
    git -C "${PROJECT_ROOT}" fetch origin "${branch}"
    revision="$(git -C "${PROJECT_ROOT}" rev-parse "origin/${branch}")"
    release="${PCBASM_RELEASES_DIR}/${revision}"
    current="${PCBASM_RUNTIME_ROOT}/current-${target}"
    module="web.api.app"
    if [ "${target}" = "ui" ]; then
        module="web.ui.app"
    fi

    if [ ! -f "${release}/.pcbasm-prepared" ]; then
        mkdir -p "${PCBASM_RELEASES_DIR}"
        if [ -e "${release}" ]; then
            echo "エラー: 未完成 release が残っています: ${release}" >&2
            return 1
        fi
        git -C "${PROJECT_ROOT}" worktree add --detach "${release}" "${revision}"
        git -C "${release}" lfs pull
        uv venv --system-site-packages "${release}/.venv"
        uv sync --directory "${release}" --locked --all-extras
    fi

    # release 内の従来パスも管理 checkout の永続データへ接続する。
    replace_with_persistent_link "${PROJECT_ROOT}/config" "${release}/config"
    replace_with_persistent_link "${PROJECT_ROOT}/uploads" "${release}/uploads"
    replace_with_persistent_link "${PROJECT_ROOT}/data/webui" "${release}/data/webui"
    "${release}/.venv/bin/python" -c "import ${module}"
    printf '%s\n' "${revision}" >"${release}/.pcbasm-prepared"

    temporary="${PCBASM_RUNTIME_ROOT}/.current-${target}.$$"
    ln -s "${release}" "${temporary}"
    mv -Tf "${temporary}" "${current}"
    mkdir -p "${PCBASM_STATE_DIR}"
}

install_rendered_file() {
    local mode="$1" destination="$2" renderer="$3" target="$4" temporary
    temporary="$(mktemp)"
    trap "rm -f '${temporary}'" EXIT
    "${renderer}" "${target}" >"${temporary}"
    ${SUDO} install -m "${mode}" "${temporary}" "${destination}"
    rm -f "${temporary}"
    trap - EXIT
}

# 旧 unit は `ExecStart=make webui` を参照している。Makefile から webui エイリアスを
# 消したため、残しておくと restart ループに入る。
#
# 掃除するのは**置き換え先（backend = api）を入れる/外すときだけ**。旧 unit は backend
# そのものなので、`install ui` で消すと稼働中の同居機から backend が消えて frontend だけが
# 残る（`install api` を打つまで backend 不在）。
purge_legacy_unit() {
    local legacy_path="${SYSTEMD_UNIT_DIR}/${LEGACY_SERVICE_NAME}"

    if [ ! -e "${legacy_path}" ]; then
        return 0
    fi

    echo "旧 ${LEGACY_SERVICE_NAME} を検出しました。無効化して削除します。"
    ${SUDO} systemctl disable --now "${LEGACY_SERVICE_NAME}" || true
    ${SUDO} rm -f "${legacy_path}"
    ${SUDO} systemctl daemon-reload
    ${SUDO} systemctl reset-failed "${LEGACY_SERVICE_NAME}" 2>/dev/null || true
}

# api 以外を対象にしたときは旧 unit を触らず、残っていることだけを知らせる。
warn_legacy_unit() {
    if [ -e "${SYSTEMD_UNIT_DIR}/${LEGACY_SERVICE_NAME}" ]; then
        echo "警告: 旧 ${LEGACY_SERVICE_NAME} が残っています。'$0 install api' を実行してください。" >&2
    fi
}

require_privileged_tools() {
    require_non_root
    require_command sudo
    require_command systemctl
}

prepare_runtime_directories() {
    local service_user service_group
    # test seamからhostの /var/lib を変更しない。専用のinstallerテストでは
    # PCBASM_*を一時pathへ向けてproduction相当の準備を個別に検証する。
    if [ "${SYSTEMD_UNIT_DIR}" != "/etc/systemd/system" ]; then
        return 0
    fi
    service_user="$(id -un)"
    service_group="$(id -gn)"
    ${SUDO} install -d -m 0755 -o "${service_user}" -g "${service_group}" \
        "${PCBASM_RUNTIME_ROOT}" "${PCBASM_STATE_DIR}" "${PCBASM_RELEASES_DIR}"
}

install_service() {
    local target="$1" name unit_file
    name="$(service_name "${target}")"

    require_command uv

    # candidate release の venv/import が成功してから既存 unit を置き換える。
    prepare_runtime_directories
    prepare_initial_release "${target}"

    if [ "${target}" = "api" ]; then
        purge_legacy_unit
    else
        warn_legacy_unit
    fi

    unit_file="$(mktemp)"
    # trap 本文は関数フレームが巻き戻された後に評価されるため、local の ${unit_file} を
    # 遅延展開すると空文字（= rm -f ""）になり、特権 install が失敗したときに mktemp した
    # ファイルが残る。設置時に展開して実パスを焼き込む。
    # shellcheck disable=SC2064
    trap "rm -f '${unit_file}'" EXIT
    render_unit "${target}" >"${unit_file}"

    ${SUDO} install -m 0644 "${unit_file}" "$(unit_path "${target}")"
    rm -f "${unit_file}"
    trap - EXIT
    install_rendered_file 0644 "${SYSTEMD_UNIT_DIR}/pcbasm-update@${target}.service" render_update_apply_unit "${target}"
    install_rendered_file 0644 "${SYSTEMD_UNIT_DIR}/pcbasm-update-check@${target}.service" render_update_check_unit "${target}"
    install_rendered_file 0644 "${SYSTEMD_UNIT_DIR}/pcbasm-update-check@${target}.timer" render_update_timer "${target}"
    install_rendered_file 0440 "${SUDOERS_DIR}/pcbasm-update-${target}" render_role_sudoers "${target}"
    ${SUDO} systemctl daemon-reload
    ${SUDO} systemctl enable "${name}"
    ${SUDO} systemctl enable --now "pcbasm-update-check@${target}.timer"
    ${SUDO} systemctl restart "${name}"

    echo "${name} を登録して起動しました。"
    echo "状態確認: $0 status ${target}"
}

remove_service() {
    local target="$1" name path
    name="$(service_name "${target}")"
    path="$(unit_path "${target}")"

    # install と対称に旧 unit も撤去する。未移行の機体には新 unit が無いので、先に
    # 掃除しないと下の「登録されていません」で抜けてしまい、`make webui` を失って
    # restart ループに入る旧 unit が enabled のまま残る。
    if [ "${target}" = "api" ]; then
        purge_legacy_unit
    fi

    if [ ! -e "${path}" ]; then
        ${SUDO} systemctl disable --now "pcbasm-update-check@${target}.timer" 2>/dev/null || true
        ${SUDO} rm -f \
            "${SYSTEMD_UNIT_DIR}/pcbasm-update@${target}.service" \
            "${SYSTEMD_UNIT_DIR}/pcbasm-update-check@${target}.service" \
            "${SYSTEMD_UNIT_DIR}/pcbasm-update-check@${target}.timer" \
            "${SUDOERS_DIR}/pcbasm-update-${target}"
        ${SUDO} systemctl daemon-reload
        echo "${name} は登録されていません。"
        return 0
    fi

    ${SUDO} systemctl disable --now "${name}"
    ${SUDO} systemctl disable --now "pcbasm-update-check@${target}.timer" || true
    ${SUDO} rm -f "${path}"
    ${SUDO} rm -f \
        "${SYSTEMD_UNIT_DIR}/pcbasm-update@${target}.service" \
        "${SYSTEMD_UNIT_DIR}/pcbasm-update-check@${target}.service" \
        "${SYSTEMD_UNIT_DIR}/pcbasm-update-check@${target}.timer" \
        "${SUDOERS_DIR}/pcbasm-update-${target}"
    ${SUDO} systemctl daemon-reload
    ${SUDO} systemctl reset-failed "${name}" 2>/dev/null || true

    echo "${name} を停止し、登録を削除しました。"
}

# 未登録の扱いは呼び出し方で変える（第 3 引数 on_missing）。
#   error … 単体指定（`start api`）。対象を明示した以上、未登録は打ち間違いか install 忘れ
#           なので従来どおりエラー終了する
#   skip  … `all`。片方だけ入っている構成（機体は api のみ、frontend 機は ui のみ）が正常
#           なので、警告して次の対象へ進む。同じ `all` ループ内の remove が未登録を
#           読み飛ばして継続するのと挙動を揃える
control_service() {
    local action="$1" target="$2" on_missing="$3" name path
    name="$(service_name "${target}")"
    path="$(unit_path "${target}")"

    if [ ! -e "${path}" ]; then
        if [ "${on_missing}" = "skip" ]; then
            echo "警告: ${name} は登録されていません。読み飛ばします（登録するには '$0 install ${target}'）。" >&2
            return 0
        fi
        echo "エラー: ${name} は登録されていません。先に '$0 install ${target}' を実行してください。" >&2
        exit 1
    fi

    ${SUDO} systemctl "${action}" "${name}"
}

# systemctl の終了コードをそのまま返す（inactive=3 / 未登録=4）。呼び出し側が
# 全対象を回しきるため、ここでは失敗しても抜けさせない。
show_status() {
    require_command systemctl
    systemctl status --no-pager "$(service_name "$1")"
}

main() {
    local command="${1:-}" targets target on_missing status_rc=0 rc
    local -a target_list

    case "${command}" in
        install | start | stop | restart | status | remove) ;;
        -h | --help)
            usage
            return 0
            ;;
        *)
            usage >&2
            return 2
            ;;
    esac

    # 対象の検証は systemd に触る前に済ませる
    targets="$(resolve_targets "${2:-}")" || {
        usage >&2
        return 2
    }

    read -r -a target_list <<<"${targets}"

    # 複数対象（= all）では未登録を致命的に扱わない（control_service のコメント参照）
    on_missing="error"
    if [ "${#target_list[@]}" -gt 1 ]; then
        on_missing="skip"
    fi

    if [ "${command}" != "status" ]; then
        require_privileged_tools
    fi

    for target in "${target_list[@]}"; do
        case "${command}" in
            install)
                install_service "${target}"
                ;;
            remove)
                remove_service "${target}"
                ;;
            status)
                # 診断経路。片方だけ落ちた同居機を切り分けるために使うので、非 0
                # （inactive=3 / 未登録=4）でも打ち切らず全対象の状態を表示する。
                # 終了コードは「全対象を見た結果」— 最初の非 0 をそのまま返す。
                rc=0
                show_status "${target}" || rc=$?
                if [ "${status_rc}" -eq 0 ]; then
                    status_rc="${rc}"
                fi
                ;;
            *)
                control_service "${command}" "${target}" "${on_missing}"
                ;;
        esac
    done

    return "${status_rc}"
}

# source されたとき（テスト）は dispatch しない
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
