# PCBアセンブリ プロジェクトメモリ

Claude Code / Codex とユーザーの対話で確立された規約・好み、およびマルチエージェント時の中間メモを記録する。
各ファイル先頭の frontmatter で description を確認し、関連タスクで参照する。

## No hardware test execution

**実機テストは Claude が実行しない。** `make test` と `@mark_hardware` を付けたテストは実機（カメラ、Klipper 接続のステージ・サーボ・エアポンプ、GPIO）を物理的に動作させるため、破損や事故につながりうる。

- Claude が使う検証コマンドは `make test-no-hardware` まで
- `make test` は `.claude/settings.json` の deny に登録済み
- 実機での確認はユーザーが行う。Claude はテストを書くところまでを担当する

## フィードバック（規約・好み）

- [try-catchより戻り値バリデーション](feedback_no_try_catch.md) — 入力バリデーションは None 返却パターンを好む、try-catch 不要
- [git -C 使用禁止](feedback_no_git_c.md) — `git -C` オプションは使わない（`settings.json` の deny に登録済み）
- [privateの直接テスト禁止](feedback_no_private_test.md) — `_` prefix の関数/メソッド/属性は直接テストせず公開 API 経由で検証する
- [テストはクラスにまとめる](feedback_test_class.md) — pytest テストは関数ではなく `class TestXxx` 形式に集約する

## エージェント間共有メモリ

`agents/` 配下に各エージェント専用のフォルダを持つ。各 agent は自身のフォルダにのみ書き込む。

- 詳細は [agents/README.md](agents/README.md)
- 現行エージェント: `orchestrator` / `implementation-planner` / `spec-test-author` / `plan-implementer` / `code-reviewer` / `code-simplifier`
- `agents/docs-keeper/` は廃止済みエージェントのノート（履歴として保持。責務は `code-simplifier` に統合）

## 追加・運用ルール

- 新しいフィードバックは `feedback_<topic>.md` として追加し、このファイルにリンクを追加する
- frontmatter には `name`, `description`, `type: feedback` を必ず付ける
- 重複・陳腐化したメモリは速やかに更新または削除する
- ここに書いた規約と `.claude/settings.json` の設定が食い違ったら、どちらかを直す（記述と実態を乖離させたままにしない）
