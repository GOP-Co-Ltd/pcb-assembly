# AGENTS.md / CLAUDE.md 理解度テスト

## 対象
- ファイル: `AGENTS.md`、`CLAUDE.md`
- 読者と用途: このリポジトリで作業するコーディングエージェント（Codex / Claude Code）が、応答言語・開発原則・検証コマンド・Git 運用・エージェント運用を誤りなく守る。CLAUDE.md は AGENTS.md との差分だけを書く構成を保つ

## 評価用問題
### Q1 あなたは Claude Code である。実装を終え、commit する前に検証する。実行するコマンドを答えよ。
- 要点: `make format && make type && make test-no-hardware`（`make test` ではない）

### Q2 あなたは Claude Code である。`tests/web/api` 配下のテストだけを確かめたい。`uv run pytest tests/web/api` をそのまま実行してよいか。
- 要点: そのままでは実行しない／`-m "not hardware"` を付ける（対象パスに関係なく付ける）

### Q3 あなたは Codex である。実機が接続されていない環境で標準フローの検証をする。`make test` と `make test-no-hardware` のどちらを使うか。あわせて何をするか。
- 要点: `make test-no-hardware`／実機がない理由を明示する

### Q4 2026-10-02 に不具合修正の作業を始める。どのブランチから分岐し、ブランチ名をどの形式にするか。
- 要点: `main` から分岐／`fix/2026-10-02/<内容>`

### Q5 あなたは Claude Code である。ユーザーから「このバグを直して」とだけ頼まれた。進め方として何を使い、何を誰が行うか。
- 要点: シングルエージェント（skill `solo-dev-cycle`）／計画 → テスト → 実装 → リファクタ → ドキュメント を自分で回す

### Q6 あなたは Claude Code のサブエージェント（例: `code-simplifier`）である。仕様に不明点があり、ユーザーに確認したい。どうするか。
- 要点: 自分ではユーザーに質問しない（`AskUserQuestion` を持たない）／質問を報告に含めて返し、メインエージェントが中継する

### Q7 WebUI の画面で、API から受け取った複数の値の合計を表示したい。合計はどこで計算するか。
- 要点: サーバー側（計算ロジックは `pcbasm` に置き、API が合計を返す）／JS は受け取った値を表示するだけで計算しない

### Q8 ユーザーへの説明の中で、pytest のエラーメッセージを引用する。エラーメッセージを日本語に訳して書くか。
- 要点: 訳さない（原文のまま残す）

### Q9 実装を終えて検証したところ `make type` が失敗した。いったん commit してから型エラーを直してよいか。
- 要点: よくない／検証が通るまで commit しない

### Q10 あなたは Claude Code である。Claude 用の新しい skill を作る。ファイルをどのパスに置くか。
- 要点: `.claude/skills/<name>/SKILL.md`

### Q11 あなたは Claude Code である。context 使用率が 60% を超えたという通知が出た。何をするか。
- 要点: 作業の区切りで `/compact-prep` を実行する／その後 `/compact` を実行する（順序: compact-prep が先）

## 保留問題
### H1 あなたは Claude Code である。ユーザーから「エージェントチームで進めて」と言われた。自分はどの役割を担うか。`src/` のコードを自分で編集するか。
- 要点: `orchestrator` の役割（skill `agent-team-startup` に従う）／`src/` `tests/` は下位 agent に任せ、自分では編集しない

### H2 テストで private 属性（`_` prefix）の値を確かめたい。その属性を public に変えてよいか。
- 要点: よくない（テスト都合で public にしない）

### H3 Codex の `docs-keeper` が担う docstring / README の同期は、Claude Code ではどの agent が担うか。
- 要点: `code-simplifier`

## ラウンド記録
### R0 執筆（AGENTS.md 157 → 158 行、CLAUDE.md 40 → 40 行）
- AGENTS.md 冒頭: 読者を Codex だけから Codex / Claude Code に直し、Claude Code は CLAUDE.md の読み替えを優先すると明記（矛盾: CLAUDE.md が AGENTS.md を丸ごと読み込むため）
- AGENTS.md `make test` / `make run`: 実機テストを含むことを明記（欠落: 実機が動く危険が読み取れなかった）
- AGENTS.md ブランチ名: 例 `docs/2026-09-25/brushup-all` を追加（曖昧: `<日付>` の書式）
- CLAUDE.md effort: Opus 5.5 への変更経緯を削除（読者の作業に不要。経緯は auto-memory `project-opus-5-5-effort.md` にある）
- CLAUDE.md hook 依存: `python3` のみ → `bash` と `python3`（矛盾: hook / statusLine は bash スクリプト）
- 未解決（ユーザー判断待ち）: commit 種別 `feature` と実績 `feat`（314 件 vs 30 件）の食い違い、「日本語化しない」と 2026-09 の commit の約 6 割が日本語本文である食い違い

### R1（AGENTS.md 158 行、CLAUDE.md 40 行、変更なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1-Q6, Q8-Q10 | 正解 | - | - |
| Q7 | 不正解（「router または backend」で計算と回答。JS は計算しない点も欠落） | 曖昧: 「サーバーが算出」の「サーバー」に router が含まれると読めた | AGENTS.md WebUI 設計: 「`pcbasm` が算出し、backend API がそのまま返す」に言い換え |
| Q11 | 不正解（「作業の区切りで」が欠落） | 埋没: 取るべき行動が statusLine の仕組みの説明文の中に埋もれていた | CLAUDE.md 60% 通知: 行動を先頭の 1 文に出し、仕組みの説明を後ろに回した |

### R2（AGENTS.md 158 行、CLAUDE.md 40 行）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1-Q11 | 正解 | - | - |
- 別の teacher からの指摘への対応（矛盾）: CLAUDE.md「圧縮後の復旧」の「AGENTS.md の決定事項」を「CLAUDE.md の決定事項」に直した。hook `userpromptsubmit-compaction-recovery.sh` の 31・37 行目と compact-prep SKILL.md の 56 行目が CLAUDE.md と言っている
- 評価用が全問正解したので、保留問題 H1-H3 を出す

### 保留問題（汎化確認、文書は R2 のまま）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| H1 | 正解 | - | - |
| H2 | 正解（要点を直したうえで） | 問題不良: 問いは「変えてよいか」だけなのに、要点に代わりの方法（公開 IF でテストする）まで入れていた | 要点から代わりの方法を外した。文書は変えていない |
| H3 | 正解 | - | - |
- 汎化確認は 3/3 で終了。ループを完了する
