# CLAUDE.md

プロジェクト共通のガイダンスは [AGENTS.md](AGENTS.md) が正典。Claude Code はそれを丸ごと読み込み、ここには Claude Code 固有の差分だけを書く。

@AGENTS.md

## Claude Code 固有の差分

AGENTS.md の記述を以下のとおり読み替える。

### 検証コマンド

- Claude が使う検証も AGENTS.md の標準フローと同じ `make check`
- pytest は実機に触れない。ただし実機テストがあった頃の防護層（`.claude/settings.json` の deny `Bash(make test:*)` と PreToolUse hook `.claude/hooks/pretooluse-block-hardware-tests.py`）が残っており、`make test` の直接実行と `-m "not hardware"` の無い pytest 起動を止める
- テストだけを流すときは `make test-no-hardware`、`pytest` を直接叩くときは `-m "not hardware"` を付ける
- 実機での確認はユーザーが受け入れ試験（`docs/acceptance.md`）で行う

### 資産の置き場所

- Codex と共有する skill の正典は `.agents/skills/<name>/`。`.claude/skills/<name>` はそこへの symlink なので、編集は `.agents/skills/` 側で行う
- `.claude/skills/` に実体を置くのは Claude 専用 skill だけ：`solo-dev-cycle`（単独開発フロー）、`edit-dot-claude`（`.claude/` 編集時の permission prompt 抑制）、`compact-prep`（下記）、`doc-teacher-student`（文書を haiku の理解度テストで改善するループ）、`agent-team-startup`（Claude 版。agent 構成が Codex と違う）
- agent 定義は `.claude/agents/*.md`（AGENTS.md の `.codex/agents/*.toml` に対応）
- `memory/` は Codex と共有する

### エージェント運用

- **既定はシングルエージェント**。ユーザーの指定がない限り skill `solo-dev-cycle` で 計画 → テスト → 実装 → リファクタ → ドキュメント を自分で回す
- 「エージェントチームで進めて」「並列で」と明示されたときだけ skill `agent-team-startup` に従い、自分が `orchestrator` の役割（統括・委譲・レビュー裁定・合流検証。`src/` `tests/` は下位 agent に任せる）を担う
- Claude 側の agent 構成：`implementation-planner` →（任意 `spec-test-author`）→ `plan-implementer` → `code-reviewer` ⇄ `code-simplifier`。Codex の `docs-keeper` は Claude では `code-simplifier` に統合済み（docstring / README 同期を兼務）
- 文書改善用に `doc-teacher`（執筆・出題・採点）と `doc-student`（解答のみ、`model: haiku`・`tools: Read`）を置く。skill `doc-teacher-student` から使う
- `doc-student` を除く全 agent が `model: inherit`。速度・コスト・深さは `effort` で差別化する（`code-reviewer` は high、他は medium。セッション既定は `settings.json` の `effortLevel: medium`）
- **ユーザーに質問できるのはメインエージェントだけ**。サブエージェントは `AskUserQuestion` を持たないため、質問は報告に含めて返しメインが中継する

## コンテキスト管理 (compact)

長いセッションの context 圧縮 (compact) で判断構造が失われる事故を防ぐ仕組みを `.claude/` に組み込んである。詳細は [compact-prep skill](.claude/skills/compact-prep/SKILL.md)。

- **60% 通知。** context 使用率が 60% を超えたと通知されたら、作業の区切りで `/compact-prep` → `/compact` の順に実行する。通知の仕組み: statusLine (`.claude/scripts/statusline.sh`) が使用率を毎ターン算出して閾値超で警告 marker を書き、`UserPromptSubmit` hook がそれを検出して促す。閾値 60% は 1M context 前提の設定
- **`/compact-prep`。** `/compact` 直前に実行する skill。要約に残りにくい判断構造 (採用/却下した案・現在フェーズ・委譲したサブエージェント) を `${TMPDIR:-/tmp}/claude-compact-state/<session_id>.md` へ退避する
- **圧縮後の復旧。** `PostCompact` hook が圧縮を marker で記録し、次の `UserPromptSubmit` hook が state file・TaskList・CLAUDE.md の決定事項を読み戻すよう指示する。圧縮サマリーの next step は仮説として扱う
- hook / statusLine は `bash` と `python3` だけに依存する。配線は `.claude/settings.json` の `statusLine` / `hooks`。marker は `${TMPDIR:-/tmp}` 配下で session_id ごとに分離する
