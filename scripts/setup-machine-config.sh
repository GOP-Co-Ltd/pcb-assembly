#!/bin/bash
# マシン設定のセットアップスクリプト。
# data/config-templates/<マシン名>.<用途>/ を選び、以下を行う。
#
#   1. machine.toml を config/ へ配置する（既存なら常に保持）
#   2. printer.cfg を Klipper 本来の場所（~/printer_data/config/printer.cfg）へ実ファイルとして
#      配置する（既に実ファイルなら保持するか退避して上書きするかを確認する）
#   3. 2 で配置した printer.cfg の [mcu] serial を実機の /dev/serial/by-id/* に書き換える
#   4. config/printer.cfg に 2 のシンボリックリンクを張る（リポジトリから閲覧するため）
#   5. klipper.env の KLIPPER_ARGS を 2 のパスに向ける
#
# カメラキャリブレーション結果は機体固有なのでテンプレートには含めない。セットアップ後に
# WebUI の camera_calibration ジョブを実行し、Apply で config/ に生成させる。
#
# config/ は .gitignore 済み。SAVE_CONFIG の較正値は ~/printer_data 側に書かれるため
# リポジトリは dirty にならない。詳細は data/config-templates/README.md を参照。

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE_ROOT="${PROJECT_ROOT}/data/config-templates"
CONFIG_DIR="${PROJECT_ROOT}/config"
KLIPPER_CONFIG_DIR="${HOME}/printer_data/config"
KLIPPER_CONFIG_FILE="${KLIPPER_CONFIG_DIR}/printer.cfg"
KLIPPER_ENV="${HOME}/printer_data/systemd/klipper.env"
SERIAL_BY_ID_DIR="/dev/serial/by-id"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
KLIPPER_BACKUP_FILE="${KLIPPER_CONFIG_FILE}.bak.${TIMESTAMP}"

die() {
    echo "エラー: $1" >&2
    exit 1
}

# printer.cfg の [mcu] セクションの serial 行だけを書き換える。名前付き MCU
# （[mcu extra] など）は対象にしない。serial 行が無ければ 1 を返す。
write_mcu_serial() {
    local file="$1" serial="$2" tmp
    tmp="$(mktemp)"
    if awk -v serial="$serial" '
        /^\[/ { in_mcu = ($0 ~ /^\[mcu\]/) }
        in_mcu && /^[[:space:]]*serial[[:space:]]*:/ {
            print "serial: " serial
            replaced = 1
            next
        }
        { print }
        END { exit(replaced ? 0 : 1) }
    ' "$file" > "$tmp"; then
        cat "$tmp" > "$file"
        rm -f "$tmp"
        return 0
    fi
    rm -f "$tmp"
    return 1
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

# 本スクリプトは Klipper インストール直後に走らせるのが通常の使い方で、そこには
# kinematics: none の stub が既に置かれている。よって実ファイルがあっても既定は
# 「退避して上書き」とする。稼働中の機体では SAVE_CONFIG が load_cell_probe の較正値や
# position_endstop を追記しているため、そのケースだけ n で保持を選ばせる。
if [ -L "$KLIPPER_CONFIG_FILE" ]; then
    plan+=("${KLIPPER_CONFIG_FILE} : シンボリックリンクを削除し printer.cfg を実ファイルとして配置")
    printer_action=replace_symlink
elif [ -f "$KLIPPER_CONFIG_FILE" ]; then
    echo "${KLIPPER_CONFIG_FILE} は既に実ファイルとして存在します。"
    echo "Klipper インストール直後なら kinematics: none の stub なので上書きしてください（既定）。"
    echo "稼働中の機体で SAVE_CONFIG の較正値が蓄積している場合は n で保持します。"
    echo ""
    read -rp "テンプレートの printer.cfg で上書きしますか? 既存は ${KLIPPER_BACKUP_FILE} へ退避します [Y/n]: " \
        overwrite_printer || overwrite_printer=""
    echo ""
    case "$overwrite_printer" in
        [nN])
            plan+=("${KLIPPER_CONFIG_FILE} : 既存を保持（スキップ）— SAVE_CONFIG の較正値を上書きしない")
            printer_action=keep
            ;;
        *)
            plan+=("${KLIPPER_CONFIG_FILE} : ${KLIPPER_BACKUP_FILE} へ退避してテンプレートで上書き")
            printer_action=backup_and_copy
            ;;
    esac
