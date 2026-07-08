#!/bin/bash
# Claude Code statusLine: model / git branch / context 使用率(%) を 1 行で表示し、
# 使用率が閾値以上になったら compact-prep 警告 marker を書く (cooldown 中でなければ)。
#
# stdin = Claude Code が渡す statusLine JSON。context 使用率は transcript 末尾側の
# 最新 usage エントリ (input + cache_read + cache_creation + output) を context
# window で割って算出する。window は model id に "1m" を含めば 1,000,000、200k 超で
# 走っていれば 1,000,000、それ以外 200,000。
#
# fail-open: 何が失敗しても最低限の 1 行を返し exit 0。依存: jq (devcontainer 同梱)。

set -uo pipefail

input=$(cat)

j() { printf '%s' "$input" | jq -r "$1" 2>/dev/null; }

sid=$(j '.session_id // empty')
model=$(j '.model.display_name // .model.id // "?"')
model_id=$(j '.model.id // ""')
transcript=$(j '.transcript_path // empty')
cwd=$(j '.workspace.current_dir // .cwd // empty')
exceeds=$(j '.exceeds_200k_tokens // false')

# git branch (cwd があれば)
branch=""
if [[ -n "$cwd" ]]; then
  branch=$(git -C "$cwd" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
fi

# context window の判定
window=200000
case "$model_id" in
  *1m*) window=1000000 ;;
esac
if [[ "$exceeds" == "true" && "$window" -lt 1000000 ]]; then
  window=1000000
fi

# 現在の context トークン数 = transcript 末尾側の最新 usage 合計
used=0
if [[ -n "$transcript" && -f "$transcript" ]]; then
  used=$(tail -n 400 "$transcript" 2>/dev/null | jq -rs '
    [ .[] | (.message.usage // empty)
      | ((.input_tokens // 0) + (.cache_read_input_tokens // 0)
         + (.cache_creation_input_tokens // 0) + (.output_tokens // 0)) ]
    | last // 0' 2>/dev/null)
fi
[[ "$used" =~ ^[0-9]+$ ]] || used=0

pct=0
if [[ "$window" -gt 0 && "$used" -gt 0 ]]; then
  pct=$((used * 100 / window))
fi

# 閾値超で 60% 警告 marker を書く (cooldown 中でなければ)
threshold=60
if [[ -n "$sid" && "$pct" -ge "$threshold" ]]; then
  warned="${TMPDIR:-/tmp}/claude-compact-warned/$sid"
  if [[ ! -f "$warned" ]]; then
    warn_dir="${TMPDIR:-/tmp}/claude-compact-warn"
    mkdir -p "$warn_dir" 2>/dev/null || true
    printf '%s\n' "$pct" >"$warn_dir/$sid" 2>/dev/null || true
  fi
fi

# 表示行
line="⏺ ${model}"
if [[ -n "$branch" ]]; then
  line="${line} | ⎇ ${branch}"
fi
line="${line} | ctx ${pct}% of $((window / 1000))k"
printf '%s\n' "$line"
exit 0
