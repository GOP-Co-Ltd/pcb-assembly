# CLAUDE.md

このリポジトリで作業する Claude Code が常時参照するガイダンス。
詳細な手続きは `memory/` と `.claude/skills/` に置く。

## 応答言語

ユーザーへの応答は日本語で行う。

- 散文・説明・要約・確認・質問・計画は日本語で書く
- コード・コマンド・ファイルパス・識別子・ログ/エラーの引用など、
    原文を保つべきものは原語のまま残す
- コミットメッセージ・PR タイトル等は「Git 運用」規約に従い、
    日本語化しない
- 技術用語は無理に訳さず、自然な範囲でカタカナ/英語を併用してよい

## 開発原則

慎重さを速度に優先する。trivial なタスクでは判断で簡略化してよい。

### 1. 実装前に考える

- 仮定を明示し、不確かな点は確認する
- 複数の解釈やトレードオフがあれば表に出す
- より単純な解決策があれば提示する
- 不明瞭なまま実装を進めない

### 2. シンプルさを優先

- 要求されていない機能・柔軟性・抽象化を追加しない
- 起こり得ないシナリオ向けの処理を増やさない
- 問題を解く最小限のコードにする

### 3. 必要な範囲だけ変更

- 周辺コードをついでに整形・リファクタしない
- 既存スタイルと公開インターフェースを尊重する
- 自分の変更で生じた未使用コードだけを片付ける
- diff の各行をユーザー要求へ直接トレースできる状態にする

### 4. ゴール駆動

検証可能な成功条件を先に置き、その条件を満たすまで実装と検証を繰り返す。
複数ステップの作業では短い計画と各ステップの検証方法を示す。

## プロジェクト概要

PCB アセンブリ装置の制御コード。Raspberry Pi 5、Klipper、KiCAD を前提に、
Python 3.12+ で HAL、ビジョン処理、制御ロジック、3D 幾何計算、FastAPI WebUI
を実装する。

主要構成（層と依存方向は `docs/develop/architecture.md`、規則は `tests/test_architecture.py`）:

- `src/pcbasm/geometry/`: 座標変換、ポリゴン、経路、配置の計算
- `src/pcbasm/config/`: `machine.toml` のスキーマと読み込み、設定ページの項目
- `src/pcbasm/migrate/`: `machine.toml` の冪等な移行
- `src/pcbasm/board/`: pcbnew に依存しない基板データ（外形・部品・パッド・銅箔）と書き出し
- `src/pcbasm/kicad/`: KiCAD 読込と基板生成（pcbnew を import する唯一の場所）
- `src/pcbasm/vision/`: 画像処理、カメラ校正、特徴検出
- `src/pcbasm/hal/`: カメラ、Klipper、ステージ、ディスペンサー、音声と、その fake
- `src/pcbasm/machine/`: 装置の組み立て、G-code ジャーナル、操作パネルの命令、駐機、装置診断
- `src/pcbasm/align/`: 基板の位置合わせ
- `src/pcbasm/pasting/`: ペースト塗布の設定解決、経路、機械手順、塗布量校正
- `src/pcbasm/commissioning.py`: 立ち上げ手順の表と状況の判定
- `src/web/api/`: 機体ごとの backend WebAPI（FastAPI、port 8081）
- `src/web/ui/`: LAN に 1 つ立てる UI frontend（FastAPI、port 8080。ページ描画と
    `/m/{machine_id}/api/**` の backend 中継）
- `src/web/contract/`、`src/web/selfupdate/`: backend・frontend・自己更新の契約と、Git 追従の自己更新

ブラウザ操作 UI は上記 2 プロセスに分かれる。開発・運用の操作は WebUI のジョブとして
提供する。ホストの導入は `install.sh`（雛形は `deploy/`）で行い、`scripts/` には
旧版の自己更新向けの互換スクリプトを置く。

## 開発コマンド

- `make setup`: 開発環境セットアップ
- `make format`: pre-commit 実行
- `make type`: pyright 型チェック
- `make test`: E2E 以外の全テスト（pytest は実機に触れない。テスト中の Klipper 接続と systemctl は必ず失敗する）
- `make test-e2e`: WebUI E2E
- `make check`: format → type → test
- `make api`: backend WebAPI 起動（port 8081）
- `make ui`: UI frontend 起動（port 8080）
- `make dev`: Klipper もカメラも fake にした backend と frontend を同時に起動（隔離ポート 8099 / 8098 と一時ディレクトリ、手動・ブラウザ確認用）

## 不変の原則

### カプセル化

- 内部実装と `__init__` で設定する属性は原則 `_` prefix
- 外部から必要なものだけ public にする
- private 属性をテスト都合で public にしない

### テスト

- 公開インターフェースと観測可能な振る舞いをテストする
- 実データ・実リソースを優先し、モックを最小化する
- テストは `class TestXxx` 形式に集約する
- 3rd-party 表面や内部関数をモックしない。装置は `pcbasm.hal.fake` の fake を使う
- 実機での確認は受け入れ試験（`docs/acceptance.md`）で人が行う
- 詳細は `testing-strategy`、`refactor-conventions` Skill

### WebUI 設計

計算・ドメインロジックは `pcbasm`（`src/pcbasm/`）に集約し、`src/web/api/` の router、
`src/web/ui/` の page ハンドラ、JS はいずれも入出力変換・DOM 操作・表示更新に徹する。

- 解決済み値・派生値・集計は `pcbasm` が算出し、backend API がそのまま返す
- frontend の page ハンドラは backend の JSON（pydantic モデル）をテンプレートへ渡すだけ。
    装置の状態を持たず、backend が返した値を再計算しない
