# code-reviewer の中間メモ

このフォルダには `code-reviewer` agent がレビュー結果を残す。

## ファイル形式

ファイル名: `<task-slug>.md`（並列レビュー時は `<task-slug>-<instance>.md`）

```markdown
# <タスク名> レビュー

## verdict: approve | request-changes

## must-fix
（項目ごとに: 対象ファイル:行、問題、根拠となる仕様箇所または再現手順）

## should-fix
（規約逸脱・構造改善。code-simplifier で対応可能なもの）

## nit
（任意）

## 検証結果
- make format: pass/fail
- make type: pass/fail
- make test-no-hardware: pass/fail
```

## 読み手

- `orchestrator` が裁定（差し戻し先の決定・誤検出の却下）に使う
- `plan-implementer` が must-fix 対応の際に参照
- `code-simplifier` が should-fix 対応の際に参照
