---
name: git -C を使わない
description: git -C オプションは使用禁止。カレントディレクトリから直接 git コマンドを実行する
type: feedback
---

`git -C` オプションは使わない。

**Why:** ユーザーが明確に不要と判断。deny リストにも追加済み。
**How to apply:** 別ディレクトリのリポジトリを操作する必要がある場合でも `git -C` は使わず、別の方法を取る。