- 表示文字列（マシンの label など）の組み立てはサーバー側で行う
- JS は API レスポンスをそのまま表示へ流し、クライアント検証は UX 最小限にする
- ドメインルール、階層 override 解決、幾何計算を JS や router に複製しない
- サーバーが返す値を JS で再導出せず、編集後はサーバー応答または再取得で更新する
- 詳細は `webui-thin-wrapper`、実起動検証は `webui-e2e` Skill

## Git 運用

- `main` から `<種別>/<日付>/<内容>` で分岐する（例: `docs/2026-09-25/brushup-all`）
- 種別は `feature`, `fix`, `refactor`, `docs`, `chore`
- `main` に直接 commit しない。main への merge はユーザー判断
- commit は `<種別>(<スコープ>): <内容>`、1 commit 1 関心事
- 検証通過前に commit しない
- force push、`git reset --hard`、未確認の破壊的操作を行わない

標準フロー:

要件確認 → ブランチ作成 → 実装 → `make check`（テンプレート・JS に触れたら `make test-e2e` も）
→ commit。

## エージェント運用

- **既定はシングルエージェント**。ユーザーの指定がない限り skill `solo-dev-cycle` で 計画 → テスト → 実装 → リファクタ → ドキュメント を自分で回す
- 「エージェントチームで進めて」「並列で」と明示されたときだけ skill `agent-team-startup` に従い、自分が `orchestrator` の役割（統括・委譲・レビュー裁定・合流検証。`src/` `tests/` は下位 agent に任せる）を担う
- agent 定義は `.claude/agents/*.md`。構成は `implementation-planner` →（任意 `spec-test-author`）→ `plan-implementer` → `code-reviewer` ⇄ `code-simplifier`（docstring / README の同期を兼務）
- 文書改善用に `doc-teacher`（執筆・出題・採点）と `doc-student`（解答のみ、`model: haiku`・`tools: Read`）を置く。skill `doc-teacher-student` から使う
- `doc-student` を除く全 agent が `model: inherit`。速度・コスト・深さは `effort` で差別化する（`code-reviewer` は high、他は medium。セッション既定は `settings.json` の `effortLevel: medium`）
- **ユーザーに質問できるのはメインエージェントだけ**。サブエージェントは `AskUserQuestion` を持たないため、質問は報告に含めて返しメインが中継する
- 中間メモはファイルに残さず各 agent の報告で受け渡し、残すべき判断は PR 本文に書く。並列 agent は書き込み範囲を分離し、同じファイルを同時編集しない

## 参照先

- `README.md`: 文書の地図（導入 → 立ち上げ → 日常 → 保守、参照、開発、設計）
- `docs/develop/`: 開発の手引きとアーキテクチャ（機能 → コードの地図）
- `memory/MEMORY.md`: ユーザーとの対話で確立した規約・好み
- skill は `.claude/skills/<name>/SKILL.md` に置く
    - `testing-strategy`: テスト戦略
    - `refactor-conventions`: 実装・テスト規約
    - `webui-e2e`: WebUI E2E
    - `webui-thin-wrapper`: WebUI 薄ラッパー方針
    - `solo-dev-cycle`: 単独開発フロー
    - `agent-team-startup`: エージェントチーム運用
    - `maximize-parallels`: tool 並列化
    - `do-on-worktree`: 独立タスクの worktree 運用
    - `github-pr`: GitHub PR 作成
    - `merge-main`: PR 前の main 取り込み
    - `edit-dot-claude`: `.claude/` 編集時の permission prompt 抑制
    - `compact-prep`: compact 前の作業状態の退避（下記）
    - `doc-teacher-student`: 文書を haiku の理解度テストで改善するループ
    - `japanese`: 応答を日本語に切り替える
    - `yomiyasu`: AI 生成の日本語を読みやすく推敲する
        （[nanaism/yomiyasu](https://github.com/nanaism/yomiyasu) v2.1.3 `c09270b` の `skills/yomiyasu` を vendoring、MIT）。
        和欧文間の半角空白を必須とする本リポジトリの規約を、SKILL.md と `references/gemini-syntax.md` 原則 11 に追記している

## コンテキスト管理 (compact)

長いセッションの context 圧縮 (compact) で判断構造が失われる事故を防ぐ仕組みを `.claude/` に組み込んである。詳細は [compact-prep skill](.claude/skills/compact-prep/SKILL.md)。

- **60% 通知。** context 使用率が 60% を超えたと通知されたら、作業の区切りで `/compact-prep` → `/compact` の順に実行する。通知の仕組み: statusLine (`.claude/scripts/statusline.sh`) が使用率を毎ターン算出して閾値超で警告 marker を書き、`UserPromptSubmit` hook がそれを検出して促す。閾値 60% は 1M context 前提の設定
- **`/compact-prep`。** `/compact` 直前に実行する skill。要約に残りにくい判断構造 (採用/却下した案・現在フェーズ・委譲したサブエージェント) を `${TMPDIR:-/tmp}/claude-compact-state/<session_id>.md` へ退避する
- **圧縮後の復旧。** `PostCompact` hook が圧縮を marker で記録し、次の `UserPromptSubmit` hook が state file・TaskList・CLAUDE.md の決定事項を読み戻すよう指示する。圧縮サマリーの next step は仮説として扱う
- hook / statusLine は `bash` と `python3` だけに依存する。配線は `.claude/settings.json` の `statusLine` / `hooks`。marker は `${TMPDIR:-/tmp}` 配下で session_id ごとに分離する
