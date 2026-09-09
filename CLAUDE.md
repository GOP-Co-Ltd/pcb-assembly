# CLAUDE.md

プロジェクト共通のガイダンスは [AGENTS.md](AGENTS.md) が正典。Claude Code はそれを丸ごと読み込み、ここには Claude Code 固有の差分だけを書く。

@AGENTS.md

## Claude Code 固有の差分

AGENTS.md の記述を以下のとおり読み替える。

### 検証コマンド

- Claude が使う検証は `make format && make type && make test-no-hardware`。標準フローの `make test` はこれに読み替える
- `make test` / `make run` / `pytest -m hardware` は実機が動くため実行しない。`.claude/settings.json` の deny と PreToolUse hook（`.claude/hooks/pretooluse-block-hardware-tests.py`）で機構的に禁止済み。実機確認はユーザーが行う
- `pytest` を直接叩くときは対象パスに関係なく `-m "not hardware"` を付ける
- GPU 学習ワークステーション（pcbnew / picamera2 が無い）では `make test-no-hardware` が collect できない。`src/ml/` と `paste_volume` の作業は `docker/` のコンテナ内で行い、検証は `make ml-docker-check`（format → 型検査 → `tests/ml` と `tests/pcbasm/pasting/paste_volume`）を使う。装置ドメイン側のテストはユーザーが実機側で回す
- コンテナは常駐させる。`make ml-docker-up` は idempotent で、各 `ml-docker-*` target が依存に持つ。`docker compose run --rm` を毎回叩かない

### 資産の置き場所

- Skill は `.claude/skills/<name>/SKILL.md`（AGENTS.md の `.agents/skills/` に対応。同名 skill は同じ内容の Claude 向け版）
- Claude 専用 skill：`solo-dev-cycle`（単独開発フロー）、`edit-dot-claude`（`.claude/` 編集時の permission prompt 抑制）、`compact-prep`（下記）
- agent 定義は `.claude/agents/*.md`（AGENTS.md の `.codex/agents/*.toml` に対応）
- `memory/` は Codex と共有する

### エージェント運用

- **既定はシングルエージェント**。ユーザーの指定がない限り skill `solo-dev-cycle` で 計画 → テスト → 実装 → リファクタ → ドキュメント を自分で回す
- 「エージェントチームで進めて」「並列で」と明示されたときだけ skill `agent-team-startup` に従い、自分が `orchestrator` の役割（統括・委譲・レビュー裁定・合流検証。`src/` `tests/` は下位 agent に任せる）を担う
- Claude 側の agent 構成：`implementation-planner` →（任意 `spec-test-author`）→ `plan-implementer` → `code-reviewer` ⇄ `code-simplifier`。Codex の `docs-keeper` は Claude では `code-simplifier` に統合済み（docstring / README 同期を兼務）
- 全 agent が `model: inherit`。速度・コスト・深さは `effort` で差別化する（`code-reviewer` xhigh、planner / implementer high、他 medium）
- **ユーザーに質問できるのはメインエージェントだけ**。サブエージェントは `AskUserQuestion` を持たないため、質問は報告に含めて返しメインが中継する

## コンテキスト管理 (compact)

長いセッションの context 圧縮 (compact) で判断構造が失われる事故を防ぐ仕組みを `.claude/` に組み込んである。詳細は [compact-prep skill](.claude/skills/compact-prep/SKILL.md)。

- **60% 通知。** statusLine (`.claude/scripts/statusline.sh`) が context 使用率を毎ターン算出し、閾値 (既定 60%) を超えると警告 marker を書く。`UserPromptSubmit` hook がそれを検出し、区切りで `/compact-prep` → `/compact` を促す。閾値 60% は 1M context 前提の設定
- **`/compact-prep`。** `/compact` 直前に実行する skill。要約に残りにくい判断構造 (採用/却下した案・現在フェーズ・委譲したサブエージェント) を `${TMPDIR:-/tmp}/claude-compact-state/<session_id>.md` へ退避する
- **圧縮後の復旧。** `PostCompact` hook が圧縮を marker で記録し、次の `UserPromptSubmit` hook が state file・TaskList・AGENTS.md の決定事項を読み戻すよう指示する。圧縮サマリーの next step は仮説として扱う
- hook / statusLine は `python3` のみに依存する。配線は `.claude/settings.json` の `statusLine` / `hooks`。marker は `${TMPDIR:-/tmp}` 配下で session_id ごとに分離する
