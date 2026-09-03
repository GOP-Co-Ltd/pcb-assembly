---
name: do-on-worktree
description: 進行中の別タスクを止めずに、main から分岐した worktree で新しいタスクを裏で並行して進める手順。現在のブランチで別作業が進行中なのに独立した変更を依頼されたとき、「worktree で」「裏で進めて」と言われたときに参照する。
---

# 別タスク進行中に worktree で裏作業を進める

現在のブランチで別タスクが進行中のまま、それとは独立した変更を `main` から分岐して並行で進めるための手順。進行中の作業を一切汚さずに、新しいブランチ・worktree で実装し、MR を出すところまでを担う。

関連: skill [gitlab-mr](../gitlab-mr/SKILL.md)、skill [edit-dot-claude](../edit-dot-claude/SKILL.md)。

## いつ使うか

- 現在のブランチで別タスクが進行中（未コミット変更やレビュー待ちのコミットがある）
- それとは無関係・独立した変更を新たに依頼された
- ユーザーが「worktree で」「裏で進めて」と明示した

逆に、進行中タスクの続き・関連変更なら worktree に分ける必要はない（同じブランチで続ける）。判断に迷ったら独立性をユーザーに確認する。

## 手順

### 1. 状況確認

```bash
git branch --show-current   # 進行中タスクのブランチ
git status --short          # 進行中の未コミット変更
```

進行中タスクと今回の依頼が本当に独立しているか確認する。関連していれば worktree にせず同ブランチで続ける。

### 2. worktree を作る

`EnterWorktree` ツールを使う（`name` には内容が分かる短い名前を付ける。例: `chore/<日付>/<内容>`）。

- `worktree.baseRef` のデフォルト `fresh` により **origin の既定ブランチ（`main`）から分岐**する。進行中ブランチの変更は持ち込まれない
- セッションの作業ディレクトリが worktree（`.claude/worktrees/<name>/`）に切り替わる。進行中タスクのファイルには触れない
- 作成されるブランチ名は `name` から自動生成され（`/` が `+` に変換される等）規約と食い違うことがある。必要なら `git branch -m <旧> <新>` で AGENTS.md の `<種別>/<日付>/<内容>` 形式に直す

### 3. 作業する

依頼された変更を worktree 内で実装する。AGENTS.md の開発原則・Git 運用に従う。
`.claude/` 配下を触る場合は skill [edit-dot-claude](../edit-dot-claude/SKILL.md) の手順（/tmp 経由）を使う。

### 4. 検証

変更内容に応じて検証する（コード変更なら `make format && make type && make test-no-hardware`）。**検証通過前にはコミットしない**。

### 5. コミット

AGENTS.md のコミット規約（`<種別>(<スコープ>): <内容>`、1 コミット 1 関心事）に従う。

### 6. MR を出す

skill [gitlab-mr](../gitlab-mr/SKILL.md) の手順で push し、`main` への MR を作成する。

### 7. worktree を残して戻る

作業が一区切りし、ユーザーが元のタスクに戻る合図をしたら `ExitWorktree` を `action: "keep"` で抜け、元の（進行中タスクの）ディレクトリに戻る。worktree とブランチはディスクに残るので、後でユーザーが続きやマージを判断できる。

- `ExitWorktree` は能動的に呼ばない。ユーザーが「戻って」「worktree を抜けて」と言ったときに実行する
- `remove` は使わない（未マージの作業を消さないため）。後始末はユーザー判断

## 注意点

- 進行中タスクのブランチには絶対に commit しない
- `main` へ直接 commit / merge しない（AGENTS.md の Git 運用）。MR 止まりにする
- worktree は `.claude/worktrees/<name>/` に作られ、元タスクのファイルとは別物
