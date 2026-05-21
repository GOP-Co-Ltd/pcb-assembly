# plan-implementer の中間メモ

このフォルダには `plan-implementer` agent が実装中・実装後に残すノートを置く。

## ファイル形式

ファイル名: `<task-slug>.md`（並列時は `<task-slug>-<instance>.md`）

```markdown
# <タスク名>

## 計画外の判断ログ
（計画書から逸脱した場合、その理由と内容）

## 他implementerへのIF変更通知（並列時）
（並列で実装している他の implementer に伝えるべきインターフェース変更）

## 既知の制約・残課題
（次フェーズに引き継ぐ必要のあるもの）

## 検証結果
- make format: pass/fail
- make type: pass/fail
- make test: pass/fail
```

## 読み手

- `code-simplifier` が次に簡素化する際の前提として読む
- 並列 implementer 同士が IF 変更を共有するために読む
- `docs-keeper` が「何が変わったか」を把握するために読む
