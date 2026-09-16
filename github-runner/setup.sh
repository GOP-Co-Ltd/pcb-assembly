#!/bin/bash

set -euo pipefail

readonly REPO_URL="https://github.com/GOP-Co-Ltd/pcb-assembly"
readonly REPO_SLUG="GOP-Co-Ltd/pcb-assembly"
readonly RUNNER_USER="github-runner"
readonly RUNNER_ROOT="/opt/actions-runner"
readonly RUNNER_NAME_PREFIX="pcb-assembly-rpi"
readonly RUNNER_LABELS="rpi-ci"
# GitHub Actions の self-hosted runner は 1 instance あたり 1 job しか
# 実行しない。GitLab Runner の concurrent = 3 に合わせて 3 instance 置く。
readonly RUNNER_COUNT="3"
readonly UV_VERSION="0.10.9"

usage() {
    cat <<EOF
Usage: $(basename "$0") <setup|install|register|status|verify>

  setup     依存関係をインストールし、runnerを登録・検証する
  install   OS依存関係、Actions Runner、uvを導入する
  register  registration tokenでrunnerを登録し、serviceとして起動する
  status    runner serviceの状態を表示する
  verify    service、runner登録、GitHub接続、host resourceを検証する

環境変数:
  RUNNER_VERSION  導入するActions Runnerのversion（既定: 最新release）
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

instance_dir() {
    echo "${RUNNER_ROOT}/${RUNNER_NAME_PREFIX}-$1"
}

instance_name() {
    echo "${RUNNER_NAME_PREFIX}-$1"
}

service_name() {
    local instance_directory service_file

    instance_directory="$(instance_dir "$1")"
    service_file="${instance_directory}/.service"
    if ! sudo test -f "${service_file}"; then
        return 1
    fi
    sudo cat "${service_file}"
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

resolve_runner_version() {
    local version

    if [ -n "${RUNNER_VERSION:-}" ]; then
        echo "${RUNNER_VERSION#v}"
        return
    fi

    version="$(
        curl --fail --silent --show-error --location \
            "https://api.github.com/repos/actions/runner/releases/latest" |
            python3 -c 'import json, sys; print(json.load(sys.stdin)["tag_name"])'
    )"
    test -n "${version}" || die "Actions Runnerの最新versionを取得できませんでした"
    echo "${version#v}"
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

create_runner_user() {
    if getent passwd "${RUNNER_USER}" >/dev/null; then
        return
    fi

    # CI jobはhost上で直接コマンドを実行する。cameraや装置を触らせないため
    # video / gpio / dialout groupには入れない。
    sudo useradd --system --create-home --shell /bin/bash "${RUNNER_USER}"
}

prepare_runner_cache() {
    local runner_group runner_home

    runner_home="$(getent passwd "${RUNNER_USER}" | cut -d: -f6)"
    if [ -z "${runner_home}" ]; then
        die "${RUNNER_USER} userのhome directoryを取得できませんでした"
    fi
    runner_group="$(id -gn "${RUNNER_USER}")"

    sudo install -d -m 0755 -o "${RUNNER_USER}" -g "${runner_group}" \
        "${runner_home}/.cache" \
        "${runner_home}/.cache/uv" \
        "${runner_home}/.cache/pre-commit"
}

install_runner_instances() {
    local version tarball runner_group instance index

    version="$(resolve_runner_version)"
    runner_group="$(id -gn "${RUNNER_USER}")"
    sudo install -d -m 0755 -o "${RUNNER_USER}" -g "${runner_group}" "${RUNNER_ROOT}"

    tarball="$(mktemp --suffix=.tar.gz)"
    trap 'rm -f "${tarball:-}"' EXIT
    curl --fail --silent --show-error --location \
        "https://github.com/actions/runner/releases/download/v${version}/actions-runner-linux-arm64-${version}.tar.gz" \
        --output "${tarball}"
    # mktempは0600で作るため、そのままでは展開側の${RUNNER_USER}が読めない。
    chmod 0644 "${tarball}"

    for index in $(seq 1 "${RUNNER_COUNT}"); do
        instance="$(instance_dir "${index}")"
        if sudo test -x "${instance}/config.sh"; then
            continue
        fi
        sudo install -d -m 0755 -o "${RUNNER_USER}" -g "${runner_group}" "${instance}"
        sudo -u "${RUNNER_USER}" tar --extract --gzip --file "${tarball}" \
            --directory "${instance}"
    done

    rm -f "${tarball}"
    trap - EXIT

    # installdependencies.shはrunner root上での実行を前提にしている。
    sudo bash -c 'cd "$1" && ./bin/installdependencies.sh' _ "$(instance_dir 1)"
    echo "Actions Runner ${version}を${RUNNER_COUNT} instance導入しました。"
}

install_dependencies() {
    require_command sudo
    require_command curl
    check_platform

    sudo apt-get update
    sudo apt-get install --yes \
        ca-certificates \
        curl \
        git \
        git-lfs \
        libgl1 \
        libglib2.0-0t64 \
        make \
        python3 \
        python3-picamera2 \
        v4l-utils
    sudo git lfs install --system --skip-repo
    verify_git_lfs
    sudo apt-get install --yes --no-install-recommends kicad
    verify_pcbnew_import

    install_uv
    create_runner_user
    prepare_runner_cache
    install_runner_instances

    echo "GitHub Actions RunnerとCI依存関係をインストールしました。"
}

verify_git_lfs() {
    if ! git lfs version >/dev/null; then
        die "Git LFSを実行できません"
    fi
    if ! git config --system --get filter.lfs.process >/dev/null; then
        die "Git LFSのsystem filter設定を確認できません"
    fi
}

verify_pcbnew_import() {
    if ! /usr/bin/python3 -c 'import pcbnew'; then
        die "/usr/bin/python3からpcbnewをimportできません"
    fi
}

instance_is_registered() {
    sudo test -f "$(instance_dir "$1")/.runner"
}

read_registration_token() {
    local token

    if command -v gh >/dev/null 2>&1 &&
        token="$(gh api --method POST \
            "repos/${REPO_SLUG}/actions/runners/registration-token" \
            --jq .token 2>/dev/null)" && [ -n "${token}" ]; then
        echo "${token}"
        return
    fi

    if ! IFS= read -r -s -p "Runner registration token (A...): " token </dev/tty; then
        printf '\n' >&2
        die "registration tokenを読み取れませんでした"
    fi
    printf '\n' >&2
    test -n "${token}" || die "registration tokenが空です"
    echo "${token}"
}

register_instance() {
    local index token instance name

    index="$1"
    token="$2"
    instance="$(instance_dir "${index}")"
    name="$(instance_name "${index}")"

    # tokenはconfig.shへ標準入力ではなく引数で渡す必要がある。runner userの
    # shell履歴には残らず、登録後は.runner / .credentialsにのみ保存される。
    if ! sudo -u "${RUNNER_USER}" env RUNNER_ALLOW_RUNASROOT=0 \
        "${instance}/config.sh" \
        --unattended \
        --replace \
        --url "${REPO_URL}" \
        --token "${token}" \
        --name "${name}" \
        --labels "${RUNNER_LABELS}" \
        --work "_work"; then
        die "${name}の登録に失敗しました"
    fi

    echo "${name}を登録しました。"
}

install_instance_service() {
    local index instance service

    index="$1"
    instance="$(instance_dir "${index}")"

    if ! service="$(service_name "${index}")"; then
        sudo "${instance}/svc.sh" install "${RUNNER_USER}"
        service="$(service_name "${index}")" ||
            die "$(instance_name "${index}")のservice名を取得できませんでした"
    fi

    sudo systemctl enable --now "${service}"
    sudo systemctl restart "${service}"
}

register_runner() {
    local index token=""

    require_command sudo
    require_command systemctl

    for index in $(seq 1 "${RUNNER_COUNT}"); do
        sudo test -x "$(instance_dir "${index}")/config.sh" ||
            die "$(instance_dir "${index}")が未インストールです。先にinstallを実行してください。"

        if instance_is_registered "${index}"; then
            echo "$(instance_name "${index}")は登録済みです。"
        else
            # registration tokenは有効期限内なら複数runnerに再利用できる。
            if [ -z "${token}" ]; then
                token="$(read_registration_token)"
            fi
            register_instance "${index}" "${token}"
        fi

        install_instance_service "${index}"
    done
    token=""
}

show_status() {
    local index service

    require_command systemctl

    for index in $(seq 1 "${RUNNER_COUNT}"); do
        if service="$(service_name "${index}")"; then
            systemctl status --no-pager "${service}" || true
        else
            echo "$(instance_name "${index}"): 未登録"
        fi
        echo
    done
}

verify_registered_runners() {
    require_command gh

    gh api "repos/${REPO_SLUG}/actions/runners" --jq \
        '.runners[] | "\(.name)\t\(.status)\tbusy=\(.busy)\t[\([.labels[].name] | join(","))]"'
}

verify_runner() {
    local index installed_version runner_home service

    require_command df
    require_command free
    require_command nproc
    require_command sudo
    require_command systemctl

    for index in $(seq 1 "${RUNNER_COUNT}"); do
        instance_is_registered "${index}" ||
            die "$(instance_name "${index}")が登録されていません"
        service="$(service_name "${index}")" ||
            die "$(instance_name "${index}")のserviceが作成されていません"
        systemctl is-enabled --quiet "${service}" ||
            die "${service}が有効化されていません"
        systemctl is-active --quiet "${service}" ||
            die "${service}が起動していません"
    done

    verify_git_lfs
    verify_pcbnew_import

    installed_version="$(/usr/local/bin/uv --version | awk '{print $2}')"
    if [ "${installed_version}" != "${UV_VERSION}" ]; then
        die "uv versionが${UV_VERSION}ではありません: ${installed_version}"
    fi

    if command -v gh >/dev/null 2>&1; then
        echo
        verify_registered_runners
    else
        echo "ghが無いためGitHub側の登録状態は確認しませんでした。"
    fi

    runner_home="$(getent passwd "${RUNNER_USER}" | cut -d: -f6)"
    echo
    echo "CPU cores: $(nproc)"
    free -h
    df -h "${runner_home}"
    echo
    echo "GitHub Actions Runnerの設定と接続を検証しました。"
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
