---
name: migrate-claude
description: 'Claude Code project assetsをCodex向けrepo assetsへ移行・同期する。設定由来のCodex rules再生成に加え、.claude/skills、.claude/agents、CLAUDE.md、関連ドキュメントをCodex形式へ判断を伴って移植するときに使う。Triggers: ClaudeからCodexへ移行, migrate-claude, migrate-codex, Codex skillに同期, .claudeを.agents/.codexへ反映.'
---

# Claude Assets を Codex へ移行する

Claude 側の資産を参照し、Codex が読む repo-managed assets を保守する。
形式的に変換できる settings だけを script に任せ、Skill、Agent、文章ガイダンスは
内容を読んで Codex 向けに移植する。

## 責務分担

- `make migrate-codex`: `.claude/settings.json` と、存在する場合は
    `.claude/settings.container.json` から `.codex/rules/default.rules` を再生成する
- `make migrate-codex-check`: rules が Claude settings と同期しているか確認する
- この Skill: `.claude/skills/`、`.claude/agents/*.md`、`CLAUDE.md` の知見を
    `.agents/skills/`、`.codex/agents/*.toml`、`AGENTS.md` へ移す

`.claude/settings.local.json` は個人設定かつ Git 管理外なので生成元に含めない。

## 手順

1. `make migrate-codex-check` を実行し、Codex rules の stale 状態を確認する。
2. stale なら `make migrate-codex` を実行し、生成された
    `.codex/rules/default.rules` の diff を確認する。
3. Claude Skill / Agent を移す場合は、元ファイルと既存 Codex 側ファイルを両方読む。
4. Codex でも意味があるプロジェクト固有の知見だけを手で反映する。
5. Claude 固有の tool 名、permission、worktree API、agent API は Codex の実機能に
    置き換える。対応機能がなければ移植しない。
6. `AGENTS.md` と Skill 内の参照パスを `.agents/skills/`、`.codex/agents/` に揃える。
7. validation とプロジェクト品質ゲートを実行する。

## 対応表

| Claude                                  | Codex                            |
| --------------------------------------- | -------------------------------- |
| `CLAUDE.md`                             | `AGENTS.md`                      |
| `.claude/skills/<name>/SKILL.md`        | `.agents/skills/<name>/SKILL.md` |
| `.claude/agents/<name>.md`              | `.codex/agents/<name>.toml`      |
| `.claude/settings*.json` の `Bash(...)` | `.codex/rules/default.rules`     |
| Claude 固有の個人設定                   | 移植しない                       |

## 判断基準

- 単純なパス・名称置換だけで意味が変わるものは script に入れない。
- 既存 Codex 資産にしかない情報を消さない。Claude 側を常に正と決め打ちしない。
- Codex 専用 Skill は `.agents/skills/` に置き、`.claude/skills/` へ逆同期しない。
- `.codex/config.toml` の project policy は手で管理し、settings generator では触らない。
- `edit-dot-claude` のような Claude の permission 回避だけを目的とする Skill は移植しない。
- Skill の frontmatter は `name` と `description` のみにする。
- Custom agent は役割境界と書き込み範囲を TOML の `developer_instructions` に明記する。

## 検証

```bash
make migrate-codex-check
python /home/gop/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .agents/skills/migrate-claude
make format
make type
make test-no-hardware
```

`.agents/skills/` と `.codex/` が sandbox 上で読み取り専用の場合は、`/tmp` に編集用
コピーを作り、内容確認後に承認付き command で配置する。
