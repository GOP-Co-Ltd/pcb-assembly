#!/bin/bash
# 旧 configs/<machine>/ レイアウトから単一 config/ レイアウトへの一時移行スクリプト。
#
#   1. 旧 configs/<machine>/ を選び、machine.toml とカメラキャリブ JSON を config/ へ配置する
#   2. printer.cfg を Klipper 本来の場所（~/printer_data/config/printer.cfg）へ実ファイルとして
#      配置する（既に実ファイルなら移行済みとみなして保持）
#   3. config/printer.cfg に 2 のシンボリックリンクを張る（リポジトリから閲覧するため）
#   4. klipper.env の KLIPPER_ARGS を 2 のパスに戻す（旧 install-printer-cfg.sh は repo 実パスを指していた）
#
# 移行が済んだらこのスクリプトは削除してよい。以降のセットアップは
# ./setup-machine-config.sh（data/config-templates/ から config/ を作る）を使う。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LEGACY_ROOT="${PROJECT_ROOT}/configs"
CONFIG_DIR="${PROJECT_ROOT}/config"
KLIPPER_CONFIG_DIR="${HOME}/printer_data/config"
KLIPPER_CONFIG_FILE="${KLIPPER_CONFIG_DIR}/printer.cfg"
KLIPPER_ENV="${HOME}/printer_data/systemd/klipper.env"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

# テスト用フィクスチャ（Klipper port 7126 = 非リッスン）を実機に配置する事故を防ぐ
EXCLUDED_DIRS=("test-fixture")

usage() {
    cat <<EOF
Usage: $(basename "$0") [移行元ディレクトリ]

  引数なし  ${LEGACY_ROOT} 配下のマシンディレクトリから対話的に選択する
  引数あり  指定したディレクトリ（machine.toml と printer.cfg を持つこと）から移行する
            configs/ を削除した後や、git show で取り出したコピーから移行する場合に使う
EOF
}

die() {
    echo "エラー: $1" >&2
    exit 1
}

is_excluded() {
    local name="$1" excluded
    for excluded in "${EXCLUDED_DIRS[@]}"; do
        [ "$name" = "$excluded" ] && return 0
    done
    return 1
}

