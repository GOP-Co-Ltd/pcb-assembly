# CLAUDE.md

Claude Code がこのリポジトリで作業する際に常時参照するガイダンス。
詳細な手続きは `memory/` と `.claude/skills/` にオフロードしている。

## プロジェクト概要

PCB アセンブリ装置の制御コード。Raspberry Pi 5 + Klipper + KiCAD を前提とし、ハードウェア抽象化レイヤ（HAL）、ビジョン処理、制御ロジック、3D 幾何計算を Python 3.12+ で実装する。

主要モジュール構成（`src/pcb_assembly/`）:

- `hal/` — ハードウェア抽象化（カメラ、Klipper、プローブ、サーボ等）
- `vision/` — 画像処理・キャリブレーション・特徴検出
- `control/` — 機器制御ロジック（調整、ペースト流量制御等）
- `geometry/` — 3D 座標と幾何計算（Transform, HeightPlane, 軌跡生成等）
- `pcb/` — PCB 設計情報の抽象化（KiCAD 読込、配置管理）

## 開発コマンド

- `make setup` — 開発環境のセットアップ
- `make test` — 全テスト実行（ハードウェアテスト含む）
- `make test-no-hardware` — ハードウェア以外のテスト実行
- `make format` — pre-commit フック（ruff, docformatter 等）
- `make type` — pyright 型チェック
- `make run` — `format` → `test` → `type` を順実行

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

標準サイクル：`implementation-planner` → `plan-implementer` ⇄ `code-simplifier` → `docs-keeper`。各 agent の中間メモは `memory/agents/<agent-name>/<task>.md`。

## 参照先マップ

### `memory/` — ユーザーとの対話で確立された規約・好み

- `memory/MEMORY.md` — インデックス
- `memory/feedback_*.md` — 個別フィードバック（規約・好み）
- `memory/agents/` — エージェント間の中間メモ

### `.claude/skills/` — 手続きのオフロード先

- `hardware-test` — ハードウェアテストの記述・実行手順
- `refactor-conventions` — テスト方針・カプセル化の詳細規約
- `agent-team-startup` — エージェントチームの起動・並列化手順
