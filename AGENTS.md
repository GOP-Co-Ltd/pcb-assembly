# AGENTS.md

Codex がこのリポジトリで作業する際に常時参照するガイダンス。
詳細な手続きは `memory/` と `.agents/skills/` に置く。

## 応答言語

ユーザーへの応答は日本語で行う。

- 散文・説明・要約・確認・質問・計画は日本語で書く
- コード・コマンド・ファイルパス・識別子・ログ/エラーの引用など、
    原文を保つべきものは原語のまま残す
- コミットメッセージ・PR/MR タイトル等は「Git 運用」規約に従い、
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

検証可能な成功条件を先に置き、実装・検証を完了するまでループする。
複数ステップの作業では短い計画と各ステップの検証方法を示す。

## プロジェクト概要

PCB アセンブリ装置の制御コード。Raspberry Pi 5、Klipper、KiCAD を前提に、
Python 3.12+ で HAL、ビジョン処理、制御ロジック、3D 幾何計算、FastAPI WebUI
を実装する。

主要構成:

- `src/pcbasm/hal/`: カメラ、Klipper、プローブ、サーボ等の HAL
- `src/pcbasm/vision/`: 画像処理、キャリブレーション、特徴検出
- `src/pcbasm/posctrl/`: PCB の位置合わせ共通制御
- `src/pcbasm/pasting/`: ペースト塗布ロジック
- `src/pcbasm/pnp/`: Pick and Place 用名前空間
- `src/pcbasm/geometry/`: 3D 座標と幾何計算
- `src/pcbasm/pcb/`: KiCAD 読込と PCB 設計情報
- `src/pcbasm/visualization/`: 塗布パス・高さ面・PCB の可視化
- `src/web/api/`: 機体ごとの backend WebAPI（FastAPI、port 8081）
- `src/web/ui/`: LAN に 1 つ立てる UI frontend（FastAPI、port 8080。ページ描画と
    `/m/{machine_id}/api/**` の backend 中継）
- `src/ml/`: ドメイン非依存の機械学習基盤（PyTorch。学習・評価・最適化・export）

ブラウザ操作 UI は上記 2 プロセスに分かれる。開発・運用の操作は WebUI のジョブとして
提供する。リポジトリ直下の `scripts/` にはセットアップ・運用スクリプトと
`migrate_codex.py` を置く。

`src/ml/` は装置ドメインを知らない。依存の向きは常に `pcbasm` → `ml` の一方向とし、
`ml` から `pcbasm` / `web` を import しない（`tests/ml/test_architecture.py` が機械検証）。
ML 依存は `pyproject.toml` の `ml-runtime` / `ml-train` / `ml-hpo` / `ml-export`
グループに分け、通常の WebAPI / UI 実行環境へ無条件に入れない。

## 開発コマンド

- `make setup`: 開発環境セットアップ
- `make format`: pre-commit 実行
- `make type`: pyright 型チェック
- `make test`: E2E 以外の全テスト
- `make test-no-hardware`: ハードウェア・E2E を除外
- `make test-ml`: `tests/ml` だけを実行（pcbnew / picamera2 不要）
- `make test-e2e`: WebUI E2E
- `make run`: format、test、type
- `make api` / `make api-dev`: backend WebAPI 起動（port 8081、dev は auto-reload）
- `make api-fake`: fake カメラで backend 起動（隔離 data_dir/port、手動・ブラウザ E2E 用）
- `make ui` / `make ui-dev`: UI frontend 起動（port 8080、dev は auto-reload）
- `make ui-fake`: `api-fake`（8099）を上流にした frontend 起動（隔離ポート 8098）
- `make migrate-codex`: Claude Bash 権限から Codex rules を再生成
- `make migrate-codex-check`: Codex rules の同期確認
- `make setup-ml`: host へ ML 依存（`ml-hpo` + `ml-export`）を入れる
- `make setup-ml-runtime`: Raspberry Pi 5 へ推論だけの `ml-runtime` を入れる
- `make ml-smoke`: ML 環境の確認（version、CUDA、inductor、forward/backward、PNG decode）
- `make ml-docker-build` / `-up` / `-down`: ML 学習・開発コンテナの build と起動・停止
- `make ml-docker-sync` / `-shell` / `-test` / `-smoke` / `-check`: 常駐コンテナへ exec

### ML 開発環境

`src/ml/` と `src/pcbasm/pasting/paste_volume/` の開発と学習は、GPU workstation 上の
専用コンテナで行う（`docker/`）。装置ドメインは pcbnew（KiCAD）と picamera2 を
要求するため、その環境では `make test-no-hardware` が collect できない。

```bash
make ml-docker-up      # image を build して常駐起動する（idempotent）
make ml-docker-check   # format → ML の型検査 → tests/ml。学習機での標準検証
```

コンテナは常駐させ `docker compose exec` で使う。`make ml-docker-shell` /
`-test` / `-smoke` / `-check` はいずれも起動済みコンテナへ exec し、必要なら
起動と依存 install まで遡って行う。`docker compose run --rm` を毎回叩かない。

