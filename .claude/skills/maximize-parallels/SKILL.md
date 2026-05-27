---
name: maximize-parallels
description: 独立な tool 呼び出しは 1 メッセージにまとめて並列発火する。並列化可能の判定基準（出力 → 入力の依存なし／共有 mutable state を同時に書かない／tool 固有の排他なし）、典型的に並列化すべきパターン（複数 file の Read、独立 Bash、複数角度の検索、disjoint なエージェント起動）、逐次必須の落とし穴（Read→Edit 連鎖、同 file への複数 Edit、cd を伴う Bash、依存する出力、ハードウェアリソースの競合）、着手前の判定手順（タスクの入出力を 1 行で書き出して依存グラフを引く）。複数タスクに着手する／複数 file を読む／複数エージェントを起動する／長めの bash 列を組む前に読む
---

# 並列化を最大化する

複数の tool 呼び出しを行うとき、論理的に独立なものは **1 メッセージに複数の tool_use ブロックを並べて発射** し、並列実行させる。これは速度・コスト・体感応答性の単純改善であり、迷ったら並列を選ぶ。

「論理的に可能」とは、並列化対象が **相互に依存していない** こと。具体的には以下の 3 つを全て満たすときに並列化できる。

## 並列化可能の判定基準

1. **出力 → 入力の依存がない**: 一方の stdout / 戻り値 / 副作用が他方の入力に使われない
2. **共有 mutable state を同時に書き換えない**: 同じ file への並列 Edit、同じ DB 行への並列 UPDATE、同じ branch への並列 checkout は不可
3. **tool 固有の排他がない**: `Bash` の cwd 切り替えのように session 内で副作用を残すものは並列禁止。`Read` / `Grep` / `Glob` は読み取り専用で常に安全

3 つすべて満たす → 1 メッセージに並べる。1 つでも引っ掛かる → 逐次。

## 並列化すべき典型パターン

- **複数 file の `Read`**: 何を読むかが事前に決まっているなら一気に並列で読む。1 件読んで「次は何を読むべきか」を考えるのは遅い
- **独立な `Bash`**: `git status` / `git diff` / `git log` のように互いを汚さない情報取得。`uv run pytest tests/A` と `uv run pytest tests/B` のように対象 file が disjoint なテストの並列実行も同じ
- **検索の発散**: `grep "foo"` / `grep "bar"` / `find -name '*.py'` のように複数の角度から同時に探す
- **複数 agent の起動**: 担当領域が disjoint な `plan-implementer × N`、独立モジュール毎の `code-simplifier × N` などは常に並列。詳細は skill [agent-team-startup](../agent-team-startup/SKILL.md) の「並列化の判断基準」セクション

## 並列化してはいけない (逐次必須) パターン

- **`Read` → `Edit` / `Write` 連鎖**: `Edit` / `Write` は事前 `Read` を要求する。同じ file の `Read` と `Edit` を並列に出すと後者が失敗する
- **同じ file への複数 `Edit` / `Write`**: 後続の `Edit` は前の `Edit` 適用後のテキストを `old_string` として参照するため、並列にすると 2 つ目以降の `old_string` が見つからない
- **`cd` を伴う `Bash`**: cwd は session 内で持続するので、並列に走らせると後続コマンドがどの cwd で動くか不定。代わりに各 Bash で絶対パスを使うか `cd dir && cmd` のように 1 Bash 内に閉じ込める
- **依存する出力**: `git rev-parse HEAD` の結果を次の `git show <sha>` に渡すような場合
- **同じ branch / worktree への破壊的操作**: `git checkout` / `git reset` / `git stash` を並列に走らせない
- **ハードウェアリソースの競合**: `@mark_hardware` テスト、`v4l2` カメラデバイス、Klipper のシリアル port、GPIO ピンなど、物理的に 1 つしかないリソースを掴むテスト / 操作は並列不可。同一カメラに対する `make test -m hardware` と `v4l2-ctl --stream` を同時実行するとデバイスがロックされる

## 実践手順 (複数タスクに着手するとき)

1. 着手するタスクを箇条書きで頭に並べる
2. 各タスクの **入力 (前提とする情報)** と **出力 (生み出す情報)** を 1 行ずつ書く
3. A の出力 → B の入力が無く、共有 mutable state を同時に書かないなら A と B は並列
4. 並列にできるグループを **単一メッセージ** にまとめて発射、依存があるところでだけ待つ
5. 結果が揃ってから次のグループを発射する

「全部逐次に並べてから後で並列化を検討する」は遅い。**最初から並列を前提に組む**。

## マルチエージェント運用との関係

エージェント運用レベルの並列化ルール (フェーズごとに `plan-implementer` を独立モジュール毎に並列、`code-simplifier` を独立モジュール毎に並列、など) は [CLAUDE.md](../../../CLAUDE.md) 「エージェントチーム」セクションおよび skill [agent-team-startup](../agent-team-startup/SKILL.md) に集約済み。この skill はその下層、**tool 呼び出しレベル** の話。両者は同じ「論理的に独立なら並列」の原則の異なる適用層であり、矛盾しない。
