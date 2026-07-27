# code-simplifier の中間メモ

このフォルダには `code-simplifier` agent が簡素化・ドキュメント同期の際に残すノートを置く。

`docs-keeper` の廃止に伴い、docstring / README の追従もこの agent の担当になった。

## ファイル形式

ファイル名: `<task-slug>.md`

```markdown
# <タスク名>

## 簡素化した内部実装
（変更した internal、削除/追加した private、抽象化解消など）

## 公開IF維持の確認
（公開インターフェースが不変であることの根拠：テスト通過、シグネチャ一致など）

## 同期したドキュメント
- README.md: （差分の意図）
- docstring: （対象シンボルと修正内容）
- 変更不要と判断したもの: （理由）

## 簡素化できなかった部分・理由
（敢えて残した複雑さがあれば、その判断理由）

## 検証結果
- make format: pass/fail
- make type: pass/fail
- make test-no-hardware: pass/fail
（WebUI 変更時は make test-e2e も）
```

## 読み手

- `code-reviewer` が再レビュー時に変更意図を把握するために読む
- 後続の `plan-implementer`（リワークが必要な場合）が前提として参照
