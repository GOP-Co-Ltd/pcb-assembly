#!/bin/bash

set -euo pipefail

SERVICE_NAME="pcbasm-webui.service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<EOF
Usage: $(basename "$0") <install|start|stop|restart|status|remove>

  install  ${SERVICE_NAME} を登録し、起動時に自動起動する（再登録時は完了待ち）
  start    サービスを起動する
  stop     実行中タスクの完了を待って一時停止する（自動起動設定は維持）
  restart  実行中タスクの完了を待って最新のソースで再起動する
  status   サービスの状態を表示する
  remove   実行中タスクの完了を待って停止・無効化し、登録を削除する

停止待機中は新規タスクを拒否し、実行中タスクの完了まで無期限に待機します。
緊急時は sudo systemctl kill --kill-whom=all --signal=SIGINT ${SERVICE_NAME} で
待機を打ち切れます。
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

install_service() {
    require_non_root
    require_command make
    require_command uv
    require_command sudo
    require_command systemctl

    local service_user service_group service_home make_bin uv_bin executable_path unit_file
    service_user="$(id -un)"
    service_group="$(id -gn)"
    service_home="${HOME}"
    make_bin="$(command -v make)"
    uv_bin="$(command -v uv)"
    executable_path="$(dirname "${uv_bin}"):/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    unit_file="$(mktemp)"
    trap 'rm -f "${unit_file:-}"' EXIT

    cat >"${unit_file}" <<EOF
[Unit]
Description=PCB Assembly WebUI
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=${service_user}
Group=${service_group}
WorkingDirectory=${PROJECT_ROOT}
Environment="HOME=${service_home}"
Environment="PATH=${executable_path}"
ExecStart=${make_bin} webui
Restart=on-failure
RestartSec=5
TimeoutStopSec=infinity

[Install]
WantedBy=multi-user.target
EOF

    sudo install -m 0644 "${unit_file}" "${UNIT_PATH}"
    rm -f "${unit_file}"
    trap - EXIT
    sudo systemctl daemon-reload
    sudo systemctl enable "${SERVICE_NAME}"
    sudo systemctl restart "${SERVICE_NAME}"

    echo "${SERVICE_NAME} を登録して起動しました。"
    echo "状態確認: $0 status"
}

remove_service() {
    require_non_root
    require_command sudo
    require_command systemctl

    if [ ! -e "${UNIT_PATH}" ]; then
        echo "${SERVICE_NAME} は登録されていません。"
        return
    fi

    sudo systemctl disable --now "${SERVICE_NAME}"
    sudo rm -f "${UNIT_PATH}"
    sudo systemctl daemon-reload
    sudo systemctl reset-failed "${SERVICE_NAME}" 2>/dev/null || true

    echo "${SERVICE_NAME} を停止し、登録を削除しました。"
}

control_service() {
    local action="$1"

    require_non_root
    require_command sudo
    require_command systemctl

    if [ ! -e "${UNIT_PATH}" ]; then
        echo "エラー: ${SERVICE_NAME} は登録されていません。先に '$0 install' を実行してください。" >&2
        exit 1
    fi

    sudo systemctl "${action}" "${SERVICE_NAME}"
}

show_status() {
    require_command systemctl
    systemctl status --no-pager "${SERVICE_NAME}"
}

case "${1:-}" in
    install)
        install_service
        ;;
    start)
        control_service start
        ;;
    stop)
        control_service stop
        ;;
    restart)
        control_service restart
        ;;
    remove)
        remove_service
        ;;
    status)
        show_status
        ;;
    -h | --help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
