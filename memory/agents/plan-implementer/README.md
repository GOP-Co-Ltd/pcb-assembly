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
- make test-no-hardware: pass/fail
```

## 読み手

- `code-reviewer` が diff と突き合わせて仕様準拠を判定するために読む
- `code-simplifier` が次に簡素化・ドキュメント同期する際の前提として読む
- 並列 implementer 同士が IF 変更を共有するために読む

## 注意

ノートは**書かれた時点の記録**であり、後続コミットで公開 IF・モジュール配置・エンドポイントが変わっていることがある。実装前に必ず現在のコードで確認する。「削除した」「新設した」という記述も、その後で復元／再削除されている場合がある。
