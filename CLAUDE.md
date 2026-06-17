# CLAUDE.md

Claude Code がこのリポジトリで作業する際に常時参照するガイダンス。
詳細な手続きは `memory/` と `.claude/skills/` にオフロードしている。

## 開発原則

LLM コーディングで陥りがちなミスを減らすための行動指針。**慎重さを速度に優先する**バイアスを置く。trivial なタスクでは判断で柔軟に運用してよい。

### 1. 実装前に考える

**仮定を勝手に置かない。混乱を隠さない。トレードオフを表に出す。**

- 仮定は明示的に述べる。不確かなら質問する
- 複数の解釈が成り立つなら全部提示する。黙って 1 つに決めない
- もっと単純な方法があるなら言う。正当な理由があれば押し返す
- 何かが不明瞭なら止まる。何が混乱の原因か名指しして質問する

### 2. シンプルさを優先

**問題を解く最小限のコード。投機的な実装はしない。**

- 頼まれていない機能は足さない
- 単発の用途しかないコードに抽象化は入れない
- 要求されていない「柔軟性」「設定可能性」は持ち込まない
- 起こり得ないシナリオに対するエラーハンドリングは書かない

自問する: 「これをシニアエンジニアが見たら過剰だと言うか?」Yes なら単純化する。

### 3. 外科的な変更

**触る必要があるものだけ触る。自分が散らかしたものだけ片付ける。**

- 周辺コード・コメント・整形を「ついでに改善」しない
- 壊れていないものをリファクタしない
- 自分なら違う書き方をするとしても既存スタイルに合わせる
- 無関係な dead code に気付いたら指摘する。勝手に消さない
- 自分の変更で生じた orphan (未使用 import / 変数 / 関数) は消す。元から dead だったコードは頼まれない限り消さない

判定基準: diff の全行が、ユーザーの要求から直接トレースできるか?

### 4. ゴール駆動の実行

**成功条件を定義する。検証できるまでループする。**

タスクを検証可能なゴールに変換する:

- 「バリデーションを足す」→「不正な入力に対するテストを書いて通す」
- 「バグを直す」→「再現するテストを書いて通す」
- 「X をリファクタする」→「変更前後でテストが通ることを確認する」

複数ステップのタスクでは短い計画を先に提示し、各ステップに検証チェックを添える。

## プロジェクト概要

PCB アセンブリ装置の制御コード。Raspberry Pi 5 + Klipper + KiCAD を前提とし、ハードウェア抽象化レイヤ（HAL）、ビジョン処理、制御ロジック、3D 幾何計算を Python 3.12+ で実装する。

主要モジュール構成（`src/pcbasm/`）:

- `hal/` — ハードウェア抽象化（カメラ、Klipper、プローブ、サーボ等）
- `vision/` — 画像処理・キャリブレーション・特徴検出
- `posctrl/` — Board/オフセットの位置合わせ共通制御（setup, tour, board/position/offset 調整）
- `pasting/` — ペースト塗布専用ロジック（applicator, calibration, fill_path, loading, probe, height 等）
- `pnp/` — Pick and Place 用の名前空間（将来用）
- `geometry/` — 3D 座標と幾何計算（Transform, HeightPlane, 軌跡生成等）
- `pcb/` — PCB 設計情報の抽象化（KiCAD 読込、配置管理）

このほか `src/webui/` にブラウザ操作 UI（FastAPI。仕様：`docs/webui/specification.md`）がある。

## 開発コマンド

- `make setup` — 開発環境のセットアップ
- `make test` — 全テスト実行（ハードウェアテスト含む）
- `make test-no-hardware` — ハードウェア以外のテスト実行
- `make format` — pre-commit フック（ruff, docformatter 等）
- `make type` — pyright 型チェック
- `make run` — `format` → `test` → `type` を順実行
- `make webui` / `make webui-dev` — WebUI サーバー起動（port 8080、dev は auto-reload）

## 不変の原則

### カプセル化

- クラスの内部実装の詳細・属性は基本的に private（`_` prefix）
- `__init__` で設定される属性は原則 private
- 外部から参照する必要がある属性のみ public

### テスト方針（要点）

- 公開インターフェースと振る舞いをテストする（内部実装はテストしない）
- 実データ・実オブジェクト優先、モック最小化
- テストは `class TestXxx` 形式に集約
- ハードウェアテストは `@mark_hardware` で分離
- 詳細：skill `refactor-conventions`, `hardware-test`

## Git 運用

### ブランチ

- `main` から `<種別>/<日付>/<内容>` で分岐（例：`refactor/20260430/height-plane`）
- 種別：`feature`, `fix`, `refactor`, `docs`, `chore`
- `main` に直接 commit しない。`main` への merge はユーザー判断

### コミットメッセージ

- `<種別>(<スコープ>): <内容>`
- 種別：`feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`
- 1 コミット 1 関心事

## 自走開発フロー

要件確認 → ブランチ作成 → 実装 → `make format && make type && make test` → コミット

- 検証通過前のコミットはしない
- 要件外の機能は実装しない
- 計画外の判断はコミットメッセージに理由を残す

## エージェントチーム

ユーザーから「エージェントチームで進めて」と指示があった場合、または中〜大規模変更時は skill `agent-team-startup` を参照。

標準サイクル：`implementation-planner` →（任意 `spec-test-author`）→ `plan-implementer` ⇄ `code-simplifier` → `docs-keeper`。各 agent の中間メモは `memory/agents/<agent-name>/<task>.md`。

`spec-test-author` は `tests/` 専用（`src/` は触らない）。仕様 first フローで `plan-implementer` と **並列実行可能**（公開 IF がシグネチャレベルで確定していることが前提）。

## 参照先マップ

### `memory/` — ユーザーとの対話で確立された規約・好み

- `memory/MEMORY.md` — インデックス
- `memory/feedback_*.md` — 個別フィードバック（規約・好み）
- `memory/agents/` — エージェント間の中間メモ

### `.claude/skills/` — 手続きのオフロード先

- `hardware-test` — ハードウェアテストの記述・実行手順
- `refactor-conventions` — テスト方針・カプセル化の詳細規約
- `testing-strategy` — テスト 4 区分・検証対象優先順位・書く/書かないリスト
- `webui-e2e` — WebUI を実サーバーで E2E 検証する手順（make test-e2e / webui-fake、常駐サーバー kill の回避策）
- `agent-team-startup` — エージェントチームの起動・並列化手順
- `maximize-parallels` — 並列 tool 呼び出しの判定基準と典型パターン
- `edit-dot-claude` — `.claude/` 配下の編集を /tmp 経由で行い permission prompt を抑える手順
- `gitlab-mr` — ブランチを GitLab に push し glab で MR を作成する手順（対象ブランチはデフォルト main）
