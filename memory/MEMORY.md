# PCB アセンブリ プロジェクトメモリ

Claude Code とユーザーの対話で確立された規約・好みと、技術選定の判断を記録する。
各ファイル先頭の frontmatter で description を確認し、関連タスクで参照する。

## 実機を動かす確認はユーザーが行う

Claude は実機（カメラ、Klipper 接続のステージ・サーボ・エアポンプ、GPIO）を動かさない。
実機での確認はユーザーが受け入れ試験（`docs/acceptance.md`）で行い、Claude は golden と fake を通すテストまでを担当する。
pytest は実機に触れない（`@mark_hardware` のテストは無く、テスト中の Klipper 接続は必ず失敗する）。

## フィードバック（規約・好み）

- [try-catch より戻り値バリデーション](feedback_no_try_catch.md) — 入力バリデーションは None 返却パターンを好む、try-catch 不要
- [バリデーションは値オブジェクトのメソッド](feedback_validation_method.md) — `validate_xxx(value)` ではなくクラスの `validate()` にする（戻り値は `str | None` のまま）
- [git -C の使いどころ](feedback_no_git_c.md) — 通常は `git -C` を使わずカレントディレクトリで実行する。worktree 作業では必要なので使ってよい
- [private の直接テスト禁止](feedback_no_private_test.md) — `_` prefix の関数/メソッド/属性は直接テストせず公開 API 経由で検証する
- [テストはクラスにまとめる](feedback_test_class.md) — pytest テストは関数ではなく `class TestXxx` 形式に集約する
- [和欧文間の半角空白は必須](feedback_jp_en_spacing.md) — 日本語と英数字の間に半角空白を 1 つ入れる。yomiyasu には規約を追記済み

## 技術選定の記録

- [rustuna を採らない判断](decision_rustuna.md) — 実測で 16 倍速いが我々のボトルネックではない。再検討する条件つき

## 追加・運用ルール

- 新しいフィードバックは `feedback_<topic>.md` として追加し、このファイルにリンクを追加する
- 技術選定の判断は `decision_<topic>.md` として追加する（`type: decision`）。採らなかった
    理由と、再検討する条件を必ず書く
- frontmatter には `name`, `description`, `type: feedback` を必ず付ける
- 重複・陳腐化したメモリは速やかに更新または削除する
- ここに書いた規約と `.claude/settings.json` の設定が食い違ったら、どちらかを直す（記述と実態を乖離させたままにしない）
