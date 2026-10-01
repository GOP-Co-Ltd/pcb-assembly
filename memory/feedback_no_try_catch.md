---
name: try-catchより戻り値でのバリデーション
description: 入力バリデーションで try-catch を使わず None 返却パターンを好む
type: feedback
---

入力バリデーションに try-catch を使わず、バリデーション関数が None を返す方式を好む。

**Why:** try-catch は冗長で、シンプルな条件分岐で済む場合は不要。
**How to apply:** パース/バリデーション関数は無効入力時に None を返し、呼び出し側で `if result is None` でチェックする。例外はネットワークやハードウェアなど本当に例外的な状況に限定する。
