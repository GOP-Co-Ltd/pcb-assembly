---
name: 和欧文間の半角空白は必須
description: 日本語の文書では日本語と英数字（英単語・略語・数値）の間に半角空白を 1 つ入れる。yomiyasu の「空白を消す」規則はこのリポジトリでは反転済み
type: feedback
---

日本語と英数字の間には半角空白を 1 つ入れる（例: `Klipper で`、`Python 3.12 以上`）。

**Why:** ユーザーの明示的な規約（2026-10-01）。upstream の yomiyasu skill は空白を消す方針だが、このリポジトリでは採らない。

**How to apply:**

- 文書・docstring・コメント・PR 説明を書くときは常にこの規約に従う
- 読点・句点・括弧の直後には空白を入れず、空白を 2 つ以上連続させない
- `.claude/skills/yomiyasu/` と `.agents/skills/yomiyasu/` は SKILL.md・`references/gemini-syntax.md` 原則 11・lint を反転・削除済み。upstream を取り込み直すときはこの変更を当て直す
