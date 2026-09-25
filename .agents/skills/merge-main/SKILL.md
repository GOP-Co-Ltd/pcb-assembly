---
name: merge-main
description: 作業の最後に PR を出す直前で、最新の main を作業ブランチに取り込み（merge）コンフリクトを解消してから PR を出すための手順。「PR を出す前に main を最新化」「main をマージ」「main に追従」「コンフリクト解決」「merge main」と言われたとき、または main が進んだ状態で PR を出す前に参照する。
---

# PR を出す前に最新の main を取り込む

作業の最後に PR を出す **直前** で実行する手順。`main` をリモート最新に更新し、自分の作業ブランチに取り込んで（merge）、conflict を解消してから PR を立てる。これにより PR が最新の base に対して clean に diff する。

AGENTS.md「Git 運用」に従い、**`main` への直接 commit / push はしない**。取り込みは作業ブランチ側で行う。PR 作成自体は skill [github-pr](../github-pr/SKILL.md) を参照。

**rebase ではなく merge** で取り込む（理由は末尾「rebase を使いたくなったら」）。

## 前提チェック

取り込みの前に作業ブランチが clean であること。未コミットの変更があると merge が止まる。

```bash
git status --short            # 出力が空であること (clean)
git branch --show-current     # main でないこと (作業ブランチ上にいる)
```

- 未コミットの変更があるなら **先に commit する**（1 コミット 1 関心事）。中途半端なら `git stash` で退避し、merge 後に `git stash pop`
- `main` ブランチ上にいたら誤り。作業ブランチに `git switch` する

## 手順

### 1. リモート最新を取得

```bash
git fetch origin main
```

`git pull` ではなく `fetch` を使う（ローカル `main` を介さず `origin/main` を直接 merge 対象にする）。ローカル `main` ブランチの更新は不要。

### 2. main が進んでいるか確認

```bash
git log HEAD..origin/main --oneline    # 自ブランチに無い main 側の新規 commit
```

- **出力が空** → main は進んでいない。取り込み不要。そのまま PR 作成へ
- **commit が並ぶ** → main が進んでいる。次の merge へ

### 3. origin/main を作業ブランチに merge

```bash
git merge origin/main
```

- conflict 無し → merge commit が作られる（または fast-forward）。手順 5 へ
- conflict 発生 → 手順 4 へ

merge commit message はデフォルト（`Merge remote-tracking branch 'origin/main' into <branch>`）でよい。編集が必要なら理由を一言添える。

### 4. conflict 解消

```bash
git status                              # Unmerged paths を確認
git diff --name-only --diff-filter=U    # conflict した file 一覧
```

conflict file ごとに次を行う。

1. `<<<<<<<` / `=======` / `>>>>>>>` マーカーの両側を読み、**両者の意図を保持して**解消する。main 側の変更も自分の変更も捨てない。`--ours` / `--theirs` で機械的に片方を採用しない
2. 判断が割れる conflict（両方が同じ関数を別意図で書き換えた等）は解消せずに止め、**何が衝突しているか名指しでユーザーに確認する**（AGENTS.md 開発原則 1）
3. 解消した file を stage する: `git add <file>`

全 file を解消したら `git merge --continue`（または `git commit`）。

- 中断したくなったら `git merge --abort` で merge 前に戻せる

### 5. 取り込み後の検証

main を取り込んだ結果コードが壊れていないか確認する。**全 green** であることが PR の前提（AGENTS.md「Git 運用」の標準フロー）。

```bash
make format && make type && make test
```

- test が落ちたら、main 側の変更と自分の変更の **意味的な衝突**（テキスト conflict は無かったが論理が壊れた）を疑う。修正して再度 green にする
- ハードウェアテスト（`@mark_hardware`）の扱いは普段どおり（実機が無ければ `make test-no-hardware`。詳細は skill [hardware-test](../hardware-test/SKILL.md)）

### 6. push して PR 作成

```bash
git push                       # 既に upstream があれば引数不要
```

以降の push / PR 作成は skill [github-pr](../github-pr/SKILL.md) の `gh pr create` 手順に従う。

## やってはいけないこと

- **`main` ブランチに直接 commit / push しない**。取り込みは作業ブランチ側のみ
- conflict を `git checkout --theirs .` 等で **一括上書きしない**（意図しない握り潰しの温床）
- conflict を残したまま `git add` / commit しない（`<<<<<<<` マーカーが混入する）
- 取り込み後に検証を省略しない。テキスト conflict が無くても論理は壊れうる
- `git push --force` しない。merge による取り込みは履歴を書き換えないので force は不要

## rebase を使いたくなったら

このスキルは **merge を既定** とする。`git pull --rebase` は自ブランチの commit hash を書き換え、push 済みブランチでは `--force-with-lease` が必要になる（AGENTS.md / skill github-pr の「`--force` 系は使わない」と衝突する）。共有・レビュー中のブランチでは履歴の安定性を優先して **merge** を使う。rebase が必要な特殊事情があるなら、その理由をユーザーに確認してから行う。

## 関連参照

- skill [github-pr](../github-pr/SKILL.md) — push / `gh pr create` / push reject 系トラブルシュート
- skill [do-on-worktree](../do-on-worktree/SKILL.md) — worktree で裏作業を進める手順
- [AGENTS.md](../../../AGENTS.md) — 「Git 運用」節