else
    plan+=("${KLIPPER_CONFIG_FILE} : printer.cfg を配置")
    printer_action=copy
fi

# [mcu] serial は機体固有。テンプレートには他機の ID か placeholder が入っているため、
# 配置する printer.cfg には実機の /dev/serial/by-id/* を書き込む。既存を保持する場合は
# 稼働中の設定が正なので触らない。
mcu_serial=""
if [ "$printer_action" = keep ]; then
    plan+=("[mcu] serial : 既存の printer.cfg を保持するため変更しない")
else
    serial_candidates=()
    while IFS= read -r -d '' path; do
        serial_candidates+=("$path")
    done < <(find "$SERIAL_BY_ID_DIR" -mindepth 1 -maxdepth 1 -print0 2> /dev/null | sort -z)

    case ${#serial_candidates[@]} in
        0)
            plan+=("[mcu] serial : ${SERIAL_BY_ID_DIR} にデバイスが無いためテンプレートの値のまま（警告）")
            ;;
        1)
            mcu_serial="${serial_candidates[0]}"
            plan+=("[mcu] serial : ${mcu_serial} を書き込む")
            ;;
        *)
            echo "${SERIAL_BY_ID_DIR} に複数のデバイスが見つかりました:"
            for i in "${!serial_candidates[@]}"; do
                echo "  $((i + 1)). ${serial_candidates[$i]}"
            done
            echo ""
            while true; do
                read -rp "MCU のデバイス番号を選択してください (1-${#serial_candidates[@]}): " serial_choice \
                    || die "中止しました"
                if [[ "$serial_choice" =~ ^[0-9]+$ ]] \
                    && [ "$serial_choice" -ge 1 ] && [ "$serial_choice" -le ${#serial_candidates[@]} ]; then
                    break
                fi
                echo "無効な選択です。1から${#serial_candidates[@]}の間で入力してください。"
            done
            mcu_serial="${serial_candidates[$((serial_choice - 1))]}"
            echo ""
            plan+=("[mcu] serial : ${mcu_serial} を書き込む")
            ;;
    esac
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
elif [ "$printer_action" = backup_and_copy ]; then
    cp -p "$KLIPPER_CONFIG_FILE" "$KLIPPER_BACKUP_FILE"
    echo "退避: ${KLIPPER_BACKUP_FILE}"
fi
if [ "$printer_action" != keep ]; then
    cp "${template_path}/printer.cfg" "$KLIPPER_CONFIG_FILE"
    echo "配置: ${KLIPPER_CONFIG_FILE}"
    if [ -n "$mcu_serial" ]; then
        if write_mcu_serial "$KLIPPER_CONFIG_FILE" "$mcu_serial"; then
            echo "設定: [mcu] serial = ${mcu_serial}"
        else
            echo "警告: ${KLIPPER_CONFIG_FILE} の [mcu] セクションに serial 行がありません。手動で設定してください" >&2
        fi
    else
        echo "警告: ${SERIAL_BY_ID_DIR} に MCU が見つかりません。${KLIPPER_CONFIG_FILE} の [mcu] serial を手動で設定してください" >&2
    fi
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
echo "  mv config config.bak.${TIMESTAMP} && ./scripts/setup-machine-config.sh"
echo ""
echo "printer.cfg は再実行すれば退避のうえテンプレートで作り直せます"
echo "（既存は printer.cfg.bak.<日時> へ退避されます）"
