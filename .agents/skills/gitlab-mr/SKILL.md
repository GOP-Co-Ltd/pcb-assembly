---
name: gitlab-mr
description: 現在のブランチを GitLab に push し、glab で対象ブランチ（デフォルト main）へのマージリクエスト（MR）を作成する手順。「MR を出して」「マージリクエストを送って」「ブランチをプッシュして MR」と言われたときに参照する。
---

# GitLab へ push して MR を作成する

現在のブランチを remote に push し、`glab` CLI で対象ブランチへの MR を作成する。

引数で対象ブランチを指定できる。**指定がなければ `main`**。

## 手順

### 1. 事前チェック

```bash
git branch --show-current   # 現在ブランチの確認
git status --short          # 未コミット変更の有無
git log <target>..HEAD --oneline  # 対象ブランチに対する差分コミット
```

- 現在ブランチが `main` の場合は中止してユーザーに確認する（main から直接 MR は出さない）
- 未コミットの変更がある場合は中止し、先にコミットするかユーザーに確認する
- 対象ブランチに対する差分コミットが 0 件なら MR を出す意味がないので報告して中止する

既存 MR の重複を避ける：

```bash
glab mr list --source-branch "$(git branch --show-current)"
```

既に open な MR があれば新規作成せず、その MR の URL を報告する（push のみ行えば MR は自動更新される）。

### 2. push

```bash
git push -u origin "$(git branch --show-current)"
```

### 3. MR 作成

`glab mr create` は対話モードがデフォルトなので、必ずフラグで非対話にする：

```bash
glab mr create \
  --target-branch <target> \
  --title "<タイトル>" \
  --description "<説明>" \
  --yes
```

- **タイトル**：コミット規約と同じ `<種別>(<スコープ>): <内容>` 形式。コミットが 1 件ならそのメッセージをそのまま使う
- **説明**：変更の要約（何を・なぜ）。複数コミットなら主要な変更点を箇条書きにする
- 説明に改行を含める場合は `--description "$(cat <<'EOF' ... EOF)"` 形式を使う

### 4. 結果報告

作成された MR の URL をユーザーに報告する。

## 注意点

- **マージはしない**。`main` への merge はユーザー判断（AGENTS.md の Git 運用に従う）。`glab mr merge` は実行しない
- 検証（`make format && make type && make test`）が未実施の変更を含む場合は、MR 作成前に実行する
- push が reject された場合（remote が先行）は `git pull --rebase` の要否をユーザーに確認する。`--force` 系は使わない
