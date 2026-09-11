#!/bin/bash

set -euo pipefail

# WebUI からのソフトウェア更新（git pull → uv sync → サービス再起動）が使う
# 唯一の特権操作を /etc/sudoers.d へ登録する。
#
# 許すのは `systemctl restart --no-block <unit...>` の **3 変種の固定 argv だけ**。
# ワイルドカードは 1 文字も置かない（sudo の glob は `/` も食うため、引数に `*` を
# 1 つ入れるだけで「任意のコマンドを root で実行」に悪化しうる）。
# `systemctl status` / `is-active` も入れない（非特権で読めるうえ、`sudo systemctl status`
# はページャ経由で root シェルを取られる）。
SUDOERS_DIR="${SUDOERS_DIR:-/etc/sudoers.d}"
# sudo はドットを含むファイル名を読み飛ばすので拡張子を付けない。
SUDOERS_FILE_NAME="pcbasm-update"
# 以下 2 つはテスト用の seam（web-service.sh の SUDO と同じ作法）。既定値は実機のもの。
VISUDO="${VISUDO:-/usr/sbin/visudo}"
SUDO="${SUDO:-sudo}"

SYSTEMCTL="/usr/bin/systemctl"
CMND_ALIAS="PCBASM_UPDATE_RESTART"
API_UNIT="pcbasm-api.service"
UI_UNIT="pcbasm-ui.service"

usage() {
    cat <<EOF
Usage: $(basename "$0") <install|remove|show>

操作
  install  sudoers 断片を構文検査してから ${SUDOERS_DIR}/${SUDOERS_FILE_NAME} へ設置する
  remove   設置した sudoers 断片を削除する
  show     設置せずに内容を表示する（特権不要）

許可するのは以下の固定 argv だけ（順序も含めて契約）。
  ${SYSTEMCTL} restart --no-block ${API_UNIT}
  ${SYSTEMCTL} restart --no-block ${UI_UNIT}
  ${SYSTEMCTL} restart --no-block ${API_UNIT} ${UI_UNIT}
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

sudoers_path() {
    printf '%s/%s\n' "${SUDOERS_DIR}" "${SUDOERS_FILE_NAME}"
}

# sudoers 断片を stdout に組み立てるだけの関数（ファイルには触らない）。
# 同居機（api + ui）の変種は必ず api → ui の順で書く。コード側
# （web.selfupdate.service.CANONICAL_ORDER）も同じ順で argv を組む。順序が違うと
# sudo が別コマンドとみなして「パスワードが必要」になり、再起動できない。
render_sudoers() {
    local user
    user="$(id -un)"

    cat <<EOF
# WebUI からのソフトウェア更新が使う唯一の特権操作。
# scripts/install-update-sudoers.sh が生成する（手で編集しない）。
# 引数まで固定した argv だけを許可し、ワイルドカードは 1 文字も置かない。
Cmnd_Alias ${CMND_ALIAS} = \\
    ${SYSTEMCTL} restart --no-block ${API_UNIT}, \\
    ${SYSTEMCTL} restart --no-block ${UI_UNIT}, \\
    ${SYSTEMCTL} restart --no-block ${API_UNIT} ${UI_UNIT}
${user} ALL=(root:root) NOPASSWD: ${CMND_ALIAS}
EOF
}

# 設置後に「実際に許可されたか」を非対話で見る。
#
# `sudo -n -l <command...>` は **その argv を実行できるかだけ**を終了コードで返す。
# `sudo -n -l` の一覧を文字列照合すると、tty が無いときの 80 桁折り返し（許可行は
# 84〜102 文字ある）に当たって、正しく設置した機体でも一致しない。
# ここで落ちても設置自体は成功しているので警告に留める。
verify_sudoers() {
    local units
    # 3 変種すべてを見る。同居機が実際に使うのは 3 つ目（api と ui を並べた 1 回の呼び出し）
    # なので、単体 2 つだけ確認しても「install は成功、実行時の preflight で落ちる」が残る。
    for units in "${API_UNIT}" "${UI_UNIT}" "${API_UNIT} ${UI_UNIT}"; do
        # shellcheck disable=SC2086
        if ! ${SUDO} -n -l ${SYSTEMCTL} restart --no-block ${units} >/dev/null 2>&1; then
            echo "警告: '${SYSTEMCTL} restart --no-block ${units}' が許可されていません。" >&2
            return 0
        fi
    done
    echo "非対話 sudo の許可を確認しました（api / ui / 同居機の 3 変種）。"
}

install_sudoers() {
    local path tmp
    path="$(sudoers_path)"
    require_command "${VISUDO}"

    tmp="$(mktemp)"
    # trap 本文は関数フレームが巻き戻された後に評価されるため、local の ${tmp} を
    # 遅延展開すると空文字になる。設置時に展開して実パスを焼き込む（web-service.sh と同じ）。
    # shellcheck disable=SC2064
    trap "rm -f '${tmp}'" EXIT
    render_sudoers >"${tmp}"

    # 壊れた sudoers を置くと sudo 全体が死に、復旧手段（sudo での書き戻し）まで失う。
    # 検査を通るまで設置先には一切触らない。
    if ! ${VISUDO} -cf "${tmp}" >/dev/null; then
        echo "エラー: 生成した sudoers 断片が構文検査に通りませんでした。設置しません。" >&2
        return 1
    fi

    if [ -e "${path}" ] && cmp -s "${tmp}" "${path}"; then
        rm -f "${tmp}"
        trap - EXIT
        echo "${path} は最新です。"
        verify_sudoers
        return 0
    fi

    ${SUDO} install -m 0440 "${tmp}" "${path}"
    rm -f "${tmp}"
    trap - EXIT

    echo "${path} を設置しました。"
    verify_sudoers
}

remove_sudoers() {
    local path
    path="$(sudoers_path)"

    if [ ! -e "${path}" ]; then
        echo "${path} は設置されていません。"
        return 0
    fi

    ${SUDO} rm -f "${path}"
    echo "${path} を削除しました。"
}

main() {
    local command="${1:-}"

    if [ "$#" -gt 1 ]; then
        usage >&2
        return 2
    fi

    case "${command}" in
        show)
            render_sudoers
            ;;
        install)
            require_non_root
            install_sudoers
            ;;
        remove)
            require_non_root
            remove_sudoers
            ;;
        -h | --help)
            usage
            ;;
        *)
            usage >&2
            return 2
            ;;
    esac
}

# source されたとき（テスト）は dispatch しない
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
