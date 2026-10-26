---
name: do-on-worktree
description: 進行中の別タスクを止めずに、main から分岐した worktree で新しいタスクを裏で並行して進める手順。現在のブランチで別作業が進行中なのに独立した変更を依頼されたとき、「worktree で」「裏で進めて」と言われたときに参照する。
---

# 別タスク進行中に worktree で裏作業を進める

現在のブランチで別タスクが進行中のまま、それとは独立した変更を `main` から分岐して並行で進める。
進行中の作業には一切手を加えず、新しいブランチと worktree で実装する。この手順は PR を出すところまでを扱う。

関連: skill [github-pr](../github-pr/SKILL.md)。

## いつ使うか

- 現在のブランチで別タスクが進行中（未コミット変更やレビュー待ちのコミットがある）
- それとは無関係・独立した変更を新たに依頼された
- ユーザーが「worktree で」「裏で進めて」と明示した

進行中タスクの続きや関連する変更なら、worktree に分けず同じブランチで続ける。判断に迷ったら独立性をユーザーに確認する。

## 手順

### 1. 状況確認

```bash
git branch --show-current   # 進行中タスクのブランチ
git status --short          # 進行中の未コミット変更
git worktree list
```

### 2. worktree を作る

ブランチ名は CLAUDE.md の `<種別>/<日付>/<内容>` 規約に従う。

`EnterWorktree` ツールを使う。`worktree.baseRef` の既定 `fresh` により origin の `main` から分岐し、`.claude/worktrees/<name>/` に作られる。自動で付くブランチ名が規約と食い違えば `git branch -m <旧> <新>` で直す。

既存の worktree や branch を削除・上書きしない。

### 3. 作業する

依頼された変更を worktree 内で実装する。CLAUDE.md の開発原則・Git 運用に従う。
shell で `cd` した先は次の呼び出しに持ち越されることがある。git は `git -C <worktree のパス>` で対象を明示し、他の worktree のブランチを切り替えない。
`.claude/` 配下を触る場合は skill `edit-dot-claude` の手順（/tmp 経由）を使う。

### 4. 検証とコミット

`make check` を通してからコミットする。コミットは CLAUDE.md の規約（`<種別>(<スコープ>): <内容>`、1 コミット 1 関心事）に従う。

### 5. PR を出す

ユーザーが PR を依頼したら、skill [github-pr](../github-pr/SKILL.md) の手順で push し、`main` への PR を作成する。

### 6. worktree を残して戻る

worktree とブランチは残す。後でユーザーが続きやマージを判断する。

- ユーザーが「戻って」「worktree を抜けて」と言ったときに `ExitWorktree` を `action: "keep"` で呼ぶ。能動的には呼ばず、`remove` も使わない
- 削除はユーザーの明示がある場合だけ行う。未マージ変更を含む worktree に `--force` を使わない

## 注意点

- 進行中タスクのブランチには commit しない
- `main` へ直接 commit / merge しない。PR の作成までにとどめる
