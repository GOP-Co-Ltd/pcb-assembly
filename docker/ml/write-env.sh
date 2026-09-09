#!/bin/bash
#
# compose へ渡す設定を host の実状から生成する。
#
# 1. docker/ml/.env         コンテナ内ユーザーを host と同じ uid/gid で作るための値
# 2. docker/ml/compose.credentials.yaml
#                        git / glab の資格情報 mount。存在する source だけを書く
#
# 資格情報 mount を別 file へ分けるのは、存在しない path を bind mount source に
# 書くと Docker がそこへ root 所有の空 directory を作ってしまうため。host の
# ~/.gitconfig の位置に root 所有 directory ができると host 側の git が壊れ、
# 一般ユーザーでは消せない。
#
# `make ml-docker-env` から呼ぶ。どちらの生成物も Git 管理外。

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly ENV_FILE="${SCRIPT_DIR}/.env"
readonly CREDENTIALS_FILE="${SCRIPT_DIR}/compose.credentials.yaml"

die() {
    echo "エラー: $*" >&2
    exit 1
}

if [ -z "${HOME:-}" ]; then
    die "HOME が設定されていません。資格情報の場所を決められません。"
fi

user_name="$(id -un)"
user_uid="$(id -u)"
user_gid="$(id -g)"

if [ "${user_uid}" -eq 0 ]; then
    die "root では実行しないでください。host の一般ユーザーで実行します。"
fi

cat > "${ENV_FILE}" <<EOF
USER_NAME=${user_name}
USER_UID=${user_uid}
USER_GID=${user_gid}
EOF

container_home="/home/${user_name}"

# 生成する YAML を組み立てる。1 件も無ければ volumes key 自体を書かない
mounts=()
notes=()

if [ -f "${HOME}/.gitconfig" ]; then
    mounts+=("      - ${HOME}/.gitconfig:${container_home}/.gitconfig:ro")
    notes+=("~/.gitconfig (read-only)")
fi

if [ -d "${HOME}/.config/glab-cli" ]; then
    mounts+=("      - ${HOME}/.config/glab-cli:${container_home}/.config/glab-cli:ro")
    notes+=("~/.config/glab-cli (read-only)")
fi

# SSH agent があれば socket だけを渡す。秘密鍵そのものをコンテナへ見せずに
# 署名だけ host 側へ委譲できるため、鍵の露出範囲が狭い。
if [ -n "${SSH_AUTH_SOCK:-}" ] && [ -S "${SSH_AUTH_SOCK}" ]; then
    mounts+=("      - ${SSH_AUTH_SOCK}:/ssh-agent.sock")
    notes+=("SSH agent socket")
elif [ -d "${HOME}/.ssh" ]; then
    # agent が無い場合の fallback。read-only でも読み出しは防げないため、
    # コンテナ内で動く任意の code が全鍵を読めることを README に明記してある。
    mounts+=("      - ${HOME}/.ssh:${container_home}/.ssh:ro")
    notes+=("~/.ssh (read-only。鍵の露出範囲に注意)")
fi

{
    echo "# ${SCRIPT_DIR#"${PWD}/"}/write-env.sh が生成する。手で編集しない。"
    echo "# host に存在する資格情報だけを mount する。"
    echo ""
    echo "services:"
    echo "  ml:"
    if [ -n "${SSH_AUTH_SOCK:-}" ] && [ -S "${SSH_AUTH_SOCK}" ]; then
        echo "    environment:"
        echo "      SSH_AUTH_SOCK: /ssh-agent.sock"
    fi
    if [ "${#mounts[@]}" -gt 0 ]; then
        echo "    volumes:"
        printf '%s\n' "${mounts[@]}"
    else
        # compose は空の service を許すので、mount が無くても有効な file になる
        echo "    volumes: []"
    fi
} > "${CREDENTIALS_FILE}"

echo "docker/ml/.env:"
sed 's/^/  /' "${ENV_FILE}"

if [ "${#notes[@]}" -gt 0 ]; then
    echo "資格情報 mount:"
    printf '  %s\n' "${notes[@]}"
else
    echo "資格情報 mount: なし（コンテナ内から git / glab は使えません）"
fi