# 移行元ディレクトリを対話的に選択して標準出力へ返す
select_source_dir() {
    [ -d "$LEGACY_ROOT" ] || die "${LEGACY_ROOT} が存在しません。移行元ディレクトリを引数で指定してください。"

    local machines=() dir name
    while IFS= read -r -d '' dir; do
        name="$(basename "$dir")"
        is_excluded "$name" && continue
        if [ -f "${dir}/machine.toml" ] && [ -f "${dir}/printer.cfg" ]; then
            machines+=("$name")
        fi
    done < <(find "$LEGACY_ROOT" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)

    if [ ${#machines[@]} -eq 0 ]; then
        die "${LEGACY_ROOT} に machine.toml と printer.cfg を持つマシンディレクトリが見つかりません"
    fi

    echo "移行元のマシン設定:" >&2
    echo "==================" >&2
    local i
    for i in "${!machines[@]}"; do
        echo "  $((i + 1)). ${machines[$i]}" >&2
    done
    echo "" >&2

    local choice
    while true; do
        read -rp "移行するマシンの番号を選択してください (1-${#machines[@]}): " choice || die "中止しました"
        if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le ${#machines[@]} ]; then
            break
        fi
        echo "無効な選択です。1から${#machines[@]}の間で入力してください。" >&2
    done

    echo "${LEGACY_ROOT}/${machines[$((choice - 1))]}"
}

case "${1:-}" in
    -h | --help)
        usage
        exit 0
        ;;
esac

[ $# -le 1 ] || {
    usage >&2
    exit 2
}

if [ $# -eq 1 ]; then
    source_dir="$(cd "$1" 2>/dev/null && pwd)" || die "ディレクトリが見つかりません: $1"
    [ -f "${source_dir}/machine.toml" ] || die "${source_dir}/machine.toml が見つかりません"
    [ -f "${source_dir}/printer.cfg" ] || die "${source_dir}/printer.cfg が見つかりません"
else
    source_dir="$(select_source_dir)"
fi

[ -d "$KLIPPER_CONFIG_DIR" ] || die "${KLIPPER_CONFIG_DIR} が存在しません（Klipper が未インストールの可能性があります）"

echo ""
echo "移行元: ${source_dir}"
echo ""

# --- 実行計画の組み立て（まだ何も書かない） ---

plan=()

if [ -e "${CONFIG_DIR}/machine.toml" ]; then
    plan+=("config/machine.toml : 既存を保持（スキップ）")
    copy_machine_toml=false
else
    plan+=("config/machine.toml : ${source_dir}/machine.toml からコピー")
    copy_machine_toml=true
fi

json_to_copy=()
for json in "${source_dir}"/*.json; do
    [ -e "$json" ] || continue
    name="$(basename "$json")"
    if [ -e "${CONFIG_DIR}/${name}" ]; then
        plan+=("config/${name} : 既存を保持（スキップ）")
    else
        plan+=("config/${name} : コピー")
        json_to_copy+=("$json")
    fi
done

# 実ファイルが既にあるなら移行済み（または Klipper 側が正）。SAVE_CONFIG の較正値を
# 上書きしないよう保持する。
if [ -L "$KLIPPER_CONFIG_FILE" ]; then
    plan+=("${KLIPPER_CONFIG_FILE} : シンボリックリンクを削除し printer.cfg を実ファイルとして配置")
    printer_action=replace_symlink
elif [ -f "$KLIPPER_CONFIG_FILE" ]; then
    plan+=("${KLIPPER_CONFIG_FILE} : 既存を保持（スキップ）— SAVE_CONFIG の較正値を上書きしない")
    printer_action=keep
else
    plan+=("${KLIPPER_CONFIG_FILE} : printer.cfg を配置")
    printer_action=copy
fi

if [ -L "${CONFIG_DIR}/printer.cfg" ]; then
    plan+=("config/printer.cfg : シンボリックリンクを張り直す")
elif [ -e "${CONFIG_DIR}/printer.cfg" ]; then
    die "${CONFIG_DIR}/printer.cfg が実ファイルです。手動で退避してから再実行してください"
else
    plan+=("config/printer.cfg : ${KLIPPER_CONFIG_FILE} へのシンボリックリンクを作成")
fi

if [ ! -f "$KLIPPER_ENV" ]; then
    plan+=("klipper.env : 見つからないためスキップ（警告）")
    env_action=skip
elif grep -Fq "klippy.py ${KLIPPER_CONFIG_FILE}" "$KLIPPER_ENV"; then
    plan+=("klipper.env : 既に ${KLIPPER_CONFIG_FILE} を指しています（変更なし）")
    env_action=noop
else
    plan+=("klipper.env : KLIPPER_ARGS の config パスを ${KLIPPER_CONFIG_FILE} に戻す")
    env_action=fix
fi

echo "実行内容:"
echo "========"
printf '  %s\n' "${plan[@]}"
echo ""
read -rp "上記を実行しますか? [y/N]: " answer || answer=""
case "$answer" in
    [yY]) ;;
    *)
        echo "中止しました"
        exit 0
        ;;
esac

# --- 実行 ---

mkdir -p "$CONFIG_DIR"

if [ "$copy_machine_toml" = true ]; then
    cp "${source_dir}/machine.toml" "${CONFIG_DIR}/machine.toml"
    echo "配置: ${CONFIG_DIR}/machine.toml"
fi

for json in ${json_to_copy+"${json_to_copy[@]}"}; do
    cp "$json" "${CONFIG_DIR}/"
    echo "配置: ${CONFIG_DIR}/$(basename "$json")"
done

if [ "$printer_action" = replace_symlink ]; then
    rm "$KLIPPER_CONFIG_FILE"
fi
if [ "$printer_action" != keep ]; then
    cp "${source_dir}/printer.cfg" "$KLIPPER_CONFIG_FILE"
    echo "配置: ${KLIPPER_CONFIG_FILE}"
fi

ln -sfn "$KLIPPER_CONFIG_FILE" "${CONFIG_DIR}/printer.cfg"
echo "シンボリックリンク: ${CONFIG_DIR}/printer.cfg -> ${KLIPPER_CONFIG_FILE}"

case "$env_action" in
    fix)
        sed -i -E "s|(klippy/klippy\.py) [^ ]+\.cfg|\1 ${KLIPPER_CONFIG_FILE}|" "$KLIPPER_ENV"
        if grep -Fq "klippy.py ${KLIPPER_CONFIG_FILE}" "$KLIPPER_ENV"; then
            echo "更新: ${KLIPPER_ENV} の config パスを ${KLIPPER_CONFIG_FILE} に変更"
        else
            echo "警告: ${KLIPPER_ENV} の KLIPPER_ARGS を書き換えられませんでした。手動で確認してください" >&2
        fi
        ;;
    skip)
        echo "警告: ${KLIPPER_ENV} が見つかりません（klipper.env の更新をスキップ）" >&2
        ;;
esac

echo ""
echo "完了しました"
echo "反映には Klipper の再起動が必要です: sudo systemctl restart klipper"
echo ""
echo "移行が確認できたら、このスクリプトは削除してよい:"
echo "  git rm scripts/migrate_config_layout.sh"
