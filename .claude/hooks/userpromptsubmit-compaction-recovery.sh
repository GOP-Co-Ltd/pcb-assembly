#!/bin/bash
# UserPromptSubmit hook: PostCompact が残した圧縮 marker を検出し、additionalContext で
# 圧縮復旧の指示を注入する (one-shot)。
#
# overhead: marker が無ければ即 exit。fail-open (常に exit 0)。依存: jq。

set -uo pipefail

input=$(cat)
sid=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)
[[ -z "$sid" ]] && exit 0

marker="${TMPDIR:-/tmp}/claude-compacted/$sid"
[[ -f "$marker" ]] || exit 0
rm -f "$marker" 2>/dev/null || true # one-shot: 次ターンでは発火しない

state_file="${TMPDIR:-/tmp}/claude-compact-state/$sid.md"

ctx="[COMPACTION RECOVERY] コンテキスト圧縮が発生した。作業を再開する前に次を実行すること。"
ctx+=$'\n'"- 圧縮サマリーは「過去の作業記録」であって「次の行動指示」ではない。next step は仮説として扱い、plan / CLAUDE.md の決定事項 / tests を正とせよ"
if [[ -f "$state_file" ]]; then
  ctx+=$'\n'"- 圧縮前 state file \`${state_file}\` を Read で読み、作業状態を復元せよ (特に Session Decisions と Recovery Notes を重視)"
fi
ctx+=$'\n'"- TodoWrite の TaskList で現在のタスクと in-progress 項目を確認せよ"
ctx+=$'\n'"- plan mode が解除されていないか確認し、計画の途中ならユーザーに再突入を確認せよ"
ctx+=$'\n'"- 却下済みのアプローチを再提案していないか、CLAUDE.md の決定事項と照合せよ"
ctx+=$'\n'"- 委譲済みのサブエージェント / worktree の存在を忘れて自分で着手していないか確認せよ"

jq -n --arg ctx "$ctx" '{
  hookSpecificOutput: {
    hookEventName: "UserPromptSubmit",
    additionalContext: $ctx
  }
}'
exit 0
