---
name: try-catchより戻り値でのバリデーション
description: 入力バリデーションでtry-catchを使わずNone返却パターンを好む
type: feedback
---

入力バリデーションにtry-catchを使わず、バリデーション関数がNoneを返す方式を好む。

**Why:** try-catchは冗長で、シンプルな条件分岐で済む場合は不要。
**How to apply:** パース/バリデーション関数は無効入力時にNoneを返し、呼び出し側で`if result is None`でチェックする。例外はネットワークやハードウェアなど本当に例外的な状況に限定する。
