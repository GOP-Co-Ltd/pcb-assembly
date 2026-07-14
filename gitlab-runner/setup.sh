#!/bin/bash

set -euo pipefail

readonly GITLAB_URL="https://gitlab.com"
readonly RUNNER_NAME="pcb-assembly-rpi"
readonly RUNNER_SERVICE="gitlab-runner"
readonly RUNNER_CONFIG="/etc/gitlab-runner/config.toml"
readonly RUNNER_CONCURRENCY="3"
readonly UV_VERSION="0.10.9"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly RUNNER_TEMPLATE="${SCRIPT_DIR}/config.template.toml"

usage() {
    cat <<EOF
Usage: $(basename "$0") <setup|install|register|status|verify>

  setup     依存関係をインストールし、runnerを登録・検証する
  install   OS依存関係、GitLab Runner、uvをインストールする
  register  GitLab UIで発行した認証tokenを使ってrunnerを登録する
  status    GitLab Runner serviceの状態を表示する
  verify    service、runner設定、GitLab接続、host resourceを検証する
EOF
}

die() {
    echo "エラー: $*" >&2
    exit 1
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        die "必要なコマンドが見つかりません: $1"
    fi
}

require_non_root() {
    if [ "${EUID}" -eq 0 ]; then
        die "sudoを付けずに実行してください。必要な操作はスクリプト内でsudoを使用します。"
    fi
}

check_platform() {
    require_command dpkg

    # shellcheck disable=SC1091
    source /etc/os-release

    case "${ID:-} ${ID_LIKE:-}" in
        *debian*) ;;
        *) die "Debian系OSが必要です: ID=${ID:-unknown}" ;;
    esac

    if [ "${VERSION_CODENAME:-}" != "trixie" ]; then
        die "Debian/Raspberry Pi OS Trixieが必要です: VERSION_CODENAME=${VERSION_CODENAME:-unknown}"
    fi

    if [ "$(dpkg --print-architecture)" != "arm64" ]; then
        die "arm64環境が必要です: architecture=$(dpkg --print-architecture)"
    fi
}

install_gitlab_repository() {
    local repository_script

    if [ -f /etc/apt/sources.list.d/runner_gitlab-runner.list ]; then
        return
    fi

    repository_script="$(mktemp)"
    trap 'rm -f "${repository_script:-}"' EXIT
    curl --fail --silent --show-error --location \
        "https://packages.gitlab.com/install/repositories/runner/gitlab-runner/script.deb.sh" \
        --output "${repository_script}"
    sudo bash "${repository_script}"
    rm -f "${repository_script}"
    trap - EXIT
}

install_uv() {
    local installed_version installer_script

    if [ -x /usr/local/bin/uv ]; then
        installed_version="$(/usr/local/bin/uv --version | awk '{print $2}')"
        if [ "${installed_version}" = "${UV_VERSION}" ]; then
            return
        fi
    fi

    installer_script="$(mktemp)"
    trap 'rm -f "${installer_script:-}"' EXIT
    curl --fail --silent --show-error --location \
        "https://astral.sh/uv/${UV_VERSION}/install.sh" \
        --output "${installer_script}"
    sudo env UV_UNMANAGED_INSTALL=/usr/local/bin sh "${installer_script}"
    rm -f "${installer_script}"
    trap - EXIT

    installed_version="$(/usr/local/bin/uv --version | awk '{print $2}')"
    if [ "${installed_version}" != "${UV_VERSION}" ]; then
        die "uv ${UV_VERSION}のインストールを確認できませんでした: ${installed_version}"
    fi
}

prepare_runner_cache() {
    local runner_group runner_home

    runner_home="$(getent passwd gitlab-runner | cut -d: -f6)"
    if [ -z "${runner_home}" ]; then
        die "gitlab-runner userのhome directoryを取得できませんでした"
    fi
    runner_group="$(id -gn gitlab-runner)"

    sudo install -d -m 0755 -o gitlab-runner -g "${runner_group}" \
        "${runner_home}/.cache" \
        "${runner_home}/.cache/uv" \
        "${runner_home}/.cache/pre-commit"
}

install_dependencies() {
    require_command sudo
    check_platform

    sudo apt-get update
    sudo apt-get install --yes \
        ca-certificates \
        curl \
        git \
        libgl1 \
        libglib2.0-0t64 \
        make \
        python3 \
        python3-picamera2 \
        v4l-utils

    require_command curl
    install_gitlab_repository
    sudo apt-get update
    sudo apt-get install --yes gitlab-runner

    install_uv
    prepare_runner_cache
    sudo systemctl enable --now "${RUNNER_SERVICE}"

    echo "GitLab RunnerとCI依存関係をインストールしました。"
}

