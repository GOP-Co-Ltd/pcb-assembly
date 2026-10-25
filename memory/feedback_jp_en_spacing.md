---
name: 和欧文間の半角空白は必須
description: 日本語の文書では日本語と英数字（英単語・略語・数値）の間に半角空白を 1 つ入れる。yomiyasu の「空白を消す」規則はこのリポジトリでは反転済み
type: feedback
---

日本語と英数字の間には半角空白を 1 つ入れる（例: `Klipper で`、`Python 3.12 以上`）。

**Why:** ユーザーの明示的な規約（2026-10-01）。upstream の yomiyasu skill は空白を原文どおり保つ方針で、規約を積極的には適用しない。

**How to apply:**

- 文書・docstring・コメント・PR 説明を書くときは常にこの規約に従う
- 読点・句点・括弧の直後には空白を入れず、空白を 2 つ以上連続させない
- `.agents/skills/yomiyasu/`（`.claude/skills/yomiyasu` はその symlink）は SKILL.md の「用途と表記の扱い」と `references/gemini-syntax.md` 原則 11 に規約を追記済み（v2.1.3 から upstream は空白を原文どおり保つ方針になり、lint の空白検知もなくなった）。upstream を取り込み直すときはこの追記を当て直す
