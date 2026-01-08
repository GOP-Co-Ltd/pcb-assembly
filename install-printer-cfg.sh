#!/bin/bash
# printer.cfg インストールスクリプト
# configs/printer_cfgs/ から設定ファイルを選択し、~/printer_data/config/printer.cfg にシンボリックリンクを作成する

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG_DIR="${SCRIPT_DIR}/configs/printer_cfgs"
TARGET_DIR="${HOME}/printer_data/config"
TARGET_FILE="${TARGET_DIR}/printer.cfg"

# cfgファイルを列挙
cfg_files=()
while IFS= read -r -d '' file; do
    cfg_files+=("$(basename "$file")")
done < <(find "$CFG_DIR" -maxdepth 1 -name "*.cfg" -print0 | sort -z)

if [ ${#cfg_files[@]} -eq 0 ]; then
    echo "エラー: ${CFG_DIR} にcfgファイルが見つかりません"
    exit 1
fi

# ダイアログ表示
echo "利用可能な設定ファイル:"
echo "========================"
for i in "${!cfg_files[@]}"; do
    echo "  $((i + 1)). ${cfg_files[$i]}"
done
echo ""

# ユーザー入力
while true; do
    read -p "インストールするファイルの番号を選択してください (1-${#cfg_files[@]}): " choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le ${#cfg_files[@]} ]; then
        break
    fi
    echo "無効な選択です。1から${#cfg_files[@]}の間で入力してください。"
done

selected_file="${cfg_files[$((choice - 1))]}"
source_path="${CFG_DIR}/${selected_file}"

echo ""
echo "選択: ${selected_file}"

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

# シンボリックリンクを作成
ln -s "$source_path" "$TARGET_FILE"
echo "シンボリックリンクを作成: ${TARGET_FILE} -> ${source_path}"
echo ""
echo "完了しました"
