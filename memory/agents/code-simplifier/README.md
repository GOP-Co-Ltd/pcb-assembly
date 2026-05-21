# code-simplifier の中間メモ

このフォルダには `code-simplifier` agent が簡素化中・簡素化後に残すノートを置く。

## ファイル形式

ファイル名: `<task-slug>.md`

```markdown
# <タスク名>

## 簡素化した内部実装
（変更したinternal、削除/追加したprivate、抽象化解消など）

## 公開IF維持の確認
（公開インターフェースが不変であることの根拠：テスト通過、シグネチャ一致など）

## 簡素化できなかった部分・理由
（敢えて残した複雑さがあれば、その判断理由）

## 検証結果
- make format: pass/fail
- make type: pass/fail
- make test: pass/fail
```

## 読み手

- `docs-keeper` が docstring/README 整備の際に参照
- 後続の `plan-implementer`（リワークが必要な場合）が前提として参照