- base image は Debian Trixie。Python 3.13 と `python3.13-dev` を標準 package で持つため、
    `pyproject.toml` の `python-preference = "only-system"` を変えずに済む。開発ヘッダは
    `torch.compile` の inductor backend が triton の C 拡張を build するのに必要
- Raspberry Pi 5 の `picamera2` / `pcbnew` は OS の `dist-packages` 由来なので、
    `only-system` は変更しない。uv の managed Python へ切り替えると Pi でこれらが見えなくなる
- host 側で `tests/ml` だけを回すこともできる（`make test-ml`）。ML 依存を host へ
    入れる場合は `make setup-ml`。ただし host の system Python には開発ヘッダが無く
    inductor が動かないため、既定はコンテナとする
- コンテナ内から git / glab を使うため、host の資格情報を mount する。`~/.ssh` を
    read-only で渡す場合、read-only は改変を防ぐが読み出しは防がない。詳細と代替
    （SSH agent / HTTPS）は [docker/README.md](docker/README.md)
- `tests/ml` は `tests/helpers`（pcbnew / picamera2 依存）を参照しない。ML 専用の
    テストヘルパーは `tests/ml/helpers.py` に置く。この分離は
    `tests/ml/test_architecture.py` が機械検証する
- 詳細は [docker/README.md](docker/README.md) と
    [画像ベース吐出量推定 ML 実装計画](docs/image-based-dispense-calibration-ml-plan.md)

## 不変の原則

### カプセル化

- 内部実装と `__init__` で設定する属性は原則 `_` prefix
- 外部から必要なものだけ public にする
- private 属性をテスト都合で public にしない

### テスト

- 公開インターフェースと観測可能な振る舞いをテストする
- 実データ・実リソースを優先し、モックを最小化する
- テストは `class TestXxx` 形式に集約する
- 3rd-party 表面や内部関数をモックしない
- ハードウェアテストは `@mark_hardware` で分離する
- 詳細は `testing-strategy`、`hardware-test`、`refactor-conventions` Skill

### WebUI 設計

計算・ドメインロジックは `pcbasm`（`src/pcbasm/`）に集約し、`src/web/api/` の router、
`src/web/ui/` の page ハンドラ、JS はいずれも入出力変換・DOM 操作・表示更新に徹する。

- 解決済み値・派生値・集計はサーバーが算出して返す
- frontend の page ハンドラは backend の JSON（pydantic モデル）をテンプレートへ渡すだけ。
    装置の状態を持たず、backend が返した値を再計算しない
- 表示文字列（マシンの label など）の組み立てはサーバー側で行う
- JS は API レスポンスをそのまま表示へ流し、クライアント検証は UX 最小限にする
- ドメインルール、階層 override 解決、幾何計算を JS や router に複製しない
- サーバーが返す値を JS で再導出せず、編集後はサーバー応答または再取得で更新する
- 詳細は `webui-thin-wrapper`、実起動検証は `webui-e2e` Skill

## Git 運用

- `main` から `<種別>/<日付>/<内容>` で分岐する
- 種別は `feature`, `fix`, `refactor`, `docs`, `chore`
- `main` に直接 commit しない。main への merge はユーザー判断
- commit は `<種別>(<スコープ>): <内容>`、1 commit 1 関心事
- 検証通過前に commit しない
- force push、`git reset --hard`、未確認の破壊的操作を行わない

標準フロー:

要件確認 → ブランチ作成 → 実装 → `make format && make type && make test`
→ commit。実機がない場合は理由を明示して `make test-no-hardware` を使う。

## Custom Agents

ユーザーがエージェント利用や並列作業を明示した場合は、`.codex/agents/` の
custom agent と `agent-team-startup` Skill を使う。

標準サイクル:

`implementation-planner` → 任意 `spec-test-author` →
`plan-implementer` → `code-simplifier` → `docs-keeper`

中間メモは `memory/agents/<agent-name>/<task>.md` に置く。並列 agent は書き込み
範囲を分離し、同じファイルを同時編集しない。

## 参照先

- `memory/MEMORY.md`: ユーザーとの対話で確立した規約・好み
- `.agents/skills/hardware-test/`: 実機テスト
- `.agents/skills/testing-strategy/`: テスト戦略
- `.agents/skills/refactor-conventions/`: 実装・テスト規約
- `.agents/skills/webui-e2e/`: WebUI E2E
- `.agents/skills/webui-thin-wrapper/`: WebUI 薄ラッパー方針
- `.agents/skills/agent-team-startup/`: custom agent 運用
- `.agents/skills/maximize-parallels/`: tool 並列化
- `.agents/skills/do-on-worktree/`: 独立タスクの worktree 運用
- `.agents/skills/gitlab-mr/`: GitLab MR 作成
- `.agents/skills/merge-main/`: MR 前の main 取り込み
- `.agents/skills/migrate-claude/`: Claude 資産から Codex 資産への移行
