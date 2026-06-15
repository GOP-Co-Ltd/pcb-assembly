---
name: do-on-worktree
description: 進行中の作業を汚さず、mainから分岐したgit worktreeで独立タスクを進める手順。ユーザーがworktreeで、裏で、別作業として進めるよう明示したときに使う。
---

# 独立タスクを Worktree で進める

ユーザーが worktree 利用を明示した場合に、現在の作業を保持したまま別ブランチで
作業する。Codex 専用 API には依存せず、標準の `git worktree` を使う。

## 1. 状況確認

```bash
git branch --show-current
git status --short
git worktree list
```

今回の依頼が進行中タスクから独立していることを確認する。関連する変更なら同じ
作業場所で続ける。

## 2. Worktree 作成

branch は `AGENTS.md` の `<種別>/<日付>/<内容>` 規約に従う。作業場所は repository
を汚さない `/tmp/pcb-assembly-worktrees/<slug>` を既定とする。

```bash
git fetch origin main
git worktree add -b <branch> /tmp/pcb-assembly-worktrees/<slug> origin/main
```

既に branch がある場合は `-b` を使わず、その branch を指定する。既存 worktree や
branch を削除・上書きしない。

## 3. 作業と検証

以降の command は worktree を `workdir` に指定して実行する。元 repository の未追跡
ファイルや未コミット変更には触れない。

```bash
make format
make type
make test-no-hardware
```

ハードウェア変更は利用可能な実機に応じて対象 test も実行する。

## 4. Commit と MR

検証後に worktree 側 branch へ commit する。ユーザーが MR を依頼した場合だけ
`gitlab-mr` Skill に従って push と MR 作成を行う。

## 5. 後片付け

worktree と branch の削除はユーザーの明示がある場合だけ行う。未マージ変更を含む
worktree に `--force` を使わない。
