#!/bin/bash
# マシン設定のセットアップスクリプト。
# data/config-templates/<マシン名>.<用途>/ を選び、以下を行う。
#
#   1. machine.toml を config/ へ配置する（既存なら常に保持）
#   2. printer.cfg を Klipper 本来の場所（~/printer_data/config/printer.cfg）へ実ファイルとして
#      配置する（既に実ファイルなら SAVE_CONFIG の較正値を守るため保持）
#   3. config/printer.cfg に 2 のシンボリックリンクを張る（リポジトリから閲覧するため）
#   4. klipper.env の KLIPPER_ARGS を 2 のパスに向ける
#
# カメラキャリブレーション結果は機体固有なのでテンプレートには含めない。セットアップ後に
# WebUI の camera_calibration ジョブを実行し、Apply で config/ に生成させる。
#
# config/ は .gitignore 済み。SAVE_CONFIG の較正値は ~/printer_data 側に書かれるため
# リポジトリは dirty にならない。詳細は data/config-templates/README.md を参照。

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_ROOT="${PROJECT_ROOT}/data/config-templates"
CONFIG_DIR="${PROJECT_ROOT}/config"
KLIPPER_CONFIG_DIR="${HOME}/printer_data/config"
KLIPPER_CONFIG_FILE="${KLIPPER_CONFIG_DIR}/printer.cfg"
KLIPPER_ENV="${HOME}/printer_data/systemd/klipper.env"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

die() {
    echo "エラー: $1" >&2
    exit 1
}

# --- 前提チェック ---

[ -d "$TEMPLATE_ROOT" ] || die "テンプレートディレクトリが見つかりません: ${TEMPLATE_ROOT}"
[ -d "$KLIPPER_CONFIG_DIR" ] || die "${KLIPPER_CONFIG_DIR} が存在しません（Klipper が未インストールの可能性があります）"

# --- テンプレート列挙（machine.toml と printer.cfg の両方を持つものだけ） ---

templates=()
while IFS= read -r -d '' dir; do
    if [ -f "${dir}/machine.toml" ] && [ -f "${dir}/printer.cfg" ]; then
        templates+=("$(basename "$dir")")
    fi
done < <(find "$TEMPLATE_ROOT" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)

if [ ${#templates[@]} -eq 0 ]; then
    die "${TEMPLATE_ROOT} に machine.toml と printer.cfg を持つテンプレートが見つかりません"
fi

# --- 対話選択 ---

echo "利用可能なマシン設定テンプレート:"
echo "================================"
for i in "${!templates[@]}"; do
    echo "  $((i + 1)). ${templates[$i]}"
done
echo ""

while true; do
    read -rp "使用するテンプレートの番号を選択してください (1-${#templates[@]}): " choice || die "中止しました"
    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le ${#templates[@]} ]; then
        break
    fi
    echo "無効な選択です。1から${#templates[@]}の間で入力してください。"
done

selected="${templates[$((choice - 1))]}"
template_path="${TEMPLATE_ROOT}/${selected}"

echo ""
echo "選択: ${selected}"
echo ""

# --- 実行計画の組み立て（まだ何も書かない） ---
#
# machine.toml は既存なら常に保持する。運用中の machine.toml は WebUI が実測値を
# 書き込み続ける「正」であり、上書きすると操作者の設定が黙って巻き戻る。
# テンプレートから作り直したい場合は config/ を手動退避してから再実行させる。

plan=()

if [ -e "${CONFIG_DIR}/machine.toml" ]; then
    plan+=("config/machine.toml : 既存を保持（スキップ）")
    copy_machine_toml=false
else
    plan+=("config/machine.toml : テンプレートからコピー")
    copy_machine_toml=true
fi

# printer.cfg も machine.toml と同じく実測値が蓄積する（SAVE_CONFIG が load_cell_probe の
# 較正値や position_endstop を追記する）。実ファイルが既にあるなら Klipper 側の稼働設定が
# 正なので、テンプレートで上書きせず保持する。差し替えたい場合は手動で退避させる。
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
    plan+=("klipper.env : KLIPPER_ARGS の config パスを ${KLIPPER_CONFIG_FILE} に変更")
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
    cp "${template_path}/machine.toml" "${CONFIG_DIR}/machine.toml"
    echo "配置: ${CONFIG_DIR}/machine.toml"
fi

if [ "$printer_action" = replace_symlink ]; then
    rm "$KLIPPER_CONFIG_FILE"
fi
if [ "$printer_action" != keep ]; then
    cp "${template_path}/printer.cfg" "$KLIPPER_CONFIG_FILE"
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
echo "machine.toml をテンプレートで作り直したい場合は、config/ を退避してから再実行してください:"
echo "  mv config config.bak.${TIMESTAMP} && ./setup-machine-config.sh"
echo ""
echo "printer.cfg をテンプレートで作り直したい場合も同様に退避してから再実行してください:"
echo "  mv ${KLIPPER_CONFIG_FILE} ${KLIPPER_CONFIG_FILE}.bak.${TIMESTAMP} && ./setup-machine-config.sh"
