# PCBアセンブリ プロジェクトメモリ

Claude Code とユーザーの対話で確立された規約・好み、およびマルチエージェント時の中間メモを記録する。
各ファイル先頭の frontmatter で description を確認し、関連タスクで参照する。

## フィードバック（規約・好み）

- [try-catchより戻り値バリデーション](feedback_no_try_catch.md) — 入力バリデーションはNone返却パターンを好む、try-catch不要
- [git -C 使用禁止](feedback_no_git_c.md) — git -C オプションは使わない、deny済み
- [privateの直接テスト禁止](feedback_no_private_test.md) — `_` prefix の関数/メソッド/属性は直接テストせず公開API経由で検証する
- [テストはクラスにまとめる](feedback_test_class.md) — pytestテストは関数ではなく `class TestXxx` 形式に集約する

## エージェント間共有メモリ

`agents/` 配下に各エージェント専用のフォルダを持つ。各 agent は自身のフォルダにのみ書き込む。

- 詳細は [agents/README.md](agents/README.md)

## 追加・運用ルール

- 新しいフィードバックは `feedback_<topic>.md` として追加し、このファイルにリンクを追加する
- frontmatter には `name`, `description`, `type: feedback` を必ず付ける
- 重複・陳腐化したメモリは速やかに更新または削除する