initialize_global_config() {
    local configured_concurrency config_file

    sudo install -d -m 0755 "$(dirname "${RUNNER_CONFIG}")"

    if sudo test -e "${RUNNER_CONFIG}"; then
        configured_concurrency="$(
            sudo awk -F= \
                '/^[[:space:]]*concurrent[[:space:]]*=/ {
                    gsub(/[[:space:]]/, "", $2); print $2; exit
                }' \
                "${RUNNER_CONFIG}"
        )"
        if [ "${configured_concurrency}" != "${RUNNER_CONCURRENCY}" ]; then
            die "${RUNNER_CONFIG}のconcurrentを${RUNNER_CONCURRENCY}に設定してください"
        fi
        return
    fi

    config_file="$(mktemp)"
    trap 'rm -f "${config_file:-}"' EXIT
    printf 'concurrent = %s\ncheck_interval = 0\n' \
        "${RUNNER_CONCURRENCY}" >"${config_file}"
    sudo install -m 0600 -o root -g root "${config_file}" "${RUNNER_CONFIG}"
    rm -f "${config_file}"
    trap - EXIT
}

runner_is_registered() {
    sudo test -f "${RUNNER_CONFIG}" && \
        sudo grep --fixed-strings --quiet \
            "name = \"${RUNNER_NAME}\"" "${RUNNER_CONFIG}"
}

validate_runner_config() {
    sudo python3 - "${RUNNER_CONFIG}" "${RUNNER_NAME}" <<'PY'
import sys
import tomllib
from pathlib import Path

config_path = Path(sys.argv[1])
runner_name = sys.argv[2]

with config_path.open("rb") as config_file:
    config = tomllib.load(config_file)

if config.get("concurrent") != 3:
    raise SystemExit("global concurrentが3ではありません")

matching_runners = [
    runner for runner in config.get("runners", []) if runner.get("name") == runner_name
]
if len(matching_runners) != 1:
    raise SystemExit(f"{runner_name}の設定が1件ではありません")

runner = matching_runners[0]
expected = {"executor": "shell", "shell": "bash", "limit": 3}
for key, expected_value in expected.items():
    if runner.get(key) != expected_value:
        raise SystemExit(
            f"{runner_name}の{key}が{expected_value!r}ではありません"
        )
PY
}

register_runner() {
    local runner_token

    require_command gitlab-runner
    require_command sudo
    require_command systemctl
    test -f "${RUNNER_TEMPLATE}" || die "設定templateがありません: ${RUNNER_TEMPLATE}"

    initialize_global_config

    if runner_is_registered; then
        validate_runner_config
        sudo systemctl restart "${RUNNER_SERVICE}"
        echo "${RUNNER_NAME}は登録済みです。設定を検証しました。"
        return
    fi

    if ! IFS= read -r -s -p "Runner authentication token (glrt-...): " runner_token; then
        printf '\n' >&2
        die "認証tokenを読み取れませんでした"
    fi
    printf '\n'

    case "${runner_token}" in
        glrt-*) ;;
        *) die "glrt-で始まるrunner authentication tokenを入力してください" ;;
    esac

    export CI_SERVER_TOKEN="${runner_token}"
    if ! sudo --preserve-env=CI_SERVER_TOKEN gitlab-runner register \
        --config "${RUNNER_CONFIG}" \
        --non-interactive \
        --url "${GITLAB_URL}" \
        --name "${RUNNER_NAME}" \
        --executor shell \
        --template-config "${RUNNER_TEMPLATE}"; then
        unset CI_SERVER_TOKEN
        die "runnerの登録に失敗しました"
    fi
    unset CI_SERVER_TOKEN
    runner_token=""

    validate_runner_config
    sudo systemctl enable --now "${RUNNER_SERVICE}"
    sudo systemctl restart "${RUNNER_SERVICE}"
    echo "${RUNNER_NAME}を登録しました。"
}

show_status() {
    require_command systemctl
    systemctl status --no-pager "${RUNNER_SERVICE}"
}

verify_runner() {
    local installed_version runner_home

    require_command df
    require_command free
    require_command gitlab-runner
    require_command nproc
    require_command sudo
    require_command systemctl

    systemctl is-enabled --quiet "${RUNNER_SERVICE}" || \
        die "${RUNNER_SERVICE}が有効化されていません"
    systemctl is-active --quiet "${RUNNER_SERVICE}" || \
        die "${RUNNER_SERVICE}が起動していません"

    validate_runner_config
    sudo gitlab-runner verify

    installed_version="$(/usr/local/bin/uv --version | awk '{print $2}')"
    if [ "${installed_version}" != "${UV_VERSION}" ]; then
        die "uv versionが${UV_VERSION}ではありません: ${installed_version}"
    fi

    runner_home="$(getent passwd gitlab-runner | cut -d: -f6)"
    echo
    echo "CPU cores: $(nproc)"
    free -h
    df -h "${runner_home}"
    echo
    echo "GitLab Runnerの設定と接続を検証しました。"
}

case "${1:-}" in
    setup)
        require_non_root
        install_dependencies
        register_runner
        verify_runner
        ;;
    install)
        require_non_root
        install_dependencies
        ;;
    register)
        require_non_root
        register_runner
        ;;
    status)
        require_non_root
        show_status
        ;;
    verify)
        require_non_root
        verify_runner
        ;;
    -h | --help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
