#!/bin/bash
# printer.cfg インストールスクリプト
# configs/<machine_name>/printer.cfg から設定ファイルを選択し、
#   1. klipper.env の KLIPPER_ARGS の config パスを repo 実パスに向ける
#      （SAVE_CONFIG が symlink を壊さず repo ファイルを直接更新するため）
#   2. ~/printer_data/config/printer.cfg に閲覧用シンボリックリンクを作成する（Mainsail 用）

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG_DIR="${SCRIPT_DIR}/configs"
TARGET_DIR="${HOME}/printer_data/config"
TARGET_FILE="${TARGET_DIR}/printer.cfg"
KLIPPER_ENV="${HOME}/printer_data/systemd/klipper.env"

# マシンディレクトリを列挙（printer.cfgを持つもののみ）
machine_dirs=()
while IFS= read -r -d '' dir; do
    if [ -f "${dir}/printer.cfg" ]; then
        machine_dirs+=("$(basename "$dir")")
    fi
done < <(find "$CFG_DIR" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)

if [ ${#machine_dirs[@]} -eq 0 ]; then
    echo "エラー: ${CFG_DIR} にprinter.cfgを持つマシンディレクトリが見つかりません"
    exit 1
fi

# ダイアログ表示
echo "利用可能なマシン設定:"
echo "========================"
for i in "${!machine_dirs[@]}"; do
    echo "  $((i + 1)). ${machine_dirs[$i]}"
done
echo ""

# ユーザー入力
while true; do
    read -p "インストールするマシンの番号を選択してください (1-${#machine_dirs[@]}): " choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le ${#machine_dirs[@]} ]; then
        break
    fi
    echo "無効な選択です。1から${#machine_dirs[@]}の間で入力してください。"
done

selected_machine="${machine_dirs[$((choice - 1))]}"
source_path="${CFG_DIR}/${selected_machine}/printer.cfg"

echo ""
echo "選択: ${selected_machine}"

# ターゲットディレクトリの確認
if [ ! -d "$TARGET_DIR" ]; then
    echo "エラー: ${TARGET_DIR} が存在しません"
    exit 1
fi

# 既存のprinter.cfgをバックアップ
if [ -e "$TARGET_FILE" ] || [ -L "$TARGET_FILE" ]; then
    if [ -L "$TARGET_FILE" ]; then
        echo "既存のシンボリックリンクを削除: ${TARGET_FILE}"
        rm "$TARGET_FILE"
    else
        backup_file="${TARGET_FILE}.backup"
        echo "既存のファイルをバックアップ: ${TARGET_FILE} -> ${backup_file}"
        mv "$TARGET_FILE" "$backup_file"
    fi
fi

# シンボリックリンクを作成（Mainsail からの閲覧・編集用）
ln -s "$source_path" "$TARGET_FILE"
echo "シンボリックリンクを作成: ${TARGET_FILE} -> ${source_path}"

# klipper.env の config パスを repo 実パスに向ける
# （Klipper が symlink ではなく repo ファイルを直接参照することで、
#   SAVE_CONFIG が symlink を破壊せず repo に git diff として現れる）
if [ -f "$KLIPPER_ENV" ]; then
    sed -i -E "s|(klippy/klippy\.py) [^ ]+\.cfg|\1 ${source_path}|" "$KLIPPER_ENV"
    echo "klipper.env の config パスを更新: ${source_path}"
    echo ""
    echo "反映には Klipper の再起動が必要です: sudo systemctl restart klipper"
else
    echo "警告: ${KLIPPER_ENV} が見つかりません（klipper.env の更新をスキップ）"
fi
echo ""
echo "完了しました"
