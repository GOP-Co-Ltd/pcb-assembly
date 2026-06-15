---
name: maximize-parallels
description: 独立なtool呼び出しをmulti_tool_use.parallelへまとめる判定基準と、依存・共有state・ハードウェア競合がある処理を直列化する手順。複数ファイルの読取、独立command、複数検索を行う前に使う。sub-agent並列化はユーザーが明示した場合に限る。
---

# Tool 呼び出しを並列化する

相互依存のない読み取りや検証を `multi_tool_use.parallel` で同時実行する。

## 並列化条件

次をすべて満たす処理だけを同じ parallel call にまとめる。

1. 一方の出力を他方の入力に使わない
2. 同じファイル、branch、process、DB、device を同時に変更しない
3. tool 固有の排他や順序制約がない

## 並列化する例

- 複数ファイルの `sed -n`、`git show`、`git diff`、`find`
- `git status`、`git log`、manifest 読み取りなど独立な調査
- 対象が disjoint な test、lint、静的解析
- 複数の検索語による read-only 調査

## 直列にする例

- 読み取り結果を見てから行う file edit
- 同じファイルへの複数 edit
- 生成後の artifact を入力にする check
- `git switch`、merge、commit など同一 worktree の状態変更
- 同じカメラ、Klipper、GPIO を使う実機操作
- dev server 起動後の readiness check

## Sub-agent

Sub-agent の起動は、ユーザーが agent 利用または並列作業を明示した場合だけ行う。
その場合も `agent-team-startup` Skill に従い、担当範囲が disjoint な sidecar task を
並列化する。即時 blocker や密結合な作業はメイン agent が担当する。
