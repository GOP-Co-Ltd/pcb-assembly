# spec-test-author の中間メモ

このフォルダには `spec-test-author` agent がテスト記述中・記述後に残すノートを置く。

## ファイル形式

ファイル名: `<task-slug>.md`（並列時は `<task-slug>-<instance>.md`）

```markdown
# <タスク名>

## 書いたテスト一覧
- tests/pcbasm/<path>/test_<x>.py::TestXxx::test_yyy — 観点（正常系/異常系/エッジ）
- ...

## 仕様根拠の対応表
（各テストが計画書のどの要件に対応するか）
- test_yyy → 計画書「正常系：A の時 B を返す」

## 期待される失敗（仕様 first の場合）
- 実装側で未実装のもの
- 実装が仕様に違反していると判明したもの

## 実装側に求める修正
（plan-implementer 向け：テストが正の前提で、本番コードをどう直すべきか）

## tests/helpers.py への追加
（追加した fake Impl とその理由。3rd-party モックは使っていないこと）

## 検証結果
- make format: pass/fail
- make test-no-hardware 実行結果（任意。仕様 first なら赤で正常）
```

## 読み手

- `plan-implementer` が必ず読む（テストを真として実装を進める起点）
- `code-reviewer` がテスト品質の判定に参照
- `code-simplifier` が公開振る舞いの契約として参照

## 書き込みルール

- このフォルダにのみ書く
- テストファイルは `tests/pcbasm/`（`src/pcbasm/` のミラー）または `tests/webui/`（`src/webui/` のミラー）に直接書く。実サーバー E2E は `tests/e2e/`（ノートにはパスを列挙）
- 本番コード（`src/pcbasm/` と `src/webui/`）は編集しない。実装を直すと「仕様に対するテスト」という立場が崩れ、実装のバグをテスト側で隠してしまうため
