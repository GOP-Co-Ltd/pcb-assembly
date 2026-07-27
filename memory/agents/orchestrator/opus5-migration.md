# Claude Code 設定の Opus 5 向け migrate

計画書: `~/.claude/plans/opus-claude-opus-4-8-opus-5-migrate-vivid-pixel.md`
ブランチ: `chore/20260727/opus5-migration`

## 背景

設定は 2026-05-21 に Opus 4.7 向けに刷新され、最終形が 2026-07-22（`4f55d6a`）。Opus 5 のリリースは 2026-07-24 で、設定がモデルより 2 日古い状態だった。

## 委譲判断

| 判断 | 内容 |
| --- | --- |
| 委譲した | 初期調査 3 体（`.claude/` 構造 / `memory/`+CLAUDE.md / Opus 5 の Web 調査）。範囲が広く disjoint で、本体 context に載せられない量だったため |
| 委譲した | Claude Code の permission 照合セマンティクス確認 1 体（`claude-code-guide`）。公式ドキュメントの逐語が必要だったため |
| 委譲した | `memory/agents/` 棚卸し 3 体（118 ノート / 1.2 MB）。ディレクトリが disjoint で、本文を context に載せずに判定だけ返させる必要があったため |
| **委譲しなかった** | 実装計画の立案。全調査結果が手元にあり、Plan エージェントに渡し直すより自分で書くほうが速く正確だった |
| **委譲しなかった** | 設定ファイルの編集本体。`.claude/` と `CLAUDE.md` は `src/` `tests/` ではなく、orchestrator の担当範囲。かつ数十回の編集で完結する規模だった |

## 計画外の判断（理由付き）

1. **C-7 の修正 — `disallowedTools` から `Write` を除外**
   計画では `code-reviewer` / `implementation-planner` に `Edit, Write, NotebookEdit` を禁止する予定だったが、両者は `memory/agents/` にノートを Write する必要があり、出力機構が壊れる。`Edit, NotebookEdit` のみ禁止に変更（既存ファイルの改変は防ぎ、ノート作成は残す）。

2. **調査の誤検出を却下 — `Bash(make test:*)` は修正しない**
   サブエージェントが「`make test-no-hardware` まで deny に巻き込まれる」と報告したが、公式ドキュメントでは `:*` が単語境界を尊重するため `make test-no-hardware` にはマッチしない。現行ルールは意図どおりのため変更しなかった。

3. **計画外の実在バグを修正 — 検証コマンドを `make test-no-hardware` に統一**
   `settings.json` が `make test` を deny している一方、`plan-implementer` / `code-simplifier` / `refactor-conventions` が完了条件に `make test` を指定していた。agent が必ず permission denial に当たる状態だったため統一した。

4. **`git -C` を deny に追加**
   `memory/feedback_no_git_c.md` と `MEMORY.md` が「deny 済み」と書いていたが実際には未登録だった。規約として維持する意図が明確なため、記述を弱めるのではなく deny を追加して記述を真にした。`statusline.sh:28` が `git -C` を使っているが、スクリプト内部の記述であり Claude のコマンド発行ではないため影響しない。

5. **`agent-team-startup` を SKILL.md + reference.md に分割（E-2 を前倒し）**
   docs-keeper 廃止で同ファイルを書き換える必要があり、二度書き換えるより一度で済ませるほうが外科的だった。188 行 → 125 行 + reference.md 95 行。

## 未解決 — ユーザー判断待ち

- **`jq` が PATH に無い。** CLAUDE.md は「システムに導入済み」と書いていたが事実ではなかった。`statusline.sh` と 3 つの hook はすべて jq 依存かつ fail-open のため、**60% の context 警告と compact 後の復旧が無音で機能していない**可能性が高い。CLAUDE.md の記述は事実に合わせたが、対処（jq 導入 / スクリプトを python3 に書き換え / 現状維持）は未決。
- **陳腐化ノートの削除**。`memory/agents/README.md` の「保持・削除はユーザー判断（デフォルトは保持）」に従い、棚卸し結果の提示までで止める。

## 主要な設計判断

- **モデル階層をやめ、effort 一本で差別化**。全 agent `model: inherit`。`code-reviewer` xhigh / `implementation-planner`・`plan-implementer` high / `spec-test-author`・`code-simplifier` medium。orchestrator は frontmatter に effort を書かない（書くとユーザーの `/effort` を上書きしてしまうため）
- **`docs-keeper` 廃止 → `code-simplifier` に統合**。README / docstring 更新は数回のツール呼び出しで終わり、委譲オーバーヘッドが利得を上回るため
- **`code-reviewer` は残す**。実装者と別コンテキストの writer-verifier であり、Opus 5 が戒める「自己検証の重複」には当たらない
- **code-reviewer に全件報告を指示**。「重要度で自分でふるいにかけるな、確信度を添えて全部出せ、採否は orchestrator が裁定する」。Opus 5 は保守的フィルタを字義通り守って recall を落とすため
